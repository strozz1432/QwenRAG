"""Free, on-device OCR: reads text out of images and scanned PDF pages.

Uses the open-source Tesseract engine (`brew install tesseract` / `apt install tesseract-ocr`) through its
command line, so there is no API, no cost and nothing leaves the machine. Pillow (already pulled in by pypdf)
prepares the pictures. If Tesseract isn't installed, OCR is simply switched off and everything else works.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import OCR_LANGS, OCR_MAX_PAGES, OCR_MIN_PIXELS, OCR_TIMEOUT

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


def read_image(path: Path) -> str:
    from PIL import Image
    with Image.open(path) as img:
        img.load()
        return _recognise(img)


def read_pdf(path: Path) -> str:
    """Text of a PDF: the embedded text where there is some, OCR of the page picture where there isn't."""
    from pypdf import PdfReader
    pages, ocr_done = [], 0
    for pg in PdfReader(str(path)).pages:
        text = (pg.extract_text() or "").strip()
        if len(text) < SCANNED_PAGE_CHARS and available() and ocr_done < OCR_MAX_PAGES:
            ocr_done += 1
            try:
                text = "\n".join(filter(None, (_recognise(im.image) for im in pg.images))) or text
            except Exception:
                pass                                # an image format pypdf can't decode: keep what text there is
        pages.append(text)
    return "\n".join(pages)
