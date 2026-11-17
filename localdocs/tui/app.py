"""Conversation turns, startup and the main loop."""
from __future__ import annotations

import asyncio
import re
import codecs
import os
import signal
import sys
import termios
import time
import tty
from pathlib import Path
from .. import activity
from ..activity import asked as note_asked, context as activity_context, last_file, record, reset as reset_activity
from ..browse import WORDS
from ..followup import suggest
from .. import preferences as prefs
from ..router import FULL_WORDS, SKIM_WORDS, route
from .doc_commands import add_doc, write_doc
from .file_commands import find_relevant, look_files
from .flow import PACE, set_progress, step, stream_reply
from .. import config
from ..llm import installed_models
from ..config import COMMANDS, EMBED, FORMAT, OLLAMA, STEER_MARK
from ..llm import check_ollama
from ..retrieval import MEMORY, build_summary, remember, retrieve
from ..store import index, indexed_paths, load_store
from .controls import handle, parse
from .render import draw
from .state import S, echo, emit, fmt_secs, write







async def answer(q: str, use_docs: bool, query: str = "", intent: str = ""):
    if not use_docs:
        step("Thinking", "general chat", 0.3)
        reply = await stream_reply([
            {"role": "system", "content": "You are a friendly, concise private assistant running "
             "locally on the user's laptop. It can answer questions about their documents, summarise "
             "files, and create or add to documents (Word, PDF, Markdown, text) when they ask in plain "
             "words. Never say you can't save or write documents. Do not describe yourself or list what you can do "
             "unless asked, and never repeat anything you already said; if the request is unclear, ask ONE short question. Keep replies short. " + FORMAT},
            {"role": "system", "content": activity_context()},
            *MEMORY, {"role": "user", "content": q}], start=0.3)
        remember(q, reply)
        activity.LAST_TEXT["md"] = reply
        return
    step("Searching your files", f"ollama.embed {EMBED} → cosine search, top 5", 0.25,
         note="Looking through your files")
    # the router already turned the message into a standalone search query (follow-ups resolved)
    top = await asyncio.to_thread(retrieve, query or q)
    if not top:
        emit("Nothing is indexed yet.")
        return
    names = sorted({Path(c[0]).name for c in top})
    context = "\n\n".join(f"[{Path(c[0]).name}]\n{c[2]}" for c in top)
    step("Reading the matches", f"ollama.chat {config.LLM} · answering from {len(top)} chunks", 0.4,
         note=f"Found it in {', '.join(names[:3])}{' and more' if len(names) > 3 else ''}")
    reply = await stream_reply([{"role": "system", "content": activity_context()}, *MEMORY, {"role": "user", "content":
        "Answer the question using only the context below. If the answer is not there, say so "
        f"plainly. Do not list file names in your answer. {FORMAT}\n\n"
        f"Context:\n{context}\n\nQuestion: {q}" + (f"\n(What they are asking for: {intent})" if intent else "")}])
    remember(q, reply)
    activity.LAST_TEXT["md"] = reply
    record(f"Answered a question from your files ({', '.join(names[:3])})")
    S.next_hint = ("docs", f"Answered '{q[:100]}' using {', '.join(names[:3])}")
    emit("\n\nSources: " + ", ".join(names))


async def summarise(arg: str):
    step("Finding the file", f"looking for '{arg}'", 0.05, note=f"Looking for {arg}")
    await asyncio.sleep(PACE)
    info = await asyncio.to_thread(build_summary, arg, set_progress)
    if "msg" in info:
        emit(info["msg"])
        record(f"Tried to summarise '{arg}': {info['msg'].splitlines()[0]}", False)
        return
    step("Writing the summary", f"ollama.chat {config.LLM} · writing the final summary", 0.7,
         note="Putting the summary together")
    reply = await stream_reply([{"role": "user", "content": info["prompt"]}], start=0.7)
    remember(f"Summarise {info['name']}", reply)
    activity.LAST_TEXT["md"] = reply
    record(f"Summarised {info['name']}", path=info["name"].replace("~", str(Path.home()), 1))
    S.next_hint = ("sum", f"Summarised {info['name']}. Summary began: {reply[:300]}")


async def recall(q: str):
    """Questions about the session itself are answered from the action log only, never from chat memory."""
    step("Thinking", "checking what happened", 0.3)
    await stream_reply([
        {"role": "system", "content": activity_context() + "\n\nAnswer the user's question about this session using ONLY the "
         "two lists above. Reply in one or two natural, friendly sentences, like a person would; never copy the list lines or "
         "show timestamps. DONE means it happened. FAILED or NO CHANGE means it did NOT happen: start with 'No' and say why. "
         "If something is not in the lists, you did not do it. Do not describe yourself.\n"
         "Examples: log 'NO CHANGE: notes.txt was NOT modified, it already had a title' and the user asks 'did you add a title?' "
         "-> 'No, notes.txt already had a title, so I left it as it was.' / log 'DONE: Created ~/Documents/Report.pdf' and "
         "the user asks 'did you make a pdf?' -> 'Yes, I saved it as Report.pdf in your Documents folder.'"},
        {"role": "user", "content": q}], start=0.3)


