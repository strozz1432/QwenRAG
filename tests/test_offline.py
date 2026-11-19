"""Offline checks (no Ollama, no network): provider parsing, cloud streaming, OCR. Run: uv run python tests/test_offline.py"""
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from localdocs import config, keys, llm, ocr, store   # noqa: E402


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
