"""A modal checklist: pick sections of a PDF's index with the keyboard. Space ticks, enter confirms."""
from __future__ import annotations

import asyncio
import time

from .state import S


class Picker:
    def __init__(self, entries: list, label: str = ""):
        self.entries, self.label = entries, label
        self.cursor = self.top = 0
        self.ticked: set = set()
        self.done, self.result, self.on_done = False, None, None

    def _finish(self, value):
        self.done, self.result = True, value
        if self.on_done:
            self.on_done(value)

    def _subtree(self, i: int) -> range:
        """Entry i and the deeper entries directly after it."""
        j = i + 1
        while j < len(self.entries) and self.entries[j].level > self.entries[i].level:
            j += 1
        return range(i, j)

    def _move(self, to: int, rows: int):
        self.cursor = min(max(to, 0), len(self.entries) - 1)
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + rows:
            self.top = self.cursor - rows + 1

    def key(self, name: str, rows: int):
        if self.done:
            return
        rows = max(1, rows)
        if name in ("up", "down", "pgup", "pgdn", "home", "end"):
            step = {"up": -1, "down": 1, "pgup": -rows, "pgdn": rows}.get(name)
            self._move(self.cursor + step if step else (0 if name == "home" else len(self.entries) - 1), rows)
        elif name == "space":
            span = set(self._subtree(self.cursor))
            self.ticked = self.ticked - span if self.cursor in self.ticked else self.ticked | span
        elif name == "a":
            self.ticked = set() if len(self.ticked) == len(self.entries) else set(range(len(self.entries)))
        elif name == "enter":
            if self.ticked:
                self._finish(sorted(self.ticked))
            else:
                S.toast, S.toast_until = " Tick at least one section (space), or esc to cancel", time.monotonic() + 2.5
        elif name == "esc":
            self._finish(None)


def picker_rows(p: Picker, W: int, H: int) -> list:
    """The whole screen (H rows) for the checklist, as ready-to-write strings."""
    from .render import fit
    note = "  ·  index detected from the contents page" if p.label == "contents page" else ""
    out = [fit([("bluebold", "Pick the sections to summarise"), ("dim", note)], W)]
    room = max(1, H - 2)
    p.top = min(p.top, max(0, len(p.entries) - room))
    for i in range(p.top, min(len(p.entries), p.top + room)):
        e = p.entries[i]
        mark = "[x]" if i in p.ticked else "[ ]"
        text = f"{'❯ ' if i == p.cursor else '  '}{'  ' * min(e.level, 2)}{mark} {e.title}"
        pages = f"   p.{e.start}" + (f"-{e.end}" if e.end > e.start else "")
        out.append(fit([("sel" if i == p.cursor else "", text), ("dim", pages)], W))
    out += [""] * (H - 1 - len(out))
    hint = S.toast if time.monotonic() < S.toast_until else " ↑↓ move · space tick · a all · enter summarise · esc cancel"
    out.append(fit([("dim", hint)], W))
    return out[:H]


async def ask(entries: list, label: str):
    """Show the checklist and wait. Returns the ticked entry indexes, or None if the user cancelled."""
    p = Picker(entries, label)
    fut = asyncio.get_running_loop().create_future()
    p.on_done = lambda v: fut.done() or fut.set_result(v)
    S.picker, S.dirty = p, True
    try:
        return await fut
    finally:
        S.picker, S.dirty = None, True
