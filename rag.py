#!/usr/bin/env python3
"""Local, private document assistant. Standard library only (plus pypdf for PDFs).

On start it scans Documents/Desktop/Downloads, indexes new or changed files
(PDF, DOCX, TXT, MD) into a local SQLite file using Ollama embeddings, then lets
you chat or ask about your files. Nothing leaves this machine.
"""
from __future__ import annotations

import array
import asyncio
import base64
import codecs
import html
import json
import operator
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import termios
import textwrap
import threading
import time
import tty
import urllib.request
import zipfile
from pathlib import Path

LLM = "qwen3:8b"
EMBED = "qwen3-embedding:0.6b"
OLLAMA = "http://localhost:11434"
ROOTS = [Path.home() / d for d in ("Documents", "Desktop", "Downloads")]
EXTS = {".pdf", ".docx", ".txt", ".md"}
SKIP_DIRS = {".venv", "node_modules", ".git", "site-packages", "chroma_db", "Library"}
MAX_BYTES = 30 * 1024 * 1024
CHUNK, OVERLAP = 2500, 300  # characters
NUM_CTX = 8192
DB_PATH = Path(__file__).parent / "index.db"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SHOW_PROGRESS = False  # set True to bring the progress bar back
BAR_WIDTH = 8
FORMAT = ("Format the reply for a terminal so it is easy to scan: short paragraphs separated by blank lines; "
          "'- ' bullet lists for several points and '1. ' lists for steps; **bold** on key terms, names and numbers; "
          "short '## ' headings only when the answer is long. Never write tables or one long block of text.")
SUMMARY_FORMAT = ("Structure it like this: a one-sentence overview in **bold**, a blank line, then the key points as "
                  "'- ' bullets with **bold** lead-ins, grouped under short '## ' headings if there are several topics. "
                  "No tables.")

COMMANDS = [
    ("/sum", "/sum ", "summarise a file (filename or topic)"),
    ("/files", "/files", "list every indexed file"),
    ("/reindex", "/reindex", "rescan your folders for new or changed files"),
    ("/clear", "/clear", "forget this conversation and clear the screen"),
    ("/help", "/help", "show these commands"),
    ("/quit", "/quit", "exit"),
]

