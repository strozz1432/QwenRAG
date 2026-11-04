"""After an action, offer the natural next step in one friendly sentence, the way a person would."""
from __future__ import annotations

import re

from .llm import chat

CAN_DO = ("summarise one file, look at several files, find which files are relevant, answer questions from the files, "
          "save something as a PDF, Word, Markdown or text file, add a section or a title to an existing file")

FALLBACK = {
    "look": "Want me to go deeper on any of these, or look at a few more?",
    "find": "Want me to summarise the top one?",
    "sum": "Want me to save this summary as a PDF, or look at another file?",
    "docs": "Want me to pull up the full file this came from?",
    "write": "Want me to add anything to it, or save a copy in another format?",
    "add": "Want me to add anything else to it?",
    "files": "Want me to look at one of them?",
}


def suggest(kind: str, detail: str) -> str:
    """One short question offering a next step the assistant can really do."""
    try:
        out = chat(
            "You are a warm, capable assistant that has just finished a task for the user.\n"
            f"What you just did: {kind}. {detail[:600]}\n"
            f"Things you can do next: {CAN_DO}.\n"
            "Do not offer anything that was already done or is already included in what you just did. "
            "Write ONE short, natural question (under 18 words) offering the single most useful next step, "
            "the way a helpful colleague would. Start with 'Want me to' or 'Should I' or 'Shall I'. "
            "No commands, no markdown, no quotes, nothing else.")
        out = re.sub(r"[\"*`]", "", out.strip().split("\n")[0]).strip()
        if 8 < len(out) <= 140 and out.endswith("?") and re.match(r"(?i)(want me|should i|shall i|would you like)", out):
            return out
    except Exception:
        pass
    return FALLBACK.get(kind, "")
