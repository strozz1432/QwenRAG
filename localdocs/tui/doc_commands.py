"""The /write and /add commands: draft a document in the chat, then save it to disk."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from ..retrieval import MEMORY

from ..authoring import (DRAFT_RULES, gather_context, new_path, parse_write, resolve_target, split_add, tidy)
from .. import config
from ..docwriter import write_document
from ..llm import tag
from .. import activity
from ..activity import record
from ..retrieval import remember
from ..store import index, read_text
from .flow import set_progress, step, stream_reply
from .state import S, emit


def short(path) -> str:
    return str(path).replace(str(Path.home()), "~", 1)


def earlier(natural: bool) -> str:
    """The last assistant reply, so \"save that\" has something to refer to."""
    if natural and MEMORY and MEMORY[-1]["role"] == "assistant":
        return "Earlier in this conversation (use it if the request says 'that' or 'the above'):\n" + MEMORY[-1]["content"][:3000]
    return ""


async def _draft(system: str, request: str, context: str, tail: str = "", tail_label: str = "The end of the existing document, so your addition fits it") -> str:
    parts = []
    if context:
        parts.append(f"Material from the user's files:\n{context}")
    if tail:
        parts.append(f"{tail_label}:\n{tail}")
    parts.append(f"Request: {request}")
    reply = await stream_reply([{"role": "system", "content": system},
                                {"role": "user", "content": "\n\n".join(parts)}], start=0.3)
    return tidy(reply)


def refers_to_previous(request: str) -> bool:
    """'Save that as a PDF' style requests that just want the last text in another format."""
    return (len(request) < 110 and bool(re.search(r"\b(that|it|this|previous|above|last|same|reply|answer|summary|note|document)\b", request, re.I))
            and bool(re.search(r"\b(save|convert|turn|put|export|make|keep|store|copy)\b", request, re.I))
            and not re.search(r"\b(about|on|regarding|comparing|explaining)\s+\w+\s+\w+", request, re.I))


async def write_doc(arg: str, natural: bool = False, query: str | None = None):
    ext, request = parse_write(arg)
    if not request:
        emit("! Usage: /write [pdf|docx|md|txt] <what to write>   e.g. /write pdf one-page summary of my invoices\n\n")
        return
    if natural and activity.LAST_TEXT["md"] and refers_to_previous(request):
        md = activity.LAST_TEXT["md"]   # "save that as a PDF": reuse the exact text, no rewriting
        emit("· Using the text above as it is\n")
    else:
        step("Gathering material", "checking your files for relevant material", 0.1, note=None)
        context = await asyncio.to_thread(gather_context, request, 7000, query)
        context = "\n\n".join(x for x in (earlier(natural), context) if x)
        if context:
            emit("· Using the earlier chat and your files\n")
        step("Drafting", f"{tag()} · drafting the document", 0.3, note="Drafting")
        md = await _draft("You are a careful writer producing a document for the user. " + DRAFT_RULES, request, context)
    if not md:
        emit("\n! The model returned nothing, so nothing was saved.\n")
        record(f"Tried to write a {ext[1:].upper()} for '{request[:80]}' but the model returned nothing, so no file was saved", False)
        return
    path = new_path(md, request, ext)
    step("Saving", f"writing {path.name}", 0.9)
    await asyncio.to_thread(write_document, path, md)
    emit(f"\n\n✓ Saved {short(path)}\n")
    activity.LAST_TEXT["md"] = md
    S.next_hint = ("write", f"Wrote and saved {path.name} ({ext[1:].upper()}) about: {request[:100]}. It already contains:\n{md[:350]}")
    record(f"Created {short(path)} ({ext[1:].upper()}) for the request: {request[:120]}", path=path)
    remember(f"/write {request}", f"I wrote the document and saved it to {short(path)}. Its content:\n{md[:2500]}")
    await asyncio.to_thread(index, set_progress)


async def add_doc(arg: str, natural: bool = False, query: str | None = None, at_start: bool = False):
    frag, request = split_add(arg)
    if not frag or not request:
        emit('! Usage: /add <filename> <what to add>   e.g. /add notes.docx a table of the open tasks\n'
             '  (use quotes if the name has spaces: /add "my notes.docx" ...)\n\n')
        return
    step("Finding the file", f"looking for '{frag}'", 0.05, note=f"Looking for {frag}")
    path, err = await asyncio.to_thread(resolve_target, frag)
    if path is None:
        emit(f"! {err}\n\n")
        record(f"Tried to add to '{frag}' but no single matching file was found, so nothing was changed", False)
        return
    emit(f"· Found {short(path)}\n")
    step("Reading the file", f"reading {path.name}", 0.15, note="Checking what's in it already")
    existing = await asyncio.to_thread(read_text, path)
    context = await asyncio.to_thread(gather_context, request, 7000, query)
    context = "\n\n".join(x for x in (earlier(natural), context) if x)
    step("Drafting", f"{tag()} · drafting the addition", 0.3, note="Writing the addition")
    if at_start:
        system = ("You are adding something to the START of an existing document, such as a title or short intro. "
                  "Write ONLY that new text, as briefly as the request allows (a title is one line starting with '# '). "
                  "Never repeat, quote or rewrite any text that is already in the document. "
                  "If it already has what the user asks for (for example it already begins with a title), "
                  "reply with exactly the single word NOTHING. " + DRAFT_RULES)
        md = await _draft(system, request, context, tail=existing[:1200], tail_label="The start of the existing document")
    else:
        md = await _draft("You are adding a new section to an existing document. Write ONLY the new content to append; "
                          "do not repeat what is already there, and start with a '## ' heading for the new section. "
                          + DRAFT_RULES, request, context, tail=existing[-3000:])
    if not md:
        emit("\n! The model returned nothing, so the file was left untouched.\n")
        record(f"Tried to add to {short(path)} but the model returned nothing, so the file was left untouched", False, path)
        return
    def norm(t: str) -> str:
        return re.sub(r"[#*`|\s]+", " ", t).strip().lower()

    already = (norm(md).startswith(norm(md)[:0]) and norm(existing)[:len(norm(md)) + 5].startswith(norm(md))
               if at_start else norm(md) in norm(existing))
    if already or md.strip().strip(".").upper() == "NOTHING":
        emit(f"\n· {path.name} already has that, so I left it as it is\n")
        record(f"{short(path)} was NOT modified: it already had what was asked for ({request[:80]}), so I left it exactly as it was", status="NO CHANGE", path=path)
        return
    step("Saving", f"adding to {path.name}", 0.9)
    backup = await asyncio.to_thread(write_document, path, md, True, at_start)
    S.next_hint = ("add", f"Added '{request[:100]}' to {path.name}")
    record(f"Added to the {'TOP' if at_start else 'END'} of {short(path)}: {request[:120]} (original backed up at {short(backup)})", path=path)
    remember(f"/add {path.name} {request}", f"I added this to {short(path)} (original backed up):\n{md[:2500]}")
    where = "the top of " if at_start else ""
    emit(f"\n\n✓ Added to {where}{short(path)}\n· The original is backed up at {short(backup)}\n")
    await asyncio.to_thread(index, set_progress)
