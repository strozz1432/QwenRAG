"""Create documents (.docx, .pdf, .md, .txt) from markdown, and append to existing ones.

Appending never edits in place without a safety copy: the original goes to
<output dir>/.backups first.
"""
from __future__ import annotations

import io
import re
import shutil
import time
from pathlib import Path
from xml.sax.saxutils import escape

from .config import OUTPUT_DIR
from .markdown import parse_blocks, spans

WRITABLE = {".docx", ".pdf", ".md", ".txt"}


# ---------------------------------------------------------------- Word

def _runs(paragraph, text: str, force_bold: bool = False):
    from docx.shared import Pt
    for kind, t in spans(text):
        run = paragraph.add_run(t)
        run.bold = True if (kind == "bold" or force_bold) else None
        run.italic = True if kind == "italic" else None
        if kind == "code":
            run.font.name = "Courier New"
            run.font.size = Pt(10)


def _style(doc, name: str, fallback: str = "Normal") -> str:
    try:
        doc.styles[name]
        return name
    except KeyError:
        return fallback


def _add_docx_blocks(doc, blocks: list):
    for b in blocks:
        kind = b[0]
        if kind == "heading":
            doc.add_heading(level=min(b[1], 4)).add_run(" ".join(t for _, t in spans(b[2])))
        elif kind == "para":
            _runs(doc.add_paragraph(), b[1])
        elif kind == "bullet":
            _runs(doc.add_paragraph(style=_style(doc, "List Bullet 2" if b[1] else "List Bullet")), b[2])
        elif kind == "number":
            _runs(doc.add_paragraph(style=_style(doc, "List Number 2" if b[1] else "List Number")), b[3])
        elif kind == "code":
            from docx.shared import Pt
            p = doc.add_paragraph()
            run = p.add_run("\n".join(b[1]))
            run.font.name, run.font.size = "Courier New", Pt(10)
        elif kind == "table":
            header, rows = b[1], b[2]
            table = doc.add_table(rows=1, cols=len(header))
            table.style = _style(doc, "Table Grid")
            for cell, h in zip(table.rows[0].cells, header):
                cell.text = ""
                _runs(cell.paragraphs[0], h, force_bold=True)
            for r in rows:
                cells = table.add_row().cells
                for cell, val in zip(cells, r):
                    cell.text = ""
                    _runs(cell.paragraphs[0], val)
            doc.add_paragraph()
        elif kind == "rule":
            doc.add_paragraph("―" * 20)


def _write_docx(path: Path, blocks: list, append: bool, at_start: bool = False):
    import docx
    doc = docx.Document(str(path)) if append else docx.Document()
    if append and at_start:  # build the new content separately, then move it to the top of the body
        new = docx.Document()
        _add_docx_blocks(new, blocks)
        body = doc.element.body
        for el in reversed([e for e in new.element.body if not e.tag.endswith("}sectPr")]):
            body.insert(0, el)
        doc.save(str(path))
        return
    if append:
        doc.add_paragraph()
    _add_docx_blocks(doc, blocks)
    doc.save(str(path))


# ---------------------------------------------------------------- PDF

_FONTS: dict = {}


def _fonts() -> tuple:
    """(regular, bold, italic, mono) font names; Arial when macOS has it so accents and symbols work."""
    if _FONTS:
        return _FONTS["f"]
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    base = Path("/System/Library/Fonts/Supplemental")
    files = {"DocSans": "Arial.ttf", "DocSans-Bold": "Arial Bold.ttf", "DocSans-Italic": "Arial Italic.ttf"}
    try:
        for name, f in files.items():
            pdfmetrics.registerFont(TTFont(name, str(base / f)))
        result = ("DocSans", "DocSans-Bold", "DocSans-Italic", "Courier")
    except Exception:
        result = ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Courier")
    _FONTS["f"] = result
    return result


def _markup(text: str, bold_all: bool = False) -> str:
    out = []
    for kind, t in spans(text):
        t = escape(t)
        if kind == "bold":
            t = f"<b>{t}</b>"
        elif kind == "italic":
            t = f"<i>{t}</i>"
        elif kind == "code":
            t = f'<font face="Courier">{t}</font>'
        out.append(t)
    s = "".join(out)
    return f"<b>{s}</b>" if bold_all else s