# ---------------------------------------------------------------- vector store

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL);
CREATE TABLE IF NOT EXISTS chunks(path TEXT, idx INTEGER, text TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
""")
CHUNKS: list = []        # (path, idx, text, normalised vector)
FILE_NAMES: list = []    # unique basenames, for tab completion


def normalise(v) -> array.array:
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return array.array("f", (x / n for x in v))


def load_store():
    new = []
    for path, idx, text, blob in db.execute("SELECT path, idx, text, vec FROM chunks"):
        v = array.array("f")
        v.frombytes(blob)
        new.append((path, idx, text, v))
    CHUNKS[:] = new
    FILE_NAMES[:] = sorted({Path(c[0]).name for c in new})


def indexed_paths() -> list:
    return sorted({c[0] for c in CHUNKS})


def search(qvec, k: int = 5) -> list:
    q = normalise(qvec)
    scored = sorted(((sum(map(operator.mul, q, c[3])), c) for c in CHUNKS),
                    key=lambda s: s[0], reverse=True)
    return [c for _, c in scored[:k]]


# ---------------------------------------------------------------- ollama over http

def _post(path: str, payload: dict):
    req = urllib.request.Request(OLLAMA + path, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=600)


def check_ollama():
    urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3).close()


def embed(texts: list) -> list:
    with _post("/api/embed", {"model": EMBED, "input": texts}) as r:
        return json.load(r)["embeddings"]


def chat(prompt: str) -> str:
    body = {"model": LLM, "messages": [{"role": "user", "content": prompt}],
            "stream": False, "think": False, "options": {"num_ctx": NUM_CTX}}
    with _post("/api/chat", body) as r:
        return json.load(r)["message"]["content"].strip()


# ---------------------------------------------------------------- scanning and indexing

def find_files():
    for root in ROOTS:
        if not root.exists():
            continue
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                entries = list(d.iterdir())
            except OSError:
                continue
            for p in entries:
                if p.name.startswith("."):
                    continue
                if p.is_dir():
                    if p.name not in SKIP_DIRS and not p.name.endswith(".app"):
                        stack.append(p)
                elif p.suffix.lower() in EXTS and not p.name.startswith("~$"):
                    try:
                        if p.stat().st_size <= MAX_BYTES:
                            yield p
                    except OSError:
                        pass


def read_text(p: Path) -> str:
    s = p.suffix.lower()
    if s == ".pdf":
        from pypdf import PdfReader  # the one third-party dependency
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
    if s == ".docx":
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf8", "ignore")
        xml = xml.replace("</w:p>", "\n").replace("<w:tab/>", "\t")
        return html.unescape(re.sub(r"<[^>]+>", "", xml))
    return p.read_text(errors="ignore")


def chunks(text: str):
    step = CHUNK - OVERLAP
    for i in range(0, len(text), step):
        c = text[i:i + CHUNK].strip()
        if c:
            yield c


def index(progress=None) -> int:
    files = list(find_files())
    seen = {str(p) for p in files}
    known = dict(db.execute("SELECT path, mtime FROM files"))
    for gone in set(known) - seen:
        db.execute("DELETE FROM chunks WHERE path=?", (gone,))
        db.execute("DELETE FROM files WHERE path=?", (gone,))
    for n, p in enumerate(files, 1):
        mtime = p.stat().st_mtime
        if known.get(str(p)) == mtime:
            continue
        frac = n / len(files)
        if progress:
            progress(f"[{n}/{len(files)}] reading {p.name}", frac)
        try:
            parts = list(chunks(read_text(p)))
        except Exception:
            parts = []
        rows = []
        for i in range(0, len(parts), 32):
            batch = parts[i:i + 32]
            if progress:
                progress(f"[{n}/{len(files)}] ollama.embed {EMBED} · {len(parts)} chunks · {p.name}", frac)
            for j, (t, v) in enumerate(zip(batch, embed(batch))):
                rows.append((str(p), i + j, t, normalise(v).tobytes()))
        db.execute("DELETE FROM chunks WHERE path=?", (str(p),))
        db.executemany("INSERT INTO chunks VALUES (?,?,?,?)", rows)
        db.execute("INSERT OR REPLACE INTO files VALUES (?,?)", (str(p), mtime))
        db.commit()
    db.commit()
    load_store()
    return len(files)


# ---------------------------------------------------------------- retrieval and summaries

MEMORY: list = []  # recent chat turns as {"role", "content"}; lives in RAM only
MEMORY_MAX_MSGS, MEMORY_MAX_CHARS = 12, 6000


def remember(user: str, assistant: str):
    MEMORY.extend([{"role": "user", "content": user}, {"role": "assistant", "content": assistant}])
    del MEMORY[:-MEMORY_MAX_MSGS]
    while len(MEMORY) > 2 and sum(len(m["content"]) for m in MEMORY) > MEMORY_MAX_CHARS:
        del MEMORY[:2]


def needs_docs(message: str) -> bool:
    """Cheap router so greetings and small talk don't trigger a document search."""
    recent = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in MEMORY[-4:]) or "(none)"
    out = chat(
        "Does the latest message need facts from the user's files or documents? "
        "Greetings, thanks, small talk, general questions, and follow-ups that only work on "
        "the earlier conversation (rewrite, shorten, explain, translate what was just said) "
        "are NO. Reply with only YES or NO.\n\n"
        f"Recent conversation:\n{recent}\n\nLatest message: {message}"
    )
    return out.upper().startswith("YES")


def retrieve(question: str) -> list:
    return search(embed([question])[0], 5)


def build_summary(fragment: str, progress=None) -> dict:
    """Return {'msg': ...} on failure, else {'prompt', 'name', 'note'} ready to stream."""
    paths = indexed_paths()
    frag = fragment.lower()
    matches = [p for p in paths if frag in Path(p).name.lower()]
    if not matches:  # also allow typing part of the folder path
        matches = [p for p in paths if frag in p.lower()]
    note = None
    if len(matches) > 1 and len({Path(p).name for p in matches}) == 1:
        # same filename in several folders: use the most recently modified copy
        matches = [max(matches, key=lambda p: Path(p).stat().st_mtime)]
        note = "Several files share this name, using the newest:"
    if not matches:
        top = retrieve(fragment)
        if not top:
            return {"msg": "Nothing is indexed yet."}
        matches = [top[0][0]]
        note = f"No filename matches '{fragment}'. Closest by content:"
    if len(matches) > 1:
        return {"msg": "Several matches, be more specific:\n" + "\n".join(f"  {m}" for m in matches[:10])}
    path = matches[0]
    parts = [c[2] for c in sorted((c for c in CHUNKS if c[0] == path), key=lambda c: c[1])]
    groups = ["\n".join(parts[i:i + 4]) for i in range(0, len(parts), 4)]
    if len(groups) <= 1:
        prompt = f"Summarise this document clearly and concisely. {SUMMARY_FORMAT}\n\n{groups[0] if groups else ''}"
    else:
        partials = []
        for i, g in enumerate(groups, 1):
            if progress:
                progress(f"ollama.chat {LLM} · summarising section {i}/{len(groups)}", 0.1 + 0.5 * (i - 1) / len(groups))
            partials.append(chat(f"Summarise this passage in a few sentences:\n\n{g}"))
        while len(partials) > 6:
            if progress:
                progress(f"ollama.chat {LLM} · merging {len(partials)} section notes", 0.6)
            joined = ["\n".join(partials[i:i + 6]) for i in range(0, len(partials), 6)]
            partials = [chat(f"Combine these notes into one concise summary:\n\n{j}") for j in joined]
        prompt = ("Combine these notes about one document into a clear, concise summary. "
                  f"{SUMMARY_FORMAT}\n\n" + "\n".join(partials))
    return {"prompt": prompt, "name": path.replace(str(Path.home()), "~", 1), "note": note}


