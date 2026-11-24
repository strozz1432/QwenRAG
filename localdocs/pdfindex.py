"""A PDF's index: its bookmarks, or failing that its printed contents page, as page ranges to read."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import ocr


@dataclass
class Entry:
    level: int
    title: str
    start: int   # 1-based, inclusive
    end: int


def _with_ends(raw: list, page_count: int) -> list:
    """(level, title, start) triples -> Entry list sorted by start page, each ending where the next begins."""
    raw = sorted(raw, key=lambda r: r[2])
    out = []
    for i, (level, title, start) in enumerate(raw):
        nxt = raw[i + 1][2] - 1 if i + 1 < len(raw) else page_count
        out.append(Entry(level, title, start, max(start, min(nxt, page_count))))
    return out


def bookmark_index(path: Path) -> list:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        raw = []

        def walk(items, level):
            for item in items:
                if isinstance(item, list):
                    walk(item, level + 1)
                    continue
                try:
                    raw.append((level, str(item.title).strip(), reader.get_destination_page_number(item) + 1))
                except Exception:
                    continue   # a bookmark that points nowhere

        walk(reader.outline, 0)
        return _with_ends([r for r in raw if r[1] and r[2] >= 1], len(reader.pages))
    except Exception:
        return []


def read_pages(path: Path, start: int, end: int) -> str:
    """Text of pages start..end (1-based, inclusive), with OCR for scanned pages."""
    return ocr.read_pdf(path, range(start - 1, end))


CONTENTS_SCAN = 15       # look for the contents page in the first pages only
CALIBRATE_SCAN = 40      # and look for the first heading in the pages after it
_CACHE: dict = {}        # (path, mtime) -> (entries, source)


def _page_text(path: Path, n: int) -> str:
    return ocr.read_pdf(path, range(n - 1, n))


def _looks_like_list(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    return len(lines) >= 3 and sum(bool(re.search(r"\d\s*$", l)) for l in lines) > len(lines) / 2


def contents_index(path: Path, ask=None) -> list:
    """Parse a printed 'Contents' page into an index. [] unless the page numbers can be matched to real pages."""
    try:
        from pypdf import PdfReader
        count = len(PdfReader(str(path)).pages)
        found = None
        for n in range(1, min(count, CONTENTS_SCAN) + 1):
            text = _page_text(path, n)
            if any(re.fullmatch(r"(table of )?contents[\s.:]*", l.strip(), re.I) for l in text.splitlines()):
                found = (n, text)
                break
        if not found:
            return []
        at, text = found
        if at < count:
            nxt = _page_text(path, at + 1)
            if _looks_like_list(nxt):
                text += "\n" + nxt
        if ask is None:
            from .llm import chat as ask
        reply = ask("Below is the contents page of a document. Reply with ONLY a JSON array of its entries in order, each "
                    '{"title": the heading text, "page": the printed page number as an integer, "level": 0 for a chapter, '
                    "1 for a sub-section, 2 below that}. No other text.\n\n" + text)
        block = re.search(r"\[.*\]", reply, re.S)
        items = json.loads(block.group(0)) if block else []
        raw = []
        for it in items:
            title, page = it.get("title"), it.get("page")
            if isinstance(title, str) and title.strip() and isinstance(page, int) and not isinstance(page, bool) and page >= 1:
                level = it.get("level")
                raw.append((min(max(level, 0), 2) if isinstance(level, int) else 0, title.strip(), page))
        if not raw:
            return []
        first_title, first_page = raw[0][1].lower(), raw[0][2]
        offset = None
        for n in range(at + 1, min(count, at + CALIBRATE_SCAN) + 1):
            if first_title in _page_text(path, n)[:200].lower():
                offset = n - first_page
                break
        if offset is None or offset < 0 or any(r[2] + offset > count for r in raw):
            return []
        return _with_ends([(lv, t, pg + offset) for lv, t, pg in raw], count)
    except Exception:
        return []


def get_index(path: Path) -> tuple:
    """(entries, source) where source is 'bookmarks', 'contents page' or ''. Cached until the file changes."""
    key = (str(path), Path(path).stat().st_mtime)
    if key not in _CACHE:
        entries = bookmark_index(path)
        source = "bookmarks" if entries else ""
        if not entries:
            entries = contents_index(path)
            source = "contents page" if entries else ""
        _CACHE[key] = (entries, source)
    return _CACHE[key]
