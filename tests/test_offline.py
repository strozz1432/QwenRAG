"""Offline checks (no Ollama, no network): provider parsing, cloud streaming, OCR. Run: uv run python tests/test_offline.py"""
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from localdocs import config, keys, llm, ocr, store   # noqa: E402
from localdocs import pdfindex, retrieval   # noqa: E402
from localdocs.tui import picker   # noqa: E402
from localdocs.tui.state import S   # noqa: E402


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_split():
    assert llm.split("qwen3:8b") == ("ollama", "qwen3:8b")
    assert llm.split("anthropic:claude-sonnet-5-5") == ("anthropic", "claude-sonnet-5-5")
    assert llm.split("openai:gpt-x") == ("openai", "gpt-x")
    assert llm.split("groq:llama-3.3-70b") == ("groq", "llama-3.3-70b")
    assert llm.split("openrouter:meta-llama/llama-3:free") == ("openrouter", "meta-llama/llama-3:free")
    assert llm.split("llama3:8b") == ("ollama", "llama3:8b")        # a plain Ollama tag is not a provider
    assert llm.protocol("anthropic") == "anthropic" and llm.protocol("gemini") == "openai"
    assert llm.key_var("openrouter") == "OPENROUTER_API_KEY"


def test_streaming():
    os.environ.update(ANTHROPIC_API_KEY="k", OPENAI_API_KEY="k")
    anth = b'data: {"type":"content_block_delta","delta":{"text":"Hi"}}\n\ndata: {"type":"content_block_delta","delta":{"text":" there"}}\n\n'
    oai = b'data: {"choices":[{"delta":{"content":"Yo"}}]}\n\ndata: [DONE]\n\n'
    for model, data, want in [("anthropic:m", anth, "Hi there"), ("openai:m", oai, "Yo")]:
        config.LLM = model
        with mock.patch("urllib.request.urlopen", return_value=Resp(data)):
            assert "".join(llm.stream_chat([{"role": "user", "content": "u"}], [])) == want
    config.LLM = config.DEFAULT_LLM


def test_compatible_provider_and_custom():
    store = {"custom_providers": "mything", "url:mything": "http://localhost:9/v1", "mything": "k", "groq": "k"}
    oai = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
    with mock.patch.object(keys, "get", lambda n: store.get(n, "")):
        assert "mything" in llm.providers() and llm.base_url("mything") == "http://localhost:9/v1"
        assert llm.base_url("groq") == "https://api.groq.com/openai/v1"
        for model in ("groq:llama", "mything:foo"):
            config.LLM = model
            with mock.patch("urllib.request.urlopen", return_value=Resp(oai)) as u:
                assert "".join(llm.stream_chat([{"role": "user", "content": "u"}], [])) == "ok"
                assert u.call_args[0][0].full_url.endswith("/chat/completions")
        # Together answers /models with a bare list, Gemini prefixes ids with "models/"
        for payload in (b'[{"id":"a/b"},{"id":"text-embed-1"}]', b'{"data":[{"id":"models/gemini-x"}]}'):
            with mock.patch("urllib.request.urlopen", return_value=Resp(payload)):
                assert llm.fetch_models("groq")[0] in ("a/b", "gemini-x")
    config.LLM = config.DEFAULT_LLM


def test_claude_cli_prompt_and_gate():
    system, text = llm._cli_prompt([{"role": "system", "content": "S"}, {"role": "user", "content": "a"},
                                    {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}])
    assert system == "S" and text.endswith("\n\nc") and "User: a" in text and "Assistant: b" in text
    assert llm._cli_prompt([{"role": "user", "content": "only"}]) == ("", "only")
    with mock.patch.object(keys, "get", lambda n: ""):
        assert llm.api_key("claude-cli") == ""                       # off until /connect claude-cli
    with mock.patch.object(keys, "get", lambda n: "on" if n == "claude-cli" else ""), \
            mock.patch("shutil.which", lambda n: "/x/claude"):
        assert llm.api_key("claude-cli") == "on" and llm.usable("claude-cli:sonnet") == ""
    assert llm.protocol("claude-cli") == "cli" and llm.split("claude-cli:opus") == ("claude-cli", "opus")


def test_pdf_with_text_and_figure():
    if not ocr.available():
        return
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfgen import canvas
    chart = Image.new("RGB", (700, 300), "white")
    ImageDraw.Draw(chart).text((30, 100), "Quarterly revenue chart", font=ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40), fill="black")
    with tempfile.TemporaryDirectory() as d:
        chart.save(Path(d) / "c.png")
        c = canvas.Canvas(str(Path(d) / "m.pdf"))
        c.drawString(72, 780, "Annual report with a chart below.")
        c.drawImage(str(Path(d) / "c.png"), 72, 450, width=420, height=180)
        c.save()
        text = store.read_text(Path(d) / "m.pdf")
        assert "Annual report" in text and "Quarterly revenue" in text      # the page text AND the words inside the picture
        assert [n for n, _ in ocr.pdf_figures(Path(d) / "m.pdf", 6)] == [1]