# ---------------------------------------------------------------- terminal: state and drawing

class S:
    text = ""                 # the whole conversation, plain text
    follow = True             # True = pinned to the bottom with the input box visible
    top = 0                   # first visible row when scrolled away
    label: str | None = None  # spinner text while busy
    progress: float | None = None
    detail = ""               # dim "what is running now" line
    t0 = 0.0
    current = None            # the running turn task, for esc
    dirty = True
    buf = ""                  # input box text
    cur = 0
    hist: list = []
    hist_i = None
    menu: list = []           # (display, description, insert-text)
    menu_sel = -1
    menu_prefix = ""
    rows_key = None
    rows: list = []
    jump_row = -1
    H = 1                     # height of the conversation area, set by draw()
    sel_a = None              # selection anchor (row, col), None = no selection
    sel_b = None
    dragging = False
    toast = ""
    toast_until = 0.0
    pasting = None
    stop: asyncio.Event


RESET = "\x1b[0m"
BLUE = "\x1b[38;5;75m"
STY = {
    "": "",
    "user": "\x1b[48;5;238;38;5;255m",
    "thought": "\x1b[2;3m",
    "dim": "\x1b[38;5;245m",
    "err": "\x1b[38;5;203m",
    "banner": "\x1b[1;38;5;75m",
    "rule": "\x1b[38;5;240m",
    "bluebold": "\x1b[1;38;5;75m",
    "button": "\x1b[48;5;75;38;5;16;1m",
    "sel": "\x1b[48;5;75;38;5;16m",
    "bold": "\x1b[1m",
    "italic": "\x1b[3m",
    "code": "\x1b[38;5;180m",
    "heading": "\x1b[1;4;38;5;75m",
    "hl": "\x1b[48;5;24;38;5;255m",
    "menu": "\x1b[48;5;235;38;5;252m",
    "menudim": "\x1b[48;5;235;38;5;245m",
}
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def term_size():
    try:
        c, l = os.get_terminal_size(1)
    except OSError:
        c, l = 100, 30
    return max(c, 20), max(l, 8)


def write(s: str):
    data = s.encode()
    while data:
        try:
            data = data[os.write(1, data):]
        except BlockingIOError:
            time.sleep(0.001)


def emit(text: str):
    text = text.replace("\r", "").replace("\t", "    ")
    S.text += CTRL.sub("", text)
    S.dirty = True


def echo(text: str):
    S.follow = True
    emit(f"\n❯ {text}\n\n")


def fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60}s"


def line_style(line: str) -> str:
    if line.startswith("❯ "):
        return "user"
    if line.startswith("✻ "):
        return "thought"
    if line.startswith("! "):
        return "err"
    if line.startswith("◆ "):
        return "banner"
    if line.startswith(("Sources:", "✓ ", "· ")):
        return "dim"
    return ""


INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET = re.compile(r"^(\s*)[-*•]\s+(.*)$")
NUMBERED = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")


def inline(text: str, base: str = "") -> list:
    """Markdown inline spans -> [(style, text)]: **bold**, *italic*, `code`."""
    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append((base, text[pos:m.start()]))
        if m.group(1) is not None:
            out.append(("bold" if not base else base, m.group(1)))
        elif m.group(2) is not None:
            out.append(("code", m.group(2)))
        else:
            out.append(("italic", m.group(3)))
        pos = m.end()
    if pos < len(text):
        out.append((base, text[pos:]))
    return out


