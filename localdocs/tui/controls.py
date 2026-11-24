"""Scrolling, selection, the input box and key/mouse handling."""
from __future__ import annotations

import base64
import shutil
import subprocess
import time
from .. import config
from ..config import COMMANDS
from ..keys import redact
from ..llm import api_key, cloud_models, installed_models, providers
from ..store import FILE_NAMES
from .render import build_rows, row_plain
from .state import S, term_size, write

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
    h_follow, h_away = max(1, lines - 6 - (1 if S.pending else 0)), max(1, lines - 3)
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

WRITABLE_EXT = (".docx", ".pdf", ".md", ".txt")


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
    elif b.startswith("/add ") and " " not in b[5:]:
        frag = b[5:].lower()
        S.menu = [(n, "", n + " ") for n in FILE_NAMES if frag in n.lower() and n.lower().endswith(WRITABLE_EXT)]
        S.menu_prefix = "/add "
    elif b.startswith("/connect ") and " " not in b[9:]:
        S.menu = [(n, "", n + " ") for n in providers() if n.startswith(b[9:].lower())]
        S.menu_prefix = "/connect "
    elif b.startswith("/disconnect ") and " " not in b[12:]:
        S.menu = [(n, "", n) for n in providers() if api_key(n) and n.startswith(b[12:].lower())]
        S.menu_prefix = "/disconnect "
    elif b.startswith("/model "):
        frag = b[7:].lower()
        S.menu = [(n, "current" if n == config.LLM else "", n) for n in installed_models() + cloud_models() if frag in n.lower()]
        S.menu_prefix = "/model "
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


def handle_picker(ev):
    """While the checklist is open it owns the keyboard (esc closes only it; ctrl-c still quits)."""
    kind = ev[0]
    name = {"key": ev[1], "char": {" ": "space"}.get(ev[1], ev[1]) if kind == "char" else None}.get(kind)
    if kind == "mouse" and ev[1] in (64, 65):
        name = "up" if ev[1] == 64 else "down"
    if name in ("ctrl-c", "ctrl-d"):
        S.stop.set()
    elif name:
        S.picker.key(name, max(1, term_size()[1] - 2))
    S.dirty = True


def handle(ev):
    if S.picker:
        return handle_picker(ev)
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
                row = S.sel_a[0] if S.sel_a else -1
                bid = S.box_rows.get(row)
                if bid:  # a click on a file's header opens / closes its box
                    S.open_boxes.symmetric_difference_update({bid})
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
        elif S.busy:
            text = S.buf.strip()
            if text:  # redirect: run what's typed next, instead of the rest of this job
                set_buf("")
                S.menu, S.hist_i = [], None
                S.pending.insert(0, text)
                S.wake.set()
            if S.current:
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




def submit():
    text = S.buf.strip()
    set_buf("")
    S.menu, S.menu_sel, S.hist_i = [], -1, None
    if not text:
        return
    if redact(text) == text and (not S.hist or S.hist[-1] != text):   # a pasted API key never goes into the history
        S.hist.append(text)
    S.on_submit(text)