def test_ocr_noise_and_named_file():
    assert not ocr.meaningful("\u201cSG") and not ocr.meaningful("") and ocr.meaningful("Order 5521 arrives on Friday morning")
    from localdocs.retrieval import _named_file
    paths = ["/h/Downloads/wp6445533-higanbana-wallpapers.jpg", "/h/x/ic_launcher.png"]
    assert _named_file("wp6445533-higanbana-wallpapers.jpg see the image", paths) == "wp6445533-higanbana-wallpapers.jpg"
    assert _named_file("the image", paths) == ""


def test_missing_key():
    os.environ.pop("OPENAI_API_KEY", None)
    with mock.patch.object(keys, "get", lambda n: ""):
        assert "/connect openai" in llm.usable("openai:x")


def test_redact():
    assert keys.redact("/connect anthropic sk-ant-SECRET") == "/connect anthropic ••••••••"
    assert "SECRET" not in keys.redact("/connect groq gsk_SECRET")
    assert "SECRET" not in keys.redact("/connect mything SECRET http://x/v1") and "http://x/v1" in keys.redact("/connect mything SECRET http://x/v1")
    assert "SECRET" not in keys.redact("/connect openai sk-SECRET http://localhost:1234/v1")
    assert "localhost" in keys.redact("/connect openai sk-SECRET http://localhost:1234/v1")
    assert keys.redact("/connect") == "/connect" and keys.redact("hello sk-x") == "hello sk-x"


def test_saved_key():
    os.environ.pop("ANTHROPIC_API_KEY", None)
    with mock.patch.object(keys, "get", lambda n: "saved-key" if n == "anthropic" else ""):
        assert llm.api_key("anthropic") == "saved-key"
        assert llm.usable("anthropic:claude-sonnet-5-5") == ""
        os.environ["ANTHROPIC_API_KEY"] = "env-key"
        assert llm.api_key("anthropic") == "env-key"     # the environment wins
    os.environ.pop("ANTHROPIC_API_KEY", None)


def test_keystore_roundtrip():
    keys._cache.clear()
    keys.set("test_probe", "abc123")
    keys._cache.clear()
    try:
        assert keys.get("test_probe") == "abc123"
    finally:
        keys.delete("test_probe")
    keys._cache.clear()
    assert keys.get("test_probe") == ""


def test_ocr():
    if not ocr.available():
        print("  (skipped: tesseract not installed)")
        return
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(img).text((30, 60), "Order 5521 arrives Friday", font=ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 44), fill="black")
    with tempfile.TemporaryDirectory() as d:
        img.save(Path(d) / "a.png")
        img.save(Path(d) / "a.pdf")
        for name in ("a.png", "a.pdf"):
            assert "Order 5521 arrives Friday" in store.read_text(Path(d) / name), name


def make_pdf(path, pages, bookmarks=()):
    """pages: text per page. bookmarks: (page, title, level) with 1-based pages and 0-based levels."""
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    marks = {}
    for page, title, level in bookmarks:
        marks.setdefault(page, []).append((title, level))
    for n, text in enumerate(pages, 1):
        for i, (title, level) in enumerate(marks.get(n, [])):
            key = f"b{n}_{i}"
            c.bookmarkPage(key)
            c.addOutlineEntry(title, key, level)
        for i, line in enumerate(text.split("\n")):
            c.drawString(72, 700 - 16 * i, line)
        c.showPage()
    c.save()


def _spans(entries):
    return [(e.start, e.end) for e in entries]


def test_bookmark_index():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.pdf"
        make_pdf(p, [f"page {i} marker" for i in range(1, 7)],
                 [(1, "Intro", 0), (2, "Setup", 1), (4, "Usage", 0), (6, "Appendix", 0)])
        idx = pdfindex.bookmark_index(p)
        assert [e.title for e in idx] == ["Intro", "Setup", "Usage", "Appendix"]
        assert [e.level for e in idx] == [0, 1, 0, 0]
        assert _spans(idx) == [(1, 1), (2, 3), (4, 5), (6, 6)]


