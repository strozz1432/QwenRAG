"""Free, on-device OCR: reads text out of images and scanned PDF pages.

Uses the open-source Tesseract engine (`brew install tesseract` / `apt install tesseract-ocr`) through its
command line, so there is no API, no cost and nothing leaves the machine. Pillow (already pulled in by pypdf)
prepares the pictures. If Tesseract isn't installed, OCR is simply switched off and everything else works.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import FIGURE_MAX_OCR, FIGURE_MIN_PIXELS, OCR_LANGS, OCR_MAX_PAGES, OCR_MIN_PIXELS, OCR_TIMEOUT

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
SCANNED_PAGE_CHARS = 25   # a PDF page with fewer extracted characters than this is treated as a scan


def available() -> bool:
    return shutil.which("tesseract") is not None


def _recognise(img) -> str:
    """OCR one PIL image."""
    if min(img.size) < OCR_MIN_PIXELS:
        return ""                                  # icons, thumbnails, bullets: nothing worth reading
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "page.png"
        img.save(src)
        try:
            out = subprocess.run(["tesseract", str(src), "stdout", "-l", OCR_LANGS, "--psm", "3"],
                                 capture_output=True, text=True, timeout=OCR_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def meaningful(text: str) -> bool:
    """False for OCR noise such as a stray quote and two letters from a wallpaper."""
    words = re.findall(r"[A-Za-z\u00C0-\u024F]{3,}", text)
    return len(words) >= 3 and sum(len(w) for w in words) >= 0.4 * max(1, len(re.sub(r"\s", "", text)))


def read_image(path: Path) -> str:
    """Text found in a picture, or '' if there is none worth keeping."""
    from PIL import Image
    with Image.open(path) as img:
        img.load()
        text = _recognise(img)
    return text if meaningful(text) else ""


def _page_images(pg) -> list:
    try:
        return [im.image for im in pg.images]
    except Exception:
        return []                                   # an image format pypdf can't decode


def is_figure(img) -> bool:
    return min(img.size) >= FIGURE_MIN_PIXELS


def read_pdf(path: Path, pages_wanted: range | None = None) -> str:
    """Text of a PDF: the embedded text, OCR of scanned pages, and any text inside figures on the other pages.
    `pages_wanted` is a range of 0-based page indexes; the default is every page."""
    from pypdf import PdfReader
    pages, scanned, figures = [], 0, 0
    for n, pg in enumerate(PdfReader(str(path)).pages, 1):
        if pages_wanted is not None and (n - 1) not in pages_wanted:
            continue
        text = (pg.extract_text() or "").strip()
        if not available():
            pages.append(text)
            continue
        if len(text) < SCANNED_PAGE_CHARS and scanned < OCR_MAX_PAGES:
            scanned += 1
            text = "\n".join(filter(None, (_recognise(im) for im in _page_images(pg)))) or text
        elif figures < FIGURE_MAX_OCR:               # a text page: also read the words inside its pictures (charts, screenshots)
            for im in _page_images(pg):
                if is_figure(im) and figures < FIGURE_MAX_OCR:
                    figures += 1
                    found = _recognise(im)
                    if meaningful(found):
                        text += f"\n[Text inside an image on page {n}: {found}]"
        pages.append(text)
    return "\n".join(pages)


def pdf_figures(path: Path, limit: int) -> list:
    """The largest figures in a PDF as (page number, PIL image), distinct sizes only, at most `limit`."""
    from pypdf import PdfReader
    found, seen = [], set()
    for n, pg in enumerate(PdfReader(str(path)).pages, 1):
        for im in _page_images(pg):
            key = (im.size, n if im.size[0] * im.size[1] < 10000 else 0)
            if is_figure(im) and key not in seen:
                seen.add(key)
                found.append((n, im))
    found.sort(key=lambda f: f[1].size[0] * f[1].size[1], reverse=True)
    return sorted(found[:limit], key=lambda f: f[0])