def show_help():
    emit("\n".join(f"  {d.strip():<9} {desc}" for d, _, desc in COMMANDS) +
         "\n  You don't need the slash: just say what you want, e.g. \"summarise invoice.pdf\" or "
         "\"make a pdf about X\".\n\n")


def show_files(everything: bool = False):
    paths = indexed_paths()
    record("Listed the indexed files" + (" (all of them)" if everything else " (a grouped overview)"))
    if everything or len(paths) <= 15:
        emit("\n".join(f"  {p}" for p in paths) + "\n\n")
        remember("list my files", f"I listed all {len(paths)} indexed files.")
        return
    home = str(Path.home())
    groups: dict = {}
    for p in paths:
        parts = Path(p.replace(home, "~", 1)).parts   # ('~', 'Desktop', 'Trade', ...)
        key = "/".join(parts[:3]) if len(parts) > 3 else "/".join(parts[:-1])
        groups[key] = groups.get(key, 0) + 1
    shown = sorted(groups.items(), key=lambda kv: -kv[1])
    lines = [f"  {k:<48} {n} file{'s' if n != 1 else ''}" for k, n in shown[:14]]
    if len(shown) > 14:
        lines.append(f"  … and {len(shown) - 14} more folders")
    emit(f"I know {len(paths)} files, from your Documents, Desktop and Downloads. By folder:\n\n" + "\n".join(lines) +
         '\n\nSay "list all files" for every path, or name one to summarise it.\n\n')
    remember("list my files", f"I showed an overview of the {len(paths)} files I've indexed, grouped by folder: "
             + ", ".join(f"{k} ({n})" for k, n in shown[:8]))


def clear_chat():
    MEMORY.clear()
    reset_activity()
    S.text, S.rows_key, S.follow = "", None, True
    emit(f"◆ Local Docs · {config.LLM} · private, on-device\n✓ Conversation cleared.\n")


def plan_from_words(d: dict, text: str):
    """Turn the router's decision into (command, argument, announcement)."""
    act = d["action"]
    if d["target"].lower().strip(" .") in ("it", "that", "this", "that file", "this file", "the file", "the document", "same file") and last_file():
        d = {**d, "target": Path(last_file()).name}  # "add a title to it" -> the file we just worked on
    if act == "sum":
        t = d["target"] or d["request"] or text
        return "/sum", t, ""
    if act == "look":
        depth, said = d["depth"], bool(FULL_WORDS.search(text) or SKIM_WORDS.search(text))
        prefs.note_depth(depth, said)
        if not said and prefs.depth_default() in ("skim", "full"):
            depth = prefs.depth_default()   # what they usually ask for, when they didn't say
        return "/look", f"{d['count']}|{d['target'] or 'random'}|{depth}", ""
    if act == "recall":
        return "/recall", "", ""
    if act == "unclear":
        return "/unclear", d["request"], ""
    if act == "find":
        return "/find", d["request"] or d["target"], ""
    if act == "write":
        fmt = d["format"].lower().lstrip(".")
        said = fmt in ("docx", "pdf", "md", "txt")
        fmt = fmt if said else prefs.favorite_format()   # no format named: the one they usually pick
        prefs.note_format(fmt, said)
        return "/write", f"{fmt} {d['request'] or text}", ""
    if act == "add":
        if not d["target"]:
            return "/add", "", ""
        return "/add", f'"{d["target"]}" {d["request"] or text}', ""
    if act == "files":
        return "/files", "all" if re.search(r"(?i)\b(all|every|entire|full|complete|whole)\b", text) else "", ""
    if act == "reindex":
        return "/reindex", "", ""
    if act == "clear":
        return "/clear", "", ""
    if act == "help":
        return "/help", "", ""
    return "", "", ""


async def offer_next():
    """End the job like a person would: say what we could do next. A short 'yes' later acts on it."""
    hint, S.next_hint = S.next_hint, None
    if not hint or S.pending or S.steer:
        return
    line = await asyncio.to_thread(suggest, *hint)
    if line:
        emit(f"\n\n{line}")
        activity.OFFER["text"] = line   # not stored in the chat memory, so the model can't parrot it back


