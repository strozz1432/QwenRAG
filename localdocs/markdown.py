"""A small markdown parser shared by the screen renderer and the document writers.

parse_blocks(text) -> list of tuples:
    ("heading", level, text)      ("para", text)
    ("bullet", depth, text)       ("number", depth, label, text)
    ("table", header, rows)       ("code", [lines])
    ("rule",)                     ("blank",)
spans(text) -> [(kind, text)] with kind in "", "bold", "italic", "code".
"""
from __future__ import annotations

import re

INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET = re.compile(r"^(\s*)[-*•]\s+(.*)$")
NUMBERED = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
RULE = re.compile(r"\s*([-*_])\1{2,}\s*")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def spans(text: str) -> list:
    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append(("", text[pos:m.start()]))
        if m.group(1) is not None:
            out.append(("bold", m.group(1)))
        elif m.group(2) is not None:
            out.append(("code", m.group(2)))
        else:
            out.append(("italic", m.group(3)))
        pos = m.end()
    if pos < len(text):
        out.append(("", text[pos:]))
    return out


def plain(text: str) -> str:
    return "".join(t for _, t in spans(text))


def split_row(line: str) -> list:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _is_table_start(lines: list, i: int) -> bool:
    return ("|" in lines[i] and i + 1 < len(lines) and "|" in lines[i + 1]
            and TABLE_SEP.match(lines[i + 1]) is not None)


def parse_blocks(text: str) -> list:
    lines = text.replace("\t", "    ").split("\n")
    blocks, i, in_code, md_fence, code = [], 0, False, False, []
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            lang = stripped[3:].strip().lower()
            if md_fence:                                   # closing a ```markdown wrapper
                md_fence = False
            elif in_code:
                blocks.append(("code", code))
                in_code, code = False, []
            elif lang in ("markdown", "md"):               # the model fenced its whole reply: treat as text
                md_fence = True
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code.append(line)
            i += 1
            continue
        if _is_table_start(lines, i):
            header = split_row(line)
            rows, i = [], i + 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                row = split_row(lines[i])
                rows.append((row + [""] * len(header))[:len(header)])
                i += 1
            blocks.append(("table", header, rows))
            continue
        if not stripped:
            blocks.append(("blank",))
        elif m := HEADING.match(line):
            blocks.append(("heading", len(m.group(1)), m.group(2)))
        elif RULE.fullmatch(line):
            blocks.append(("rule",))
        elif m := BULLET.match(line):
            blocks.append(("bullet", len(m.group(1).replace("\t", "    ")) // 2, m.group(2)))
        elif m := NUMBERED.match(line):
            blocks.append(("number", len(m.group(1)) // 2, m.group(2), m.group(3)))
        else:
            blocks.append(("para", line))
        i += 1
    if in_code and code:  # unterminated fence (still streaming)
        blocks.append(("code", code))
    return blocks