def wrap_segs(segs: list, width: int) -> list:
    """Word-wrap styled segments to `width`, keeping each character's style. Returns a list of seg lists."""
    plain = "".join(t for _, t in segs)
    styles = [st for st, t in segs for _ in t]
    pieces = textwrap.wrap(plain, max(1, width), replace_whitespace=False, drop_whitespace=False,
                           break_long_words=True) or [""]
    rows, pos = [], 0
    for n, piece in enumerate(pieces):
        row = []
        skip = len(piece) - len(piece.lstrip(" ")) if n else 0  # wrapped rows don't start with a space
        for ch, st in zip(piece[skip:], styles[pos + skip:pos + len(piece)]):
            if row and row[-1][0] == st:
                row[-1] = (st, row[-1][1] + ch)
            else:
                row.append((st, ch))
        pos += len(piece)
        rows.append(row or [("", "")])
    return rows


def build_rows(W: int) -> list:
    """Render the conversation (light markdown) into display rows (cached until it changes).
    A row is (base_style, [(style, text), ...], is_continuation)."""
    key = (len(S.text), W)
    if S.rows_key == key:
        return S.rows
    rows, in_code = [], False
    for line in S.text.split("\n"):
        st = line_style(line)
        if st:  # chrome lines: user echo, thinking time, errors, banner, sources
            for i, seg in enumerate(wrap_segs([(st if st == "dim" else "", line)], W)):
                if st == "user":
                    pad = W - sum(len(t) for _, t in seg)
                    seg = seg + [("", " " * pad)] if pad > 0 else seg
                rows.append((st, seg, i > 0))
            continue
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            for i, seg in enumerate(wrap_segs([("code", "  " + line)], W)):
                rows.append(("", seg, i > 0))
            continue
        prefix, body, base = [], line, ""
        if m := HEADING.match(line):
            body, base = m.group(2).replace("**", ""), "heading"
        elif re.fullmatch(r"\s*([-*_])\1{2,}\s*", line):
            rows.append(("rule", [("", "─" * W)], False))
            continue
        elif m := BULLET.match(line):
            ind = m.group(1).replace("\t", "    ")
            prefix, body = [("", ind), ("bluebold", "◦ " if ind else "• ")], m.group(2)
        elif m := NUMBERED.match(line):
            prefix, body = [("", m.group(1)), ("bluebold", m.group(2) + " ")], m.group(3)
        hang = sum(len(t) for _, t in prefix)
        segs = inline(body, base)
        for i, seg in enumerate(wrap_segs(segs, W - hang)):
            lead = prefix if i == 0 else ([("", " " * hang)] if hang else [])
            rows.append(("", lead + seg, i > 0))
    S.rows, S.rows_key = rows, key
    return rows


def row_plain(row) -> str:
    return "".join(t for _, t in row[1])


def render_row(idx: int, row, W: int) -> str:
    base, segs, _ = row
    lo = hi = None
    if S.sel_a is not None and S.sel_a != S.sel_b:
        (r1, c1), (r2, c2) = sorted((S.sel_a, S.sel_b))
        if r1 <= idx <= r2:
            lo = c1 if idx == r1 else 0
            hi = c2 if idx == r2 else 10**9
    out, col = [], 0
    for style, text in segs:
        n = len(text)
        if lo is None or hi <= col or lo >= col + n:
            parts = [(style, text)]
        else:
            a, b = max(lo - col, 0), min(hi - col, n)
            parts = [(style, text[:a]), ("hl", text[a:b]), (style, text[b:])]
        for st, tx in parts:
            if tx:
                out.append((STY[base] if st != "hl" else "") + STY[st] + tx + RESET)
        col += n
    return "".join(out)


def status_rows():
    if S.label is None:
        return [""], [""]
    frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
    segs = [("bluebold", f" {frame} "), ("", f"{S.label}… ")]
    if SHOW_PROGRESS and S.progress is not None:
        filled = int(round(min(max(S.progress, 0), 1) * BAR_WIDTH))
        segs += [("bluebold", "█" * filled), ("dim", "░" * (BAR_WIDTH - filled)),
                 ("", f" {int(S.progress * 100)}% ")]
    segs.append(("dim", f"({fmt_secs(time.monotonic() - S.t0)} · esc to interrupt)"))
    return segs, [("dim", f"   ↳ {S.detail}")] if S.detail else [""]


