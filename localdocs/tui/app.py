"""Conversation turns, startup and the main loop."""
from __future__ import annotations

import asyncio
import codecs
import os
import signal
import sys
import termios
import time
import tty
from pathlib import Path
from .doc_commands import add_doc, write_doc
from .flow import PACE, set_progress, step, stream_reply
from ..config import COMMANDS, EMBED, FORMAT, LLM, OLLAMA
from ..llm import check_ollama
from ..retrieval import MEMORY, build_summary, needs_docs, remember, retrieve
from ..store import index, indexed_paths, load_store
from .controls import handle, parse
from .render import draw
from .state import S, echo, emit, fmt_secs, write







async def answer(q: str):
    step("Understanding your question", f"router: {LLM} · does this need your files?", 0.1)
    if not await asyncio.to_thread(needs_docs, q):
        step(detail="general chat", progress=0.3)
        reply = await stream_reply([
            {"role": "system", "content": "You are a friendly, concise private assistant running "
             "locally on the user's laptop. You can answer questions about their documents and "
             "summarise files with /sum. Keep replies short. " + FORMAT},
            *MEMORY, {"role": "user", "content": q}], start=0.3)
        remember(q, reply)
        return
    step("Searching your files", f"ollama.embed {EMBED} → cosine search, top 5", 0.25,
         note=f"Searching your files for \"{q if len(q) <= 50 else q[:47] + '…'}\"")
    # short follow-ups ("and the second one?") borrow the previous question for searching
    prev = next((m["content"] for m in reversed(MEMORY) if m["role"] == "user"), "")
    top = await asyncio.to_thread(retrieve, f"{prev} {q}" if prev and len(q.split()) < 8 else q)
    if not top:
        emit("Nothing is indexed yet.")
        return
    names = sorted({Path(c[0]).name for c in top})
    context = "\n\n".join(f"[{Path(c[0]).name}]\n{c[2]}" for c in top)
    step("Reading the matches", f"ollama.chat {LLM} · answering from {len(top)} chunks", 0.4,
         note=f"Found {len(top)} relevant passages in {', '.join(names)}, reading them now")
    reply = await stream_reply([*MEMORY, {"role": "user", "content":
        "Answer the question using only the context below. If the answer is not there, say so "
        f"plainly. Do not list file names in your answer. {FORMAT}\n\n"
        f"Context:\n{context}\n\nQuestion: {q}"}])
    remember(q, reply)
    emit("\n\nSources: " + ", ".join(names))


async def summarise(arg: str):
    step("Finding the file", f"looking for '{arg}'", 0.05, note=f"Looking for '{arg}' in your indexed files")
    await asyncio.sleep(PACE)
    info = await asyncio.to_thread(build_summary, arg, set_progress)
    if "msg" in info:
        emit(info["msg"])
        return
    step("Writing the summary", f"ollama.chat {LLM} · writing the final summary", 0.7,
         note="Writing the summary")
    reply = await stream_reply([{"role": "user", "content": info["prompt"]}], start=0.7)
    remember(f"Summarise {info['name']}", reply)


