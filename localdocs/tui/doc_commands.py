"""The /write and /add commands: draft a document in the chat, then save it to disk."""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..authoring import (DRAFT_RULES, gather_context, new_path, parse_write, resolve_target, split_add, tidy)
from ..config import LLM
from ..docwriter import write_document
from ..store import index, read_text
from .flow import set_progress, step, stream_reply
from .state import emit


def short(path) -> str:
    return str(path).replace(str(Path.home()), "~", 1)


async def _draft(system: str, request: str, context: str, tail: str = "") -> str:
    parts = []
    if context:
        parts.append(f"Material from the user's files:\n{context}")
    if tail:
        parts.append(f"The end of the existing document, so your addition fits it:\n{tail}")
    parts.append(f"Request: {request}")
    reply = await stream_reply([{"role": "system", "content": system},
                                {"role": "user", "content": "\n\n".join(parts)}], start=0.3)
    return tidy(reply)


async def write_doc(arg: str):
    ext, request = parse_write(arg)
    if not request:
        emit("! Usage: /write [pdf|docx|md|txt] <what to write>   e.g. /write pdf one-page summary of my invoices\n\n")
        return
    step("Gathering material", "checking your files for relevant material", 0.1,
         note=f"Planning a {ext[1:].upper()} document")
    context = await asyncio.to_thread(gather_context, request)
    if context:
        emit("· Using material from your files\n")
    step("Drafting", f"ollama.chat {LLM} · drafting the document", 0.3, note="Drafting it now")
    md = await _draft("You are a careful writer producing a document for the user. " + DRAFT_RULES, request, context)
    if not md:
        emit("\n! The model returned nothing, so nothing was saved.\n")
        return
    path = new_path(md, request, ext)
    step("Saving", f"writing {path.name}", 0.9)
    await asyncio.to_thread(write_document, path, md)
    emit(f"\n\n✓ Saved {short(path)}\n")
    await asyncio.to_thread(index, set_progress)


async def add_doc(arg: str):
    frag, request = split_add(arg)
    if not frag or not request:
        emit('! Usage: /add <filename> <what to add>   e.g. /add notes.docx a table of the open tasks\n'
             '  (use quotes if the name has spaces: /add "my notes.docx" ...)\n\n')
        return
    step("Finding the file", f"looking for '{frag}'", 0.05, note=f"Looking for '{frag}' in your indexed files")
    path, err = await asyncio.to_thread(resolve_target, frag)
    if path is None:
        emit(f"! {err}\n\n")
        return
    emit(f"· Found {short(path)}\n")
    step("Reading the file", f"reading {path.name}", 0.15, note="Reading what's already in it")
    existing = await asyncio.to_thread(read_text, path)
    context = await asyncio.to_thread(gather_context, request)
    step("Drafting", f"ollama.chat {LLM} · drafting the addition", 0.3, note="Drafting the addition")
    md = await _draft("You are adding a new section to an existing document. Write ONLY the new content to append; "
                      "do not repeat what is already there, and start with a '## ' heading for the new section. "
                      + DRAFT_RULES, request, context, tail=existing[-3000:])
    if not md:
        emit("\n! The model returned nothing, so the file was left untouched.\n")
        return
    step("Saving", f"adding to {path.name}", 0.9)
    backup = await asyncio.to_thread(write_document, path, md, True)
    emit(f"\n\n✓ Added to {short(path)}\n· The original is backed up at {short(backup)}\n")
    await asyncio.to_thread(index, set_progress)
