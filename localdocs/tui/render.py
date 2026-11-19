"""Turning the conversation into rows, and drawing the screen."""
from __future__ import annotations

import math
import textwrap
import time
from ..markdown import parse_blocks, spans
from ..config import BAR_WIDTH, LONG_AFTER, SHOW_PROGRESS, SPINNER, STEER_MARK
from ..keys import redact
from .state import RESET, S, STY, fmt_secs, term_size, write

def line_style(line: str) -> str:
    if line.startswith("❯ "):
        return "user"
    if line.startswith(STEER_MARK + " "):
        return "user"  # a steer note looks like any other message you sent
    if line.startswith("◈ "):
        return "thought"
    if line.startswith("! "):
        return "err"
    if line.startswith("◆ "):
        return "banner"
    if line.startswith(("Sources:", "✓ ", "· ")):
        return "dim"
    return ""


def inline(text: str, base: str = "") -> list:
    """Markdown inline spans -> [(style, text)]: **bold**, *italic*, `code`."""
    return [((base or "bold") if k == "bold" else (k or base), t) for k, t in spans(text)]


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

def seg_len(segs) -> int:
    return sum(len(t) for _, t in segs)


def table_rows(header: list, body: list, W: int) -> list:
    """Draw a markdown table with box lines, wrapping cells so it fits W columns."""
    n = len(header)
    cells = [[inline(c, "bluebold") for c in header]] + [[inline(c) for c in r] for r in body]
    widths = [max(3, *(seg_len(r[c]) for r in cells)) for c in range(n)]
    room = W - (3 * n + 1)
    if room < n * 4:  # too narrow for a grid: one "Header: value" line per cell instead
        out = []
        for r in body:
            for h, c in zip(header, r):
                for i, seg in enumerate(wrap_segs([("bold", h + ": ")] + inline(c), W)):
                    out.append(("", seg, i > 0))
            out.append(("", [("", "")], False))
        return out
    while sum(widths) > room:  # shrink the widest column until it fits
        widths[widths.index(max(widths))] -= 1

    def border(l, m, r):
        return ("rule", [("", l + m.join("─" * (w + 2) for w in widths) + r)], False)

    def line(row):
        wrapped = [wrap_segs(c, w) for c, w in zip(row, widths)]
        out = []
        for k in range(max(len(w) for w in wrapped)):
            segs = [("rule", "│")]
            for c, w in enumerate(widths):
                part = wrapped[c][k] if k < len(wrapped[c]) else []
                segs += [("", " ")] + part + [("", " " * (w - seg_len(part) + 1)), ("rule", "│")]
            out.append(("", segs, False))
        return out
    multi = any(len(wrap_segs(c, w)) > 1 for r in cells[1:] for c, w in zip(r, widths))
    rows = [border("┌", "┬", "┐"), *line(cells[0]), border("├", "┼", "┤")]
    for i, r in enumerate(cells[1:]):
        rows += line(r)
        if multi and i < len(cells) - 2:
            rows.append(border("├", "┼", "┤"))
    rows.append(border("└", "┴", "┘"))
    return rows


def block_rows(text: str, W: int) -> list:
    """Render a run of ordinary (non-chrome) text lines as styled rows."""
    rows = []
    for b in parse_blocks(text):
        kind = b[0]
        if kind == "blank":
            rows.append(("", [("", "")], False))
        elif kind == "rule":
            rows.append(("rule", [("", "─" * W)], False))
        elif kind == "table":
            rows += table_rows(b[1], b[2], W)
        elif kind == "code":
            for line in b[1]:
                for i, seg in enumerate(wrap_segs([("code", "  " + line)], W)):
                    rows.append(("", seg, i > 0))
        else:
            prefix, base = [], ""
            if kind == "heading":
                body, base = b[2].replace("**", ""), "heading"
            elif kind == "bullet":
                prefix, body = [("", "  " * b[1]), ("bluebold", "◦ " if b[1] else "• ")], b[2]
            elif kind == "number":
                prefix, body = [("", "  " * b[1]), ("bluebold", b[2] + " ")], b[3]
            else:
                body = b[1]
            hang = seg_len(prefix)
            for i, seg in enumerate(wrap_segs(inline(body, base), W - hang)):
                lead = prefix if i == 0 else ([("", " " * hang)] if hang else [])
                rows.append(("", lead + seg, i > 0))
    return rows


BOX_OPEN, BOX_SEP, BOX_CLOSE = "\ue000", "\ue001", "\ue002"   # markers around a collapsible per-file box


def _line_rows(lines: list, W: int) -> list:
    """Rows for ordinary conversation lines (chrome lines styled, the rest rendered as markdown)."""
    rows, run = [], []

    def flush():
        if run:
            rows.extend(block_rows("\n".join(run), W))
            run.clear()

    for line in lines:
        st = line_style(line)
        if not st:
            run.append(line)
            continue
        flush()  # chrome lines: user echo, thinking time, errors, banner, sources
        for i, seg in enumerate(wrap_segs([(st if st == "dim" else "", line)], W)):
            if st == "user":
                pad = W - seg_len(seg)
                seg = seg + [("", " " * pad)] if pad > 0 else seg
            rows.append((st, seg, i > 0))
    flush()
    return rows


