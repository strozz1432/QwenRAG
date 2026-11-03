"""The local vector store (SQLite) plus scanning and indexing of your folders."""
from __future__ import annotations

import array
import html
import operator
import re
import sqlite3
import zipfile
from pathlib import Path
from .config import CHUNK, OVERLAP
from .config import DB_PATH, EMBED, EXTS, MAX_BYTES, ROOTS, SKIP_DIRS
from .llm import embed

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