def test_bookmark_index_out_of_order_and_same_page():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.pdf"
        make_pdf(p, [f"page {i}" for i in range(1, 6)],
                 [(4, "Usage", 0), (1, "Intro", 0), (4, "Extra", 0)])
        idx = pdfindex.bookmark_index(p)
        assert sorted(e.start for e in idx) == [e.start for e in idx] == [1, 4, 4]
        assert idx[0].end == 3
        assert idx[1].end == 4 and idx[2].end == 5


def test_bookmark_index_none():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.pdf"
        make_pdf(p, ["one", "two"])
        assert pdfindex.bookmark_index(p) == []
        junk = Path(d) / "junk.pdf"
        junk.write_bytes(b"not a pdf at all")
        assert pdfindex.bookmark_index(junk) == []


def test_read_pages():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.pdf"
        make_pdf(p, ["alpha one", "bravo two", "charlie three", "delta four"])
        text = pdfindex.read_pages(p, 2, 3)
        assert "bravo two" in text and "charlie three" in text
        assert "alpha one" not in text and "delta four" not in text


def _contents_pdf(d):
    p = Path(d) / "c.pdf"
    make_pdf(p, ["Cover page", "Contents\nIntro .... 1\nSetup .... 3", "Intro\nwelcome text", "more intro",
                 "Setup\ninstall text", "closing"])
    return p


def test_contents_index_offset():
    with tempfile.TemporaryDirectory() as d:
        p = _contents_pdf(d)
        reply = '[{"title":"Intro","page":1,"level":0},{"title":"Setup","page":3,"level":0}]'
        idx = pdfindex.contents_index(p, ask=lambda prompt: "Sure: " + reply)
        assert [(e.title, e.start, e.end) for e in idx] == [("Intro", 3, 4), ("Setup", 5, 6)]


def test_contents_index_garbage():
    with tempfile.TemporaryDirectory() as d:
        p = _contents_pdf(d)
        assert pdfindex.contents_index(p, ask=lambda prompt: "sorry") == []
        assert pdfindex.contents_index(p, ask=lambda prompt: '[{"title":"X","page":999}]') == []
        assert pdfindex.contents_index(p, ask=lambda prompt: '[{"title":"Setup","page":"three"}]') == []


def test_contents_index_calibration_fails():
    with tempfile.TemporaryDirectory() as d:
        p = _contents_pdf(d)
        reply = '[{"title":"Nowhere","page":1},{"title":"Setup","page":3}]'
        assert pdfindex.contents_index(p, ask=lambda prompt: reply) == []


def test_get_index_prefers_bookmarks_and_caches():
    pdfindex._CACHE.clear()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.pdf"
        make_pdf(p, ["one", "two", "three"], [(1, "A", 0), (2, "B", 0)])
        with mock.patch.object(pdfindex, "contents_index", side_effect=AssertionError("fallback used")), \
             mock.patch.object(pdfindex, "bookmark_index", wraps=pdfindex.bookmark_index) as spy:
            first, src = pdfindex.get_index(p)
            second, src2 = pdfindex.get_index(p)
        assert src == src2 == "bookmarks" and [e.title for e in first] == ["A", "B"]
        assert spy.call_count == 1 and second == first


def test_get_index_none():
    pdfindex._CACHE.clear()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.pdf"
        make_pdf(p, ["one", "two"])
        with mock.patch.object(pdfindex, "contents_index", return_value=[]):
            assert pdfindex.get_index(p) == ([], "")


def _picker(levels, label="bookmarks"):
    entries = [pdfindex.Entry(lv, f"Section {i}", i + 1, i + 1) for i, lv in enumerate(levels)]
    return picker.Picker(entries, label)


def test_picker_toggle_parent_ticks_children():
    p = _picker([0, 1, 1, 0, 1])
    p.key("space", 10)
    assert p.ticked == {0, 1, 2}
    p.key("space", 10)
    assert p.ticked == set()
    p.key("down", 10)
    p.key("space", 10)
    assert p.ticked == {1}


def test_picker_all_and_enter():
    p = _picker([0, 1, 0])
    p.key("enter", 10)
    assert not p.done and "Tick at least one" in S.toast
    p.key("a", 10)
    assert p.ticked == {0, 1, 2}
    p.key("a", 10)
    assert p.ticked == set()
    p.key("down", 10)
    p.key("space", 10)
    p.key("enter", 10)
    assert p.done and p.result == [1]


def test_picker_esc_and_scroll():
    p = _picker([0] * 40)
    for _ in range(25):
        p.key("down", 10)
    assert p.cursor == 25 and p.top <= p.cursor < p.top + 10
    p.key("end", 10)
    assert p.cursor == 39 and p.top <= p.cursor < p.top + 10
    p.key("esc", 10)
    assert p.done and p.result is None