def _box_rows(title: str, body: list, done: bool, expanded: bool, W: int) -> list:
    """A header row with a dropdown arrow, plus (when open) the live output inside a left-ruled box."""
    arrow = "▾" if expanded else "▸"
    head = [("bluebold", f"{arrow} "), ("bold", title[:max(10, W - 22)])]
    if not done:
        head.append(("dim", "   ● writing…"))
    elif not expanded:
        head.append(("dim", "   click to open"))
    if not expanded and not done:
        last = next((l.strip() for l in reversed(body) if l.strip() and not l.startswith("· ")), "")
        if last:
            head.append(("dim", "  " + last.replace("**", "")[-max(0, W - len(title) - 30):]))
    rows = [("boxhead", head, False)]
    if expanded:
        for base, segs, cont in _line_rows(body, max(10, W - 3)):
            rows.append((base, [("rule", "│ ")] + segs, cont))
        rows.append(("rule", [("", "╰" + "─" * min(W - 2, 30))], False))
    return rows


def build_rows(W: int) -> list:
    """Render the conversation into display rows (cached until it changes).
    A row is (base_style, [(style, text), ...], is_continuation)."""
    key = (len(S.text), W, frozenset(S.open_boxes))
    if S.rows_key == key:
        return S.rows
    rows, plain = [], []
    S.box_rows = {}
    lines = S.text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(BOX_OPEN):
            rows.extend(_line_rows(plain, W))
            plain.clear()
            bid, _, title = line[1:].partition(BOX_SEP)
            body, i = [], i + 1
            while i < len(lines) and not lines[i].startswith(BOX_CLOSE):
                body.append(lines[i])
                i += 1
            done = i < len(lines)
            S.box_rows[len(rows)] = bid
            rows.extend(_box_rows(title, body, done, bid in S.open_boxes, W))
        else:
            plain.append(line)
        i += 1
    rows.extend(_line_rows(plain, W))
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


def fit(segs, W: int) -> str:
    """Join (style, text) segments, clipped to W columns."""
    out, used = [], 0
    for style, text in segs:
        if used >= W:
            break
        text = text[:W - used]
        used += len(text)
        out.append(STY[style] + text + RESET)
    return "".join(out)


def pulse():
    """Slow grey <-> white breathing (about 2.4s per cycle) for the dim text shown while working."""
    phase = (1 - math.cos(time.monotonic() * 2 * math.pi / 2.4)) / 2  # 0..1
    STY["pulse"] = f"\x1b[38;5;{243 + round(phase * 7)}m"  # 240 = grey ... 255 = white


def status_rows():
    if S.label is None:
        return [""], [""]
    pulse()
    elapsed = time.monotonic() - S.t0
    frame = SPINNER[int(time.monotonic() * 8) % len(SPINNER)]
    if elapsed < LONG_AFTER:  # quick job: just a quiet "thinking", no internals
        return [("bluebold", f" {frame} "), ("pulse", "Thinking… "), ("dim", f"({fmt_secs(elapsed)})")], [""]
    segs = [("bluebold", f" {frame} "), ("pulse", f"{S.label}… ")]
    if SHOW_PROGRESS and S.progress is not None:
        filled = int(round(min(max(S.progress, 0), 1) * BAR_WIDTH))
        segs += [("bluebold", "█" * filled), ("dim", "░" * (BAR_WIDTH - filled)),
                 ("", f" {int(S.progress * 100)}% ")]
    segs.append(("dim", f"({fmt_secs(elapsed)} · esc to interrupt)"))
    return segs, [("dim", f"   ↳ {S.detail}")] if S.detail else [""]


def draw():
    S.dirty = False
    W, lines = term_size()
    rows = build_rows(W)
    total = len(rows)
    q = 1 if (S.pending and S.follow) else 0
    stack_n = 4 + q if S.follow else 1
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
    if q:
        first = redact(S.pending[0]).replace("\n", " ")
        more = f"  (+{len(S.pending) - 1} more)" if len(S.pending) > 1 else ""
        out.append(fit([("dim", f"   ⋯ queued: {first[:max(10, W - 24)]}{'…' if len(first) > max(10, W - 24) else ''}{more}")], W))

    cursor = ""
    if S.follow:
        out.append(fit([("rule", "─" * W)], W))
        room = max(1, W - 3)
        start = max(0, S.cur - room)
        masked = redact(S.buf)
        if masked != S.buf:      # an API key being typed: show dots, cursor at the end
            shown, start = masked[-room:], 0
            col = 3 + len(shown)
        else:
            shown = S.buf[start:start + room]
            col = 3 + S.cur - start
        if S.buf.startswith("/") and start == 0:
            cmd, sp, rest = shown.partition(" ")
            body = [("bluebold", cmd), ("", sp + rest)]
        else:
            body = [("", shown)]
        out.append(fit([("bluebold", "❯ ")] + body, W))
        out.append(fit([("rule", "─" * W)], W))
        if time.monotonic() < S.toast_until:
            hint = S.toast
        elif S.busy and S.buf.strip():
            hint = " enter: send after this finishes · esc: stop now and send this instead"
        elif S.busy:
            hint = " type to queue a message · /steer <note> to adjust it · esc interrupt · ctrl+c exit"
        else:
            hint = " / commands · tab · wheel/pgup/pgdn scroll · drag to select & copy · esc interrupt · ctrl+c exit"
        out.append(fit([("dim", hint)], W))
        cursor = f"\x1b[{H + 4 + q};{col}H\x1b[?25h"
    else:
        out.append(fit([("button", " ↓ Jump to bottom "), ("dim", "  click, press End, or just start typing")], W))
        S.jump_row = lines

    frame = "\x1b[?25l" + "".join(f"\x1b[{i + 1};1H{r}\x1b[K" for i, r in enumerate(out[:lines])) + cursor
    write(frame)


