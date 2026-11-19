"""Live status updates and streaming a model reply into the conversation."""
from __future__ import annotations

import asyncio
import threading
import time

from ..llm import stream_chat, tag
from .state import S, emit

PACE = 0.7  # seconds between activity lines


def step(label=None, detail="", progress=None, note=None):
    """Update the live status line; `note` (if given) is written into the conversation as an activity line."""
    if label:
        S.label = label
    S.detail, S.progress = detail, progress
    if note:
        emit(f"· {note}\n")


def set_progress(detail: str, frac: float, label: str | None = None, note: str | None = None):
    """Thread-safe status update; `note` becomes an activity line in the conversation."""
    if label:
        S.label = label
    S.detail, S.progress = detail, frac
    if note:
        S.loop.call_soon_threadsafe(emit, f"· {note}\n")
        time.sleep(PACE)  # let each stage be seen before the next one appears


def looping(text: str) -> bool:
    """True when the end of the text is a chunk that has already appeared several times (a degenerate loop)."""
    tail = text[-120:]
    return len(text) > 600 and len(tail.strip()) > 40 and text.count(tail) >= 3


def with_steer(messages: list, steer: list) -> list:
    """Append the user's mid-job instructions to the last user message."""
    if not steer:
        return messages
    out = [dict(m) for m in messages]
    for m in reversed(out):
        if m["role"] == "user":
            m["content"] += ("\n\nUPDATED INSTRUCTION (replaces or adjusts the request above; follow every part of it, "
                            "including any format or length it asks for): " + " / ".join(steer) +
                            "\nStart your reply directly with the content. Do not mention this update or that the request changed.")
            break
    return out


async def stream_reply(messages, start=0.45):
    """Stream a reply into the chat. If the user /steers meanwhile, stop and restart with their note included."""
    while True:
        applied = len(S.steer)
        reply, redirected = await _stream_once(with_steer(messages, S.steer[:applied]), applied)
        if not redirected:
            return reply
        S.label, S.progress, S.detail = "Adjusting", None, "taking your note into account"
        emit("\n\n· Reworking it with your note\n")


async def _stream_once(messages, applied: int):
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    cancel, holder = threading.Event(), []

    def worker():
        try:
            for tok in stream_chat(messages, holder):
                if cancel.is_set():
                    break
                loop.call_soon_threadsafe(q.put_nowait, tok)
        except Exception as e:
            if not cancel.is_set():
                loop.call_soon_threadsafe(q.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    threading.Thread(target=worker, daemon=True).start()
    first, n, full = True, 0, []
    try:
        while True:
            try:
                tok = await asyncio.wait_for(q.get(), 0.25)
            except asyncio.TimeoutError:
                if len(S.steer) > applied:
                    return "".join(full), True
                continue
            if tok is None:
                return "".join(full), False
            if len(S.steer) > applied:
                return "".join(full), True
            if isinstance(tok, Exception):
                raise tok
            if first:
                S.label, S.progress, first = "Writing the answer", None, False  # writing isn't compute: no bar
                emit("\n")
            n += 1
            S.detail = f"{tag()} · streaming · {n} tokens"
            full.append(tok)
            emit(tok)
            if n % 150 == 0 and looping("".join(full)):  # the model got stuck repeating itself: stop it
                emit("\n\n· I started repeating myself, so I stopped there\n")
                return "".join(full), False
    finally:
        cancel.set()
        for r in holder:
            try:
                r.close()
            except Exception:
                pass