def test_picker_rows_indent_and_marks():
    import re
    p = _picker([0, 1, 0, 5], "contents page")
    p.key("space", 10)      # ticks Section 0 and its child
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", r) for r in picker.picker_rows(p, 80, 8)]
    assert len(plain) == 8
    body = [r for r in plain if "Section" in r]
    assert "[x] Section 0" in body[0] and "[x] Section 1" in body[1] and "[ ] Section 2" in body[2] and "[ ] Section 3" in body[3]
    indent = [len(r) - len(r.lstrip(" ")) for r in (b.replace("❯", " ", 1) for b in body)]
    assert indent[1] - indent[0] == 2 and indent[3] - indent[2] == 4   # one level = 2 columns, capped at two levels
    assert any("contents page" in r for r in plain)
    assert all(len(r) <= 80 for r in plain)
    narrow = [re.sub(r"\x1b\[[0-9;]*m", "", r) for r in picker.picker_rows(p, 20, 8)]
    assert all(len(r) <= 20 for r in narrow)


def test_picker_owns_the_keyboard():
    import asyncio
    from localdocs.tui import controls
    S.stop = asyncio.Event()
    S.busy, S.current, S.buf = True, mock.Mock(), ""
    S.picker = _picker([0, 0])
    try:
        controls.handle(("char", " "))
        controls.handle(("char", "x"))              # typing doesn't leak into the input box
        assert S.picker.ticked == {0} and S.buf == ""
        controls.handle(("key", "esc"))
        assert S.picker.done and S.picker.result is None
        S.current.cancel.assert_not_called()         # esc closed the checklist, not the running job
        assert not S.stop.is_set()
        controls.handle(("key", "ctrl-c"))
        assert S.stop.is_set()                       # ctrl-c still quits
    finally:
        S.picker, S.busy, S.current = None, False, None


def _summary(entries, choose, source="bookmarks"):
    """Run build_summary on a real one-file store with a fake index; returns (result, choose calls)."""
    calls = []
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "book.pdf"
        make_pdf(p, ["x"])
        texts = {1: "AAA text", 2: "BBB text", 3: "CCC text"}
        with mock.patch.object(retrieval, "indexed_paths", return_value=[str(p)]), \
             mock.patch.object(retrieval, "CHUNKS", [(str(p), 0, "WHOLE FILE TEXT", None)]), \
             mock.patch.object(retrieval, "chat", side_effect=lambda prompt, **k: "note"), \
             mock.patch.object(pdfindex, "get_index", return_value=(entries, source)), \
             mock.patch.object(pdfindex, "read_pages", side_effect=lambda path, a, b: texts[a]):
            def wrapped(*a):
                calls.append(a)
                return choose(*a)
            return retrieval.build_summary("book.pdf", choose=wrapped if choose else None), calls


def _summary_with_texts(texts, chosen):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "book.pdf"
        make_pdf(p, ["x"])
        with mock.patch.object(retrieval, "indexed_paths", return_value=[str(p)]), \
             mock.patch.object(retrieval, "CHUNKS", [(str(p), 0, "WHOLE", None)]), \
             mock.patch.object(retrieval, "chat", side_effect=lambda prompt, **k: "note"), \
             mock.patch.object(pdfindex, "get_index", return_value=(_three(), "bookmarks")), \
             mock.patch.object(pdfindex, "read_pages", side_effect=lambda path, a, b: texts[a]):
            return retrieval.build_summary("book.pdf", choose=lambda e, s: chosen), None


def _three():
    return [pdfindex.Entry(0, f"Section {i}", i + 1, i + 1) for i in range(3)]


def test_summary_uses_only_chosen_sections():
    info, calls = _summary(_three(), lambda entries, source: [1])
    assert "BBB text" in info["prompt"] and "Section 1" in info["prompt"]
    assert "AAA" not in info["prompt"] and "CCC" not in info["prompt"] and "WHOLE FILE" not in info["prompt"]
    assert calls and calls[0][1] == "bookmarks"


def test_summary_all_ticked_is_whole_file():
    info, _ = _summary(_three(), lambda entries, source: [0, 1, 2])
    assert "WHOLE FILE TEXT" in info["prompt"] and "AAA" not in info["prompt"]


def test_summary_cancel():
    info, _ = _summary(_three(), lambda entries, source: None)
    assert "Cancelled" in info["msg"]


def test_summary_no_index_or_single_entry_skips_picker():
    for entries in ([], _three()[:1]):
        info, calls = _summary(entries, lambda e, s: [0])
        assert calls == [] and "WHOLE FILE TEXT" in info["prompt"]