def draw():
    S.dirty = False
    W, lines = term_size()
    rows = build_rows(W)
    total = len(rows)
    stack_n = 4 if S.follow else 1
    H = max(1, lines - 2 - stack_n)
    maxtop = max(0, total - H)
    S.top = maxtop if S.follow else min(S.top, maxtop)

    S.H = H
    out = [render_row(S.top + i, r, W) for i, r in enumerate(rows[S.top:S.top + H])]
    out += [""] * (H - len(out))

    if S.follow and S.menu:  # completion menu, drawn just above the status lines
        ofs = max(0, S.menu_sel - 7) if S.menu_sel >= 8 else 0
        shown = S.menu[ofs:ofs + 8]
        dw = max(len(m[0].strip()) for m in shown)
        for i, (disp, desc, _) in enumerate(shown):
            sel = ofs + i == S.menu_sel
            st, std = ("sel", "sel") if sel else ("menu", "menudim")
            out[H - len(shown) + i] = fit([(st, f"  {disp.strip():<{dw}}  "), (std, f"{desc}  ")], W)

    s1, s2 = status_rows()
    out.append(fit(s1, W) if s1 != [""] else "")
    out.append(fit(s2, W) if s2 != [""] else "")

    cursor = ""
    if S.follow:
        out.append(fit([("rule", "─" * W)], W))
        room = max(1, W - 3)
        start = max(0, S.cur - room)
        shown = S.buf[start:start + room]
        if S.buf.startswith("/") and start == 0:
            cmd, sp, rest = shown.partition(" ")
            body = [("bluebold", cmd), ("", sp + rest)]
        else:
            body = [("", shown)]
        out.append(fit([("bluebold", "❯ ")] + body, W))
        out.append(fit([("rule", "─" * W)], W))
        hint = S.toast if time.monotonic() < S.toast_until else \
            " / commands · tab · wheel/pgup/pgdn scroll · drag to select & copy · esc interrupt · ctrl+c exit"
        out.append(fit([("dim", hint)], W))
        cursor = f"\x1b[{H + 4};{3 + S.cur - start}H\x1b[?25h"
    else:
        out.append(fit([("button", " ↓ Jump to bottom "), ("dim", "  click, press End, or just start typing")], W))
        S.jump_row = lines

    frame = "\x1b[?25l" + "".join(f"\x1b[{i + 1};1H{r}\x1b[K" for i, r in enumerate(out[:lines])) + cursor
    write(frame)


# ---------------------------------------------------------------- terminal: scrolling and input

def text_pos(x: int, y: int):
    """Screen cell (1-based) -> (row index, column) in the conversation."""
    rows = build_rows(term_size()[0])
    r = min(max(S.top + y - 1, 0), max(len(rows) - 1, 0))
    return r, min(max(x - 1, 0), len(row_plain(rows[r])) if rows else 0)


def copy_selection():
    rows = build_rows(term_size()[0])
    (r1, c1), (r2, c2) = sorted((S.sel_a, S.sel_b))
    out = []
    for i in range(r1, min(r2, len(rows) - 1) + 1):
        t = row_plain(rows[i])
        t = t[(c1 if i == r1 else 0):(c2 if i == r2 else len(t))].rstrip()
        # soft-wrapped rows rejoin into one line; real line breaks stay
        if rows[i][2] and out:
            out[-1] += " " + t.lstrip()
        else:
            out.append(t)
    text = "\n".join(out).strip("\n")
    if not text:
        return
    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=text.encode(), check=False)
    write("\x1b]52;c;" + base64.b64encode(text.encode()).decode() + "\x07")
    S.toast, S.toast_until = f" ✓ Copied {len(text)} characters", time.monotonic() + 2.5


def clear_selection():
    S.sel_a = S.sel_b = None
    S.dragging = False


def jump_bottom():
    S.follow = True
    S.dirty = True


def scroll(delta: int):
    W, lines = term_size()
    total = len(build_rows(W))
    h_follow, h_away = max(1, lines - 6), max(1, lines - 3)
    maxtop_away = max(0, total - h_away)
    if S.follow:
        if delta >= 0 or total <= h_follow:
            return
        S.follow = False
        S.top = max(0, maxtop_away + delta)
    else:
        S.top = min(max(S.top + delta, 0), maxtop_away)
        if delta > 0 and S.top >= maxtop_away:
            jump_bottom()
    S.dirty = True


def page() -> int:
    return max(1, term_size()[1] - 8)


def compute_menu():
    S.menu_sel = -1
    b = S.buf[:S.cur]
    if b.startswith("/") and " " not in b:
        S.menu = [(d, desc, ins) for d, ins, desc in COMMANDS if ins.startswith(b)]
        S.menu_prefix = ""
    elif b.startswith("/sum "):
        frag = b[5:].lower()
        S.menu = [(n, "", n) for n in FILE_NAMES if frag in n.lower()]
        S.menu_prefix = "/sum "
    else:
        S.menu = []


def set_buf(text: str):
    S.buf, S.cur = text, len(text)


def complete(step: int):
    if not S.menu:
        compute_menu()
    if not S.menu:
        return
    n = len(S.menu)
    S.menu_sel = (0 if step > 0 else n - 1) if S.menu_sel < 0 else (S.menu_sel + step) % n
    set_buf(S.menu_prefix + S.menu[S.menu_sel][2])