def _pdf_bytes(blocks: list) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.pdfbase import pdfmetrics

    reg, bold, ital, mono = _fonts()
    if reg == "DocSans":
        pdfmetrics.registerFontFamily("DocSans", normal="DocSans", bold="DocSans-Bold",
                                      italic="DocSans-Italic", boldItalic="DocSans-Bold")
    body = ParagraphStyle("body", fontName=reg, fontSize=10.5, leading=15, alignment=TA_LEFT, spaceAfter=5)
    heads = {n: ParagraphStyle(f"h{n}", parent=body, fontName=bold, fontSize=s, leading=s + 5,
                               spaceBefore=10, spaceAfter=6)
             for n, s in {1: 20, 2: 15.5, 3: 13, 4: 11.5}.items()}
    cell = ParagraphStyle("cell", parent=body, fontSize=9.5, leading=12.5, spaceAfter=0)
    story = []
    for b in blocks:
        kind = b[0]
        if kind == "heading":
            story.append(Paragraph(_markup(b[2].replace("**", "")), heads[min(b[1], 4)]))
        elif kind == "para":
            story.append(Paragraph(_markup(b[1]), body))
        elif kind == "bullet":
            st = ParagraphStyle("b", parent=body, leftIndent=14 + 14 * b[1], bulletIndent=2 + 14 * b[1])
            story.append(Paragraph(_markup(b[2]), st, bulletText="•" if not b[1] else "–"))
        elif kind == "number":
            st = ParagraphStyle("n", parent=body, leftIndent=18 + 14 * b[1], bulletIndent=2 + 14 * b[1])
            story.append(Paragraph(_markup(b[3]), st, bulletText=b[2]))
        elif kind == "code":
            story.append(Preformatted("\n".join(b[1]), ParagraphStyle("c", fontName=mono, fontSize=9, leading=11.5,
                                                                         backColor=colors.whitesmoke, borderPadding=4)))
            story.append(Spacer(1, 6))
        elif kind == "table":
            header, rows = b[1], b[2]
            data = [[Paragraph(_markup(h, bold_all=True), cell) for h in header]]
            data += [[Paragraph(_markup(c), cell) for c in r] for r in rows]
            t = Table(data, repeatRows=1, hAlign="LEFT")
            t.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#b0b0b0")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story += [t, Spacer(1, 8)]
        elif kind == "rule":
            story.append(Spacer(1, 8))
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                      topMargin=18 * mm, bottomMargin=18 * mm).build(story or [Spacer(1, 1)])
    return buf.getvalue()


def _write_pdf(path: Path, blocks: list, append: bool, at_start: bool = False):
    from pypdf import PdfReader, PdfWriter
    new = io.BytesIO(_pdf_bytes(blocks))
    if not append:
        path.write_bytes(new.getvalue())
        return
    writer = PdfWriter()
    old = PdfReader(str(path))
    for part in ((PdfReader(new), old) if at_start else (old, PdfReader(new))):
        writer.append(part)
    tmp = path.with_suffix(".pdf.tmp")
    with open(tmp, "wb") as f:
        writer.write(f)
    tmp.replace(path)


# ---------------------------------------------------------------- public API

def _write_text(path: Path, markdown: str, append: bool, at_start: bool = False):
    text = markdown.strip()
    if path.suffix.lower() == ".txt":  # plain text: no markdown markers
        text = "\n".join(re.sub(r"^#+\s*", "", ln).replace("**", "") for ln in text.split("\n"))
    if append and at_start:
        old = path.read_text(encoding="utf-8", errors="replace")
        path.write_text(text + "\n\n" + old, encoding="utf-8")
    elif append:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n\n" + text + "\n")
    else:
        path.write_text(text + "\n", encoding="utf-8")


def write_document(path: Path, markdown: str, append: bool = False, at_start: bool = False) -> Path | None:
    """Create (or append to) a document; returns the backup path when appending."""
    ext = path.suffix.lower()
    if ext not in WRITABLE:
        raise ValueError(f"I can write {', '.join(sorted(WRITABLE))} files, not {ext or 'this type'}.")
    backup = None
    if append:
        if not path.exists():
            raise FileNotFoundError(path)
        backup_dir = OUTPUT_DIR / ".backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{path.stem}.{time.strftime('%Y%m%d-%H%M%S')}{ext}"
        shutil.copy2(path, backup)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    blocks = parse_blocks(markdown)
    if ext == ".docx":
        _write_docx(path, blocks, append, at_start)
    elif ext == ".pdf":
        _write_pdf(path, blocks, append, at_start)
    else:
        _write_text(path, markdown, append, at_start)
    return backup