async def turn(text: str):
    echo(text)
    cmd, _, arg = text.partition(" ")
    if cmd in ("/quit", "/exit"):
        S.stop.set()
        return
    if cmd == "/help":
        emit("\n".join(f"  {d.strip():<9} {desc}" for d, _, desc in COMMANDS) +
             "\n  anything else: chat, or ask about your files\n\n")
        return
    if cmd == "/clear":
        MEMORY.clear()
        S.text, S.rows_key, S.follow = "", None, True
        emit(f"◆ Local Docs · {LLM} · private, on-device\n✓ Conversation cleared.\n")
        return
    if cmd == "/files":
        emit("\n".join(f"  {p}" for p in indexed_paths()) + "\n\n")
        return
    if cmd.startswith("/") and cmd not in ("/sum", "/reindex", "/write", "/add"):
        emit(f"! Unknown command {cmd}. Type /help.\n\n")
        return
    if cmd == "/sum" and not arg.strip():
        emit("! Usage: /sum <filename or topic>\n\n")
        return
    if cmd in ("/write", "/add") and not arg.strip():
        emit("! Usage: /write [pdf|docx|md|txt] <what to write>  or  /add <filename> <what to add>\n\n")
        return
    async with lock:
        S.current = asyncio.current_task()
        S.t0 = time.monotonic()
        try:
            if cmd == "/reindex":
                step("Scanning your folders", "walking Documents, Desktop, Downloads", None)
                n = await asyncio.to_thread(index, set_progress)
                emit(f"✓ {n} documents indexed.\n\n")
            else:
                if cmd == "/write":
                    await write_doc(arg)
                elif cmd == "/add":
                    await add_doc(arg)
                else:
                    await (summarise(arg.strip()) if cmd == "/sum" else answer(text))
                emit(f"\n\n◈ Thought for {fmt_secs(time.monotonic() - S.t0)}\n")
        except asyncio.CancelledError:
            emit("\n\n! Interrupted\n")
        except Exception as e:
            emit(f"\n\n! Error: {e}\n")
        finally:
            S.label, S.detail, S.progress, S.current = None, "", None, None


async def startup():
    async with lock:
        S.t0 = time.monotonic()
        await asyncio.to_thread(load_store)
        step("Scanning your folders", "walking Documents, Desktop, Downloads", None)
        try:
            await asyncio.to_thread(check_ollama)
        except Exception:
            emit(f"! Can't reach Ollama at {OLLAMA}. Start it with `ollama serve`, then type /reindex.\n")
            S.label = None
            return
        try:
            n = await asyncio.to_thread(index, set_progress)
            emit(f"✓ {n} documents indexed, all on this machine.\n")
        except Exception as e:
            emit(f"! Indexing failed: {e}\n")
        finally:
            S.label, S.detail, S.progress = None, "", None


async def worker():
    """Run submitted messages one at a time, in order."""
    while not S.stop.is_set():
        await S.wake.wait()
        while S.pending:
            S.busy = True
            S.dirty = True
            task = asyncio.ensure_future(turn(S.pending.pop(0)))
            task.add_done_callback(lambda t: (not t.cancelled() and t.exception()) and emit(f"! Error: {t.exception()}\n"))
            await asyncio.wait({task})
        S.busy = False
        S.dirty = True
        S.wake.clear()


async def render_loop():
    while not S.stop.is_set():
        if S.toast_until and time.monotonic() >= S.toast_until:
            S.toast_until, S.dirty = 0.0, True
        if S.dirty or S.label:
            draw()
        await asyncio.sleep(0.05)


async def main():
    global lock
    lock = asyncio.Lock()
    S.stop = asyncio.Event()

    S.wake = asyncio.Event()

    def on_submit(text):
        if text.split()[0] in ("/quit", "/exit"):  # never wait behind a running job
            S.stop.set()
            return
        S.pending.append(text)
        S.wake.set()

    S.on_submit = on_submit
    loop = S.loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    decoder = codecs.getincrementaldecoder("utf-8")("ignore")

    def on_stdin():
        data = os.read(fd, 65536)
        if not data:
            S.stop.set()
            return
        for ev in parse(decoder.decode(data)):
            handle(ev)
        S.dirty = True

    try:
        tty.setraw(fd)
        # alt screen, mouse (wheel + click), bracketed paste
        write("\x1b[?1049h\x1b[?1002h\x1b[?1006h\x1b[?2004h")
        loop.add_reader(fd, on_stdin)
        loop.add_signal_handler(signal.SIGWINCH, lambda: setattr(S, "dirty", True))
        emit(f"◆ Local Docs · {LLM} · private, on-device\n")
        asyncio.ensure_future(startup())
        render = asyncio.ensure_future(render_loop())
        asyncio.ensure_future(worker())
        await S.stop.wait()
        render.cancel()
    finally:
        loop.remove_reader(fd)
        write("\x1b[?2004l\x1b[?1006l\x1b[?1002l\x1b[?25h\x1b[?1049l")
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    asyncio.run(main())