async def turn(text: str):
    echo(text)
    if not text.startswith(("/quit", "/exit")):
        note_asked(text)
    cmd, _, arg = text.partition(" ")
    natural = not cmd.startswith("/")
    if cmd in ("/quit", "/exit"):
        S.stop.set()
        return
    if not natural:
        if cmd == "/help":
            return show_help()
        if cmd == "/clear":
            return clear_chat()
        if cmd == "/model":
            name, models = arg.strip(), installed_models()
            if not name:
                emit("Installed models (type /model and pick one):\n" + "\n".join(f"  {'●' if m == config.LLM else ' '} {m}" for m in models) + "\n\n")
            elif name not in models:
                emit(f"! '{name}' isn't installed. Installed: {', '.join(models) or 'none found'}\n\n")
            else:
                prefs.set_model(name)
                emit(f"✓ Now using {name}.\n\n")
            return
        if cmd == "/prefs":
            if arg.strip() == "clear":
                prefs.clear()
                emit("✓ Forgotten. I'll start learning again from scratch.\n\n")
            else:
                emit(prefs.describe() + "\n\n")
            return
        if cmd == "/files":
            return show_files(arg.strip() == "all")
        if cmd not in ("/sum", "/reindex", "/write", "/add", "/steer", "/look", "/find"):
            emit(f"! Unknown command {cmd}. Type /help.\n\n")
            return
        if cmd == "/look":  # typed form: /look [count] [folder, name or topic]
            first, _, rest = arg.strip().partition(" ")
            arg = f"{first}|{rest.strip() or 'random'}" if (first.isdigit() or first.lower() in WORDS) else f"3|{arg.strip() or 'random'}"
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
            use_docs, query, at_start, intent = False, None, False, ""
            if natural:
                step("Thinking", "working out what you want", 0.05)
                offer = activity.take_offer()
                prefs.learn_message(text)
                d = await asyncio.to_thread(route, text, offer)
                if offer:
                    prefs.note_offer(d["action"] not in ("chat", "unclear") or False)
                cmd, arg, say = plan_from_words(d, text)
                use_docs, query = d["action"] == "docs", d["query"]
                at_start, intent = d["position"].lower() == "start", d["intent"]
                if cmd == "/add" and not arg:
                    emit("! Which file should I add that to? Say its name, e.g. \"add that to notes.docx\".\n")
                    return
                say = say or (d.get("doing", "") if d["action"] not in ("chat", "recall", "unclear") else "")
                if say:
                    emit(f"· {say}\n")
                    await asyncio.sleep(PACE)
                if cmd == "/recall":
                    await recall(text)
                    emit(f"\n\n◈ Thought for {fmt_secs(time.monotonic() - S.t0)}\n")
                    return
                if cmd == "/unclear":
                    ask = arg or "I'm not sure what you'd like. Could you say it another way, or tell me which file?"
                    emit(f"{ask}\n")
                    remember(text, ask)
                    return
                if cmd in ("/help", "/clear", "/files"):
                    show_files(arg == "all") if cmd == "/files" else {"/help": show_help, "/clear": clear_chat}[cmd]()
                    return
            if cmd == "/reindex":
                step("Scanning your folders", "walking Documents, Desktop, Downloads", None)
                n = await asyncio.to_thread(index, set_progress)
                emit(f"✓ {n} documents indexed.\n\n")
                record(f"Rescanned your folders ({n} documents indexed)")
            else:
                if cmd == "/write":
                    await write_doc(arg, natural, query)
                elif cmd == "/add":
                    await add_doc(arg, natural, query, at_start)
                elif cmd == "/sum":
                    await summarise(arg.strip())
                elif cmd == "/look":
                    await look_files(arg)
                elif cmd == "/find":
                    await find_relevant(arg)
                else:
                    await answer(text, use_docs, query or "", intent)
                await offer_next()
                emit(f"\n\n◈ Thought for {fmt_secs(time.monotonic() - S.t0)}\n")
        except asyncio.CancelledError:
            emit("\n\n! Interrupted\n")
            record(f"Was interrupted before finishing: {text[:100]}", False)
        except Exception as e:
            emit(f"\n\n! Error: {e}\n")
            record(f"Hit an error on '{text[:80]}': {e}", False)
        finally:
            S.label, S.detail, S.progress, S.current = None, "", None, None
            S.steer.clear()
            S.next_hint = None


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
        if text.split()[0] == "/steer":
            note = text[len("/steer"):].strip()
            if not note:
                emit("! Usage: /steer <what to change>, while it's working\n")
            elif S.current is None:
                emit("! Nothing is running right now, so just send your message normally.\n")
            else:
                S.steer.append(note)
                record(f"Was redirected by the user mid-reply: {note[:100]}")
                emit(f"\n{STEER_MARK} {note}\n")
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
        emit(f"◆ Local Docs · {config.LLM} · private, on-device\n")
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