def edited():
    S.hist_i = None
    compute_menu()


def insert(text: str):
    S.buf = S.buf[:S.cur] + text + S.buf[S.cur:]
    S.cur += len(text)
    edited()


def history(step: int):
    if not S.hist:
        return
    i = len(S.hist) if S.hist_i is None else S.hist_i
    i += step
    if i >= len(S.hist):
        S.hist_i = None
        set_buf("")
    else:
        S.hist_i = max(0, i)
        set_buf(S.hist[S.hist_i])
    S.menu = []


def submit():
    text = S.buf.strip()
    set_buf("")
    S.menu, S.menu_sel, S.hist_i = [], -1, None
    if not text:
        return
    if not S.hist or S.hist[-1] != text:
        S.hist.append(text)
    task = asyncio.ensure_future(turn(text))
    task.add_done_callback(lambda t: (not t.cancelled() and t.exception()) and emit(f"! Error: {t.exception()}\n"))


def parse(s: str):
    """Turn raw terminal input into events: ('char',c) ('key',name) ('mouse',b,x,y,press) ('paste',text)."""
    keys = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "shift-tab"}
    tilde = {"1": "home", "7": "home", "4": "end", "8": "end", "3": "delete", "5": "pgup", "6": "pgdn"}
    ctrl = {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace", "\t": "tab",
            "\x03": "ctrl-c", "\x04": "ctrl-d", "\x01": "home", "\x05": "end", "\x15": "ctrl-u", "\x17": "ctrl-w"}
    i = 0
    while i < len(s):
        if S.pasting is not None:
            j = s.find("\x1b[201~", i)
            if j < 0:
                S.pasting.append(s[i:])
                return
            S.pasting.append(s[i:j])
            yield ("paste", "".join(S.pasting))
            S.pasting, i = None, j + 6
            continue
        ch = s[i]
        if ch == "\x1b":
            if i + 1 >= len(s) or s[i + 1] not in "[O":
                yield ("key", "esc")
                i += 1
                continue
            j = i + 2
            while j < len(s) and not ("@" <= s[j] <= "~"):
                j += 1
            seq, i = s[i + 2:j + 1], j + 1
            if seq.startswith("<") and seq[-1] in "Mm":
                try:
                    b, x, y = (int(v) for v in seq[1:-1].split(";"))
                    yield ("mouse", b, x, y, seq[-1] == "M")
                except ValueError:
                    pass
            elif seq == "200~":
                S.pasting = []
            elif seq[-1] == "~" and seq[:-1] in tilde:
                yield ("key", tilde[seq[:-1]])
            elif seq[-1] in keys:
                yield ("key", keys[seq[-1]])
        elif ch in ctrl:
            yield ("key", ctrl[ch])
            i += 1
        elif ch >= " ":
            yield ("char", ch)
            i += 1
        else:
            i += 1


def handle(ev):
    kind = ev[0]
    if kind != "mouse":
        S.sel_a = S.sel_b = None
    if kind == "mouse":
        _, b, x, y, press = ev
        if b == 64:
            scroll(-3)
        elif b == 65:
            scroll(3)
        elif b == 0 and press and not S.follow and y == S.jump_row:
            jump_bottom()
        elif b == 0 and press and y <= S.H:
            S.sel_a = S.sel_b = text_pos(x, y)
            S.dragging = True
        elif b == 32 and S.dragging:  # drag
            if y <= 1:
                scroll(-1)
            elif y >= S.H:
                scroll(1)
            S.sel_b = text_pos(x, y)
        elif b == 0 and not press and S.dragging:
            S.dragging = False
            if S.sel_a != S.sel_b:
                copy_selection()
            else:
                S.sel_a = S.sel_b = None
        S.dirty = True
        return
    if kind == "paste":
        jump_bottom()
        insert(ev[1].replace("\r", "").replace("\n", " "))
        return
    if kind == "char":
        jump_bottom()
        insert(ev[1])
        return
    name = ev[1]
    if name in ("ctrl-c", "ctrl-d"):
        S.stop.set()
        return
    if name == "esc":
        if S.menu:
            S.menu, S.menu_sel = [], -1
        elif S.current:
            S.current.cancel()
        S.dirty = True
        return
    if not S.follow:
        moves = {"up": -1, "down": 1, "pgup": -page(), "pgdn": page(), "home": -10**9}
        if name in moves:
            scroll(moves[name])
            return
        jump_bottom()  # end, or any other key: return to the input box
        if name == "end":
            return
    S.dirty = True
    if name == "enter":
        if S.menu_sel >= 0:
            S.menu, S.menu_sel = [], -1
        else:
            submit()
    elif name in ("tab", "shift-tab"):
        complete(1 if name == "tab" else -1)
    elif name in ("up", "down"):
        if S.menu:
            complete(-1 if name == "up" else 1)
        else:
            history(-1 if name == "up" else 1)
    elif name == "backspace" and S.cur:
        S.buf, S.cur = S.buf[:S.cur - 1] + S.buf[S.cur:], S.cur - 1
        edited()
    elif name == "delete":
        S.buf = S.buf[:S.cur] + S.buf[S.cur + 1:]
        edited()
    elif name == "left":
        S.cur = max(0, S.cur - 1)
    elif name == "right":
        S.cur = min(len(S.buf), S.cur + 1)
    elif name == "home":
        S.cur = 0
    elif name == "end":
        S.cur = len(S.buf)
    elif name == "ctrl-u":
        S.buf, S.cur = S.buf[S.cur:], 0
        edited()
    elif name == "ctrl-w":
        head = S.buf[:S.cur].rstrip()
        head = head[:head.rfind(" ") + 1]
        S.buf, S.cur = head + S.buf[S.cur:], len(head)
        edited()
    elif name == "pgup":
        scroll(-page())


