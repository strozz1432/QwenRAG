"""Shared UI state, styles and small terminal helpers."""
from __future__ import annotations

import asyncio
import os
import re
import time

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
    open_boxes: set = set()   # ids of expanded per-file boxes
    box_rows: dict = {}       # row index -> box id, for clicking a header
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
    pending: list = []        # messages waiting for the current job to finish
    steer: list = []          # mid-job instructions from /steer, applied to the current reply
    next_hint = None          # (kind, detail) set by a finished action, used to offer a next step
    busy = False              # a turn is running (or about to)
    wake: asyncio.Event       # set when something is added to `pending`
    on_submit = None          # set by the app: called with the text the user entered
    loop: asyncio.AbstractEventLoop


RESET = "\x1b[0m"
BLUE = "\x1b[38;5;75m"
STY = {
    "": "",
    "boxhead": "",
    "user": "\x1b[48;5;238;38;5;255m",
    "thought": "\x1b[2;3m",
    "dim": "\x1b[38;5;245m",
    "pulse": "\x1b[38;5;245m",
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