def test_summary_without_chooser_is_unchanged():
    info, calls = _summary(_three(), None)
    assert "WHOLE FILE TEXT" in info["prompt"]


def test_summary_notes_a_detected_index():
    info, _ = _summary(_three(), lambda e, s: [0], source="contents page")
    assert "contents page" in info["note"]


def test_chooser_refuses_once_the_job_was_cancelled():
    import threading
    cancelled = threading.Event()
    choose = picker.chooser(cancelled)
    cancelled.set()
    S.loop = None                                     # opening a checklist would fail loudly
    assert choose(_three(), "bookmarks") is None


def test_contents_scan_reads_embedded_text_only():
    with tempfile.TemporaryDirectory() as d:
        p = _contents_pdf(d)
        reply = '[{"title":"Intro","page":1},{"title":"Setup","page":3}]'
        with mock.patch.object(ocr, "read_pdf", side_effect=AssertionError("OCR used while detecting the index")):
            idx = pdfindex.contents_index(p, ask=lambda prompt: reply)
        assert [e.start for e in idx] == [3, 5]


def test_summary_reports_progress_while_reading_sections():
    log = []
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "book.pdf"
        make_pdf(p, ["x"])
        with mock.patch.object(retrieval, "indexed_paths", return_value=[str(p)]), \
             mock.patch.object(retrieval, "CHUNKS", [(str(p), 0, "WHOLE", None)]), \
             mock.patch.object(pdfindex, "get_index", return_value=(_three(), "bookmarks")), \
             mock.patch.object(pdfindex, "read_pages", side_effect=lambda path, a, b: log.append(("read", a)) or f"text {a}"):
            retrieval.build_summary("book.pdf", progress=lambda *a: log.append(("progress", a[2] if len(a) > 2 else "", a[0])),
                                    choose=lambda e, s: [0, 2])
    reads = [i for i, x in enumerate(log) if x[0] == "read"]
    assert len(reads) == 2
    for i in reads:   # right before each read, the status names the section being read, not "Waiting for your pick"
        before = log[i - 1]
        assert before[0] == "progress" and before[1] == "Reading the sections" and "Section" in before[2]


def test_summary_does_not_invent_empty_sections():
    info, _ = _summary_with_texts({1: "  ", 2: "BBB text", 3: "CCC"}, [0, 1])
    assert "BBB text" in info["prompt"] and "Section 0" in info["prompt"] and "no readable text" in info["prompt"].lower()
    assert "## Section 0\n" not in info["prompt"]
    info, _ = _summary_with_texts({1: "", 2: "\n", 3: "CCC"}, [0, 1])
    assert "no readable text" in info["msg"].lower()


def test_index_titles_have_no_control_characters():
    e = pdfindex._with_ends([(0, "Bad\x1b]0;pwn\x07\nTitle\t!", 1)], 1)[0]
    assert not any(ord(c) < 32 or 127 <= ord(c) < 160 for c in e.title) and "Title" in e.title


def _notes(entries, source, choose):
    """The activity lines build_summary writes into the conversation, in order."""
    notes = []
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "book.pdf"
        make_pdf(p, ["x"])
        with mock.patch.object(retrieval, "indexed_paths", return_value=[str(p)]), \
             mock.patch.object(retrieval, "CHUNKS", [(str(p), 0, "WHOLE FILE TEXT", None)]), \
             mock.patch.object(retrieval, "chat", side_effect=lambda prompt, **k: "note"), \
             mock.patch.object(pdfindex, "get_index", return_value=(entries, source)), \
             mock.patch.object(pdfindex, "read_pages", side_effect=lambda path, a, b: "text " * 600 * a):
            retrieval.build_summary("book.pdf", progress=lambda *a: a[3:4] and a[3] and notes.append(a[3]), choose=choose)
    return notes


def _order(notes, *needles):
    at = []
    for n in needles:
        hit = [i for i, line in enumerate(notes) if n in line]
        assert hit, f"no activity line with {n!r} in {notes}"
        at.append(hit[0])
    assert at == sorted(at), f"out of order: {notes}"


def test_activity_lines_show_index_then_pick_then_chunking():
    notes = _notes(_three(), "bookmarks", lambda e, s: [1, 2])
    _order(notes, "Checking the PDF for an index", "Found an index", "Picked 2 of 3", "Chunked", "Key points, part 1")
    assert any("bookmarks" in n and "3 sections" in n for n in notes)


def test_activity_lines_when_there_is_no_index():
    notes = _notes([], "", lambda e, s: [0])
    _order(notes, "Checking the PDF for an index", "No index found", "Read it all")
    assert not any("Found an index" in n or "Picked" in n for n in notes)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
