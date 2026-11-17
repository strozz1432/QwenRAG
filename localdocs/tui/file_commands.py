"""Looking at several files at once, and finding which files are relevant."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from .. import activity
from ..browse import age, brief, next_files, pick_files, relevant_now, short, skim, to_count
from ..retrieval import build_summary, remember
from .flow import PACE, set_progress, step, stream_reply
from .render import BOX_CLOSE, BOX_OPEN, BOX_SEP
from .state import S, emit


def _cell(text: str, n: int = 70) -> str:
    text = re.sub(r"\s+", " ", text).replace("|", "/").strip()
    return text if len(text) <= n else text[:n - 1] + "…"


async def look_files(arg: str):
    """arg = '<count>|<target>|<depth>': target is random, a folder/name fragment or a topic; depth is skim or full."""
    count_s, _, rest = arg.partition("|")
    target, _, depth = rest.partition("|")
    count = to_count(count_s)
    full = depth.strip().lower() == "full" or count == 1
    step("Picking files", "choosing which files to look at", 0.05)
    paths, how = await asyncio.to_thread(pick_files, target, count)
    if not paths:
        emit("! I couldn't find any files to look at. Try /reindex.\n\n")
        activity.record(f"Tried to look at files ({target or 'any'}) but none matched", False)
        return
    activity.LAST_FILES[:] = paths
    emit(f"· Picked {len(paths)} file{'s' if len(paths) != 1 else ''} {how}\n")
    await asyncio.sleep(PACE)
    summary = []
    if full and len(paths) > 1:
        # a full read of several files: one collapsible box per file, each streaming its own summary live
        for i, p in enumerate(paths, 1):
            name = Path(p).name
            bid = f"f{len(S.text)}x{i}"
            step("Reading", f"{name} ({i} of {len(paths)})", 0.05 + 0.9 * (i - 1) / len(paths))
            emit(f"\n{BOX_OPEN}{bid}{BOX_SEP}Summarising {name}  ·  {Path(short(p)).parent}\n")
            try:
                info = await asyncio.to_thread(build_summary, p, set_progress)
                if "msg" in info:
                    emit(info["msg"] + "\n")
                    summary.append(f"{name}: (could not read)")
                    continue
                step("Writing the summary", f"{name} ({i} of {len(paths)})", None, note="Putting the summary together")
                reply = await stream_reply([{"role": "user", "content": info["prompt"]}])
                summary.append(f"{name}: {reply}")
            finally:
                emit(f"\n{BOX_CLOSE}\n")
    elif full:
        for i, p in enumerate(paths, 1):
            step("Reading", f"{Path(p).name}", 0.1 + 0.85 * (i - 1) / len(paths))
            text = await asyncio.to_thread(brief, p)
            emit(f"\n**{Path(p).name}**  ·  {Path(short(p)).parent}  ·  {age(p)}\n{text}\n")
            summary.append(f"{Path(p).name}: {text}")
    else:
        step("Skimming", "reading just the openings", 0.3)
        notes = await asyncio.to_thread(skim, paths)
        for p, text in zip(paths, notes):
            emit(f"\n**{Path(p).name}**  ·  {Path(short(p)).parent}  ·  {age(p)}\n{text}\n")
            summary.append(f"{Path(p).name}: {text}")
    for p in paths:
        activity.record(f"{'Read' if full else 'Skimmed'} {short(p)}", path=p)
    names = ", ".join(Path(p).name for p in paths)
    S.next_hint = ("look", ("Read: " if full else "Skimmed (openings only): ") + names)
    if full:   # these are done: suggest something new instead of re-offering them
        S.next_hint = None
        more = await asyncio.to_thread(next_files, paths)
        if more:
            first, rest = Path(more[0]).name, [Path(p).name for p in more[1:]]
            emit(f"\n\nThat's {'all' if len(paths) > 1 else 'it'} for those. What else should I go through? "
                 f"Want me to take {first} next" + (f", or {' or '.join(rest)}?" if rest else "?"))
            activity.OFFER["text"] = f"Want me to summarise {first} next? (also related: {', '.join(rest)}) Full name: {more[0]}"
        else:
            emit("\n\nThat's all of them. What else would you like me to look at?")
    elif len(paths) > 1:   # offer the whole set, so a "yes" reads all of them
        S.next_hint = None
        activity.OFFER["text"] = f"Want me to read all {len(paths)} in full? They are: {names}"
        emit(f"\n\nWant me to read all {len(paths)} in full and summarise them?")
    remember(f"look at {target or 'some files'}", ("I read these files:\n" if full else "I skimmed these files (openings only):\n") + "\n".join(summary))


async def find_relevant(topic: str):
    step("Thinking about what matters", "ranking your files", 0.2)
    ranked, how = await asyncio.to_thread(relevant_now, topic)
    if not ranked:
        if how.startswith("about"):
            emit(f"· I looked through your indexed files and nothing matches {how[6:]!r}.\n\n"
                 "It may be named differently, or not indexed yet (/reindex rescans). Want me to search inside the files for it instead?")
            activity.record(f"Looked for files {how}, found none", False)
            S.next_hint = None
            activity.OFFER["text"] = f"Search inside the files for {how[6:]}"
        else:
            emit("I don't have anything indexed yet. Try /reindex.\n\n")
        return
    if how.startswith("about"):
        emit(f"· Found {len(ranked)} file{'s' if len(ranked) != 1 else ''} {how}\n")
        await asyncio.sleep(PACE)
    emit("· Nothing to go on yet, so these are the files you changed most recently\n\n" if how.startswith("(")
         else (f"\n" if how.startswith("about") else f"· Files relevant {how}\n\n"))
    step("Skimming", "reading just the openings", 0.6)
    notes = await asyncio.to_thread(skim, [p for _, p, _ in ranked])
    rows = ["| File | Where | Updated | What it's about |", "|---|---|---|---|"]
    for (_, p, _), note in zip(ranked, notes):
        rows.append(f"| {_cell(Path(p).name, 38)} | {_cell(str(Path(short(p)).parent), 34)} | {age(p)} | {_cell(note, 70)} |")
    emit("\n".join(rows) + "\n")
    names = ", ".join(Path(p).name for _, p, _ in ranked[:4])
    activity.LAST_FILES[:] = [p for _, p, _ in ranked[:4]]
    if len(ranked) > 1:
        S.next_hint = None
        activity.OFFER["text"] = f"Want me to read these {min(len(ranked), 4)} in full? They are: {names}"
        emit(f"\n\nWant me to read these {min(len(ranked), 4)} in full and summarise them?")
    else:
        S.next_hint = ("find", "Listed relevant files: " + names)
    remember(f"which files are relevant {how}", "The relevant files are: " + ", ".join(Path(p).name for _, p, _ in ranked))
    activity.record(f"Listed the files most relevant {how}: " + ", ".join(Path(p).name for _, p, _ in ranked[:4]))
