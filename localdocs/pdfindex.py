"""A PDF's index: its bookmarks, or failing that its printed contents page, as page ranges to read."""
from __future__ import annotations

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
