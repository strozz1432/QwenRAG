"""Live status updates and streaming a model reply into the conversation."""
from __future__ import annotations

import asyncio
import json
import threading
import time

from ..config import LLM, NUM_CTX
from ..llm import _post
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


async def stream_reply(messages, start=0.45):
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    cancel, holder = threading.Event(), []

    def worker():
        try:
            body = {"model": LLM, "messages": messages, "stream": True, "think": False,
                    "options": {"num_ctx": NUM_CTX}}
            with _post("/api/chat", body) as r:
                holder.append(r)
                for line in r:
                    if cancel.is_set():
                        break
                    part = json.loads(line)
                    tok = part.get("message", {}).get("content", "")
                    if tok:
                        loop.call_soon_threadsafe(q.put_nowait, tok)
                    if part.get("done"):
                        break
        except Exception as e:
            if not cancel.is_set():
                loop.call_soon_threadsafe(q.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    threading.Thread(target=worker, daemon=True).start()
    first, n, full = True, 0, []
    try:
        while (tok := await q.get()) is not None:
            if isinstance(tok, Exception):
                raise tok
            if first:
                S.label, S.progress, first = "Writing the answer", None, False  # writing isn't compute: no bar
                emit("\n")
            n += 1
            S.detail = f"ollama.chat {LLM} · streaming · {n} tokens"
            full.append(tok)
            emit(tok)
        return "".join(full)
    finally:
        cancel.set()
        for r in holder:
            try:
                r.close()
            except Exception:
                pass