# ---------------------------------------------------------------- turns

def step(label=None, detail="", progress=None, log=True):
    """Update the live status line and (optionally) leave a dim note in the conversation."""
    if label:
        S.label = label
    S.detail, S.progress = detail, progress
    if log and detail:
        emit(f"· {detail}\n")


def set_progress(detail: str, frac: float):
    S.detail, S.progress = detail, frac  # safe to call from a worker thread


async def stream_reply(messages, start=0.45):
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    cancel, holder = threading.Event(), []

    def worker():
        try:
            body = {"model": LLM, "messages": messages, "stream": True, "think": False,
                    "options": {"num_ctx": NUM_CTX}}
            with _post("/api/chat", body) as r:
                holder.append(r)
                for line in r:
                    if cancel.is_set():
                        break
                    part = json.loads(line)
                    tok = part.get("message", {}).get("content", "")
                    if tok:
                        loop.call_soon_threadsafe(q.put_nowait, tok)
                    if part.get("done"):
                        break
        except Exception as e:
            if not cancel.is_set():
                loop.call_soon_threadsafe(q.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    threading.Thread(target=worker, daemon=True).start()
    first, n, full = True, 0, []
    try:
        while (tok := await q.get()) is not None:
            if isinstance(tok, Exception):
                raise tok
            if first:
                S.label, first = "Writing", False
                emit("\n")
            n += 1
            # real length is unknown, so ease towards 95% as tokens arrive
            S.progress = start + (0.95 - start) * (1 - 0.5 ** (n / 120))
            S.detail = f"ollama.chat {LLM} · streaming · {n} tokens"
            full.append(tok)
            emit(tok)
        return "".join(full)
    finally:
        cancel.set()
        for r in holder:
            try:
                r.close()
            except Exception:
                pass


async def answer(q: str):
    step("Thinking", f"router: {LLM} · does this need your files?", 0.1, log=False)
    if not await asyncio.to_thread(needs_docs, q):
        step(detail="router → general chat, skipping document search", progress=0.3)
        reply = await stream_reply([
            {"role": "system", "content": "You are a friendly, concise private assistant running "
             "locally on the user's laptop. You can answer questions about their documents and "
             "summarise files with /sum. Keep replies short. " + FORMAT},
            *MEMORY, {"role": "user", "content": q}], start=0.3)
        remember(q, reply)
        return
    step("Searching your files", f"ollama.embed {EMBED} → cosine search, top 5", 0.25)
    # short follow-ups ("and the second one?") borrow the previous question for searching
    prev = next((m["content"] for m in reversed(MEMORY) if m["role"] == "user"), "")
    top = await asyncio.to_thread(retrieve, f"{prev} {q}" if prev and len(q.split()) < 8 else q)
    if not top:
        emit("Nothing is indexed yet.")
        return
    names = sorted({Path(c[0]).name for c in top})
    emit(f"· found {len(top)} chunks in {len(names)} files: {', '.join(names)}\n")
    context = "\n\n".join(f"[{Path(c[0]).name}]\n{c[2]}" for c in top)
    step("Thinking", f"ollama.chat {LLM} · answering from {len(top)} chunks", 0.4)
    reply = await stream_reply([*MEMORY, {"role": "user", "content":
        "Answer the question using only the context below. If the answer is not there, say so "
        f"plainly. Do not list file names in your answer. {FORMAT}\n\n"
        f"Context:\n{context}\n\nQuestion: {q}"}])
    remember(q, reply)
    emit("\n\nSources: " + ", ".join(names))


async def summarise(arg: str):
    step("Reading the file", f"finding '{arg}'", 0.05)
    info = await asyncio.to_thread(build_summary, arg, set_progress)
    if "msg" in info:
        emit(info["msg"])
        return
    emit((info["note"] + " " if info["note"] else "Summarising ") + info["name"] + "\n")
    step("Thinking", f"ollama.chat {LLM} · writing the final summary", 0.7)
    reply = await stream_reply([{"role": "user", "content": info["prompt"]}], start=0.7)
    remember(f"Summarise {info['name']}", reply)


async def turn(text: str):
    echo(text)
    cmd, _, arg = text.partition(" ")
    if cmd in ("/quit", "/exit"):
        S.stop.set()
        return
    if cmd == "/help":
        emit("\n".join(f"  {d.strip():<9} {desc}" for d, _, desc in COMMANDS) +
             "\n  anything else: chat, or ask about your files\n\n")
        return
    if cmd == "/clear":
        MEMORY.clear()
        S.text, S.rows_key, S.follow = "", None, True
        emit(f"◆ Local Docs · {LLM} · private, on-device\n✓ Conversation cleared.\n")
        return
    if cmd == "/files":
        emit("\n".join(f"  {p}" for p in indexed_paths()) + "\n\n")
        return
    if cmd.startswith("/") and cmd not in ("/sum", "/reindex"):
        emit(f"! Unknown command {cmd}. Type /help.\n\n")
        return
    if cmd == "/sum" and not arg.strip():
        emit("! Usage: /sum <filename or topic>\n\n")
        return
    async with lock:
        S.current = asyncio.current_task()
        S.t0 = time.monotonic()
        try:
            if cmd == "/reindex":
                step("Scanning your folders", "walking Documents, Desktop, Downloads", None, log=False)
                n = await asyncio.to_thread(index, set_progress)
                emit(f"✓ {n} documents indexed.\n\n")
            else:
                await (summarise(arg.strip()) if cmd == "/sum" else answer(text))
                emit(f"\n\n✻ Thought for {fmt_secs(time.monotonic() - S.t0)}\n")
        except asyncio.CancelledError:
            emit("\n\n! Interrupted\n")
        except Exception as e:
            emit(f"\n\n! Error: {e}\n")
        finally:
            S.label, S.detail, S.progress, S.current = None, "", None, None


async def startup():
    async with lock:
        S.t0 = time.monotonic()
        await asyncio.to_thread(load_store)
        step("Scanning your folders", "walking Documents, Desktop, Downloads", None, log=False)
        try:
            await asyncio.to_thread(check_ollama)
        except Exception:
            emit(f"! Can't reach Ollama at {OLLAMA}. Start it with `ollama serve`, then type /reindex.\n")
            S.label = None
            return
        try:
            n = await asyncio.to_thread(index, set_progress)
            emit(f"✓ {n} documents indexed, all on this machine.\n")
        except Exception as e:
            emit(f"! Indexing failed: {e}\n")
        finally:
            S.label, S.detail, S.progress = None, "", None


async def render_loop():
    while not S.stop.is_set():
        if S.toast_until and time.monotonic() >= S.toast_until:
            S.toast_until, S.dirty = 0.0, True
        if S.dirty or S.label:
            draw()
        await asyncio.sleep(0.05)


async def main():
    global lock
    lock = asyncio.Lock()
    S.stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    decoder = codecs.getincrementaldecoder("utf-8")("ignore")

    def on_stdin():
        data = os.read(fd, 65536)
        if not data:
            S.stop.set()
            return
        for ev in parse(decoder.decode(data)):
            handle(ev)
        S.dirty = True

    try:
        tty.setraw(fd)
        # alt screen, mouse (wheel + click), bracketed paste
        write("\x1b[?1049h\x1b[?1002h\x1b[?1006h\x1b[?2004h")
        loop.add_reader(fd, on_stdin)
        loop.add_signal_handler(signal.SIGWINCH, lambda: setattr(S, "dirty", True))
        emit(f"◆ Local Docs · {LLM} · private, on-device\n")
        asyncio.ensure_future(startup())
        render = asyncio.ensure_future(render_loop())
        await S.stop.wait()
        render.cancel()
    finally:
        loop.remove_reader(fd)
        write("\x1b[?2004l\x1b[?1006l\x1b[?1002l\x1b[?25h\x1b[?1049l")
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    asyncio.run(main())
