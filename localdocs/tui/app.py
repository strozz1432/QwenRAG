"""Conversation turns, startup and the main loop."""
from __future__ import annotations

import asyncio
import re
import codecs
import os
import shutil
import signal
import sys
import termios
import threading
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
from .. import keys
from ..keys import redact
from ..llm import CLI_MODELS, NAME_OK, chat_with, PROVIDERS, api_key, cloud_models, fetch_models, key_var, providers, display, installed_models, is_cloud, privacy_line, tag, usable
from ..config import COMMANDS, EMBED, FORMAT, OLLAMA, STEER_MARK
from ..llm import check_ollama
from ..retrieval import MEMORY, build_summary, remember, retrieve
from ..store import index, indexed_paths, load_store
from . import picker
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
             "unless asked, and never repeat anything you already said; if the request is unclear, make the most sensible assumption, say what you assumed in a few words, and answer; ask only if you truly cannot proceed. Keep replies short. " + FORMAT},
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
    step("Reading the matches", f"{tag()} · answering from {len(top)} chunks", 0.4,
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
    cancelled = threading.Event()      # set if this job is interrupted while the worker thread is still running
    try:
        info = await asyncio.to_thread(build_summary, arg, set_progress, None, picker.chooser(cancelled))
    except asyncio.CancelledError:
        cancelled.set()
        raise
    if "msg" in info:
        emit(info["msg"])
        record(f"Tried to summarise '{arg}': {info['msg'].splitlines()[0]}", False)
        return
    step("Writing the summary", f"{tag()} · writing the final summary", 0.7,
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


def connection_status() -> str:
    on = [pv for pv in providers() if api_key(pv)]
    rows = [f"  ● {pv:<12} {'via your claude command' if pv == 'claude-cli' else 'environment variable' if os.environ.get(key_var(pv), '').strip() else 'saved'}" for pv in on]
    rows += ["  (nothing connected yet)"] if not on else []
    return ("\n".join(rows) + "\n\n  Available: " + ", ".join(pv for pv in providers() if pv not in on)
            + "\n  /connect <name> <key>   ·   /connect <any-name> <key> <base url> adds your own   ·   /disconnect <name>\n")


async def connect(args: list):
    """/connect <provider> <key> [base url]: save the key, check it works, list the models it unlocks."""
    if not args:
        emit("API connections:\n" + connection_status() + "\n")
        return
    prov = args[0].lower()
    if len(args) < 2 and prov != "claude-cli":
        emit("! Usage: /connect <provider> <key>  (providers: " + ", ".join(providers()) + ")\n\n")
        return
    if prov == "claude-cli":
        return await connect_claude_cli()
    key, url = args[1], (args[2] if len(args) > 2 else "")
    if prov not in providers():           # a new, user-named OpenAI-compatible server
        if not NAME_OK.match(prov) or prov == "ollama":
            emit("! A custom provider name is 2-24 letters, digits, - or _ (and not 'ollama').\n\n")
            return
        if not url:
            emit(f"! '{prov}' isn't a built-in provider, so I need its address: /connect {prov} <key> <base url>\n"
                 f"  Built-in: {', '.join(PROVIDERS)}\n\n")
            return
        keys.add_custom(prov)
    if url:
        keys.set("url:" + prov, url)
    keys.set(prov, key)
    step("Checking the key", f"{prov} · listing available models", 0.4)
    try:
        models = await asyncio.to_thread(fetch_models, prov)
    except Exception as e:
        if "401" in str(e) or "403" in str(e):
            disconnect(prov, quiet=True)
            emit(f"! {prov} rejected that key ({e}). Nothing was saved.\n\n")
            return
        emit(f"✓ Key saved for {prov}, but I couldn't list its models ({e}). You can still type /model {prov}:<model>.\n\n")
        return
    where = "your Keychain" if sys.platform == "darwin" else str(keys.FILE)
    shown = ", ".join(models[:6]) + (f" … +{len(models) - 6} more" if len(models) > 6 else "")
    emit(f"✓ Connected to {prov}. Key stored in {where}, never in the project folder.\n  Models: {shown or 'none listed'}\n"
         f"  Pick one with /model (it will show ☁ in the banner, and text from your files is then sent to {prov}).\n\n")


async def connect_claude_cli():
    """/connect claude-cli: use your own logged-in `claude` command (and so your Claude plan) as the model."""
    if not shutil.which("claude"):
        emit("! I can't find the `claude` command. Install Claude Code, log in once with `claude`, then try again.\n\n")
        return
    step("Checking claude", "asking your claude command for a one-word reply", 0.4)
    try:
        reply = await asyncio.to_thread(chat_with, "claude-cli:haiku", "Reply with the single word: ok")
    except Exception as e:
        emit(f"! `claude` didn't answer ({e}). Run `claude` in a terminal to check you're logged in.\n\n")
        return
    keys.set("claude-cli", "on")
    emit("✓ Connected to your claude command. Models: " + ", ".join(CLI_MODELS) + "  (use /model claude-cli:sonnet)\n"
         "  Things to know:\n"
         "  - It uses your Claude plan through your own Claude Code login. Local Docs never sees or stores that login.\n"
         "  - Questions and the passages of your files they use are sent to Anthropic, and count against your plan's limits.\n"
         "  - Each reply starts a process, so it's slower than the API. Anthropic's rules for plans in other tools have changed before:\n"
         "    if in doubt, use /connect anthropic <API key> instead.\n\n")


def disconnect(prov: str, quiet: bool = False):
    if prov not in providers():
        if not quiet:
            emit("! Usage: /disconnect <provider>  (connected: " + (", ".join(p for p in providers() if api_key(p)) or "none") + ")\n\n")
        return
    keys.delete(prov)
    keys.delete("url:" + prov)
    if prov not in PROVIDERS:
        keys.remove_custom(prov)
    if quiet:
        return
    if is_cloud() and config.LLM.startswith(prov + ":"):
        prefs.set_model(config.DEFAULT_LLM)
        emit(f"✓ Removed the {prov} key and switched back to {config.DEFAULT_LLM} (on-device).\n\n")
    else:
        emit(f"✓ Removed the saved {prov} key.\n\n")


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
    emit(f"◆ Local Docs · {display()} · {privacy_line()}\n✓ Conversation cleared.\n")


def plan_from_words(d: dict, text: str):
    """Turn the router's decision into (command, argument, announcement)."""
    act = d["action"]
    if re.fullmatch(r"(it|that|this|the|same|that one|this one|the same)( (file|image|picture|photo|pic|document|doc|pdf|one))?", d["target"].lower().strip(" .?!")) and last_file():
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
        note_asked(redact(text))
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
            name = arg.strip()
            if not name:
                models = installed_models() + cloud_models()
                emit("Models (type /model and pick one; cloud ones are ☁ and need an API key):\n" + "\n".join(f"  {'●' if m == config.LLM else ' '} {m}" for m in models)
                     + "\n  Other cloud models: /model openai:<model> or /model anthropic:<model>\n\n")
                return
            problem = usable(name)
            if problem:
                emit(f"! {problem}\n\n")
                return
            prefs.set_model(name)
            if is_cloud(name):
                emit(f"✓ Now using ☁ {name}.\n! Questions and the passages of your files they use are now sent to that provider. "
                     "Type /model <local model> to go back to on-device.\n\n")
            else:
                emit(f"✓ Now using {name} (on-device).\n\n")
            return
        if cmd == "/connect":
            return await connect(arg.split())
        if cmd == "/disconnect":
            return disconnect(arg.strip().lower())
        if cmd == "/prefs":
            if arg.strip() == "clear":
                prefs.clear()
                emit("✓ Forgotten. I'll start learning again from scratch.\n\n")
            else:
                shown = prefs.describe()
                emit(shown + "\n\n")
                remember("What have you learned about me?", shown)   # so a follow-up like "what else" has something to refer to
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
        emit(f"◆ Local Docs · {display()} · {privacy_line()}\n")
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

