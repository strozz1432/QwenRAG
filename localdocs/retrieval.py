"""Chat memory, the question router, search and file summaries."""
from __future__ import annotations

from pathlib import Path
from .config import LLM, SUMMARY_FORMAT
from .llm import chat, embed
from .store import CHUNKS, indexed_paths, search

MEMORY: list = []  # recent chat turns as {"role", "content"}; lives in RAM only
MEMORY_MAX_MSGS, MEMORY_MAX_CHARS = 12, 6000


def remember(user: str, assistant: str):
    MEMORY.extend([{"role": "user", "content": user}, {"role": "assistant", "content": assistant}])
    del MEMORY[:-MEMORY_MAX_MSGS]
    while len(MEMORY) > 2 and sum(len(m["content"]) for m in MEMORY) > MEMORY_MAX_CHARS:
        del MEMORY[:2]


def needs_docs(message: str) -> bool:
    """Cheap router so greetings and small talk don't trigger a document search."""
    recent = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in MEMORY[-4:]) or "(none)"
    out = chat(
        "Does the latest message need facts from the user's files or documents? "
        "Greetings, thanks, small talk, general questions, and follow-ups that only work on "
        "the earlier conversation (rewrite, shorten, explain, translate what was just said) "
        "are NO. Reply with only YES or NO.\n\n"
        f"Recent conversation:\n{recent}\n\nLatest message: {message}"
    )
    return out.upper().startswith("YES")


def retrieve(question: str) -> list:
    return search(embed([question])[0], 5)


def build_summary(fragment: str, progress=None) -> dict:
    """Return {'msg': ...} on failure, else {'prompt', 'name', 'note'} ready to stream."""
    paths = indexed_paths()
    frag = fragment.lower()
    matches = [p for p in paths if frag in Path(p).name.lower()]
    if not matches:  # also allow typing part of the folder path
        matches = [p for p in paths if frag in p.lower()]
    note = None
    if len(matches) > 1 and len({Path(p).name for p in matches}) == 1:
        # same filename in several folders: use the most recently modified copy
        matches = [max(matches, key=lambda p: Path(p).stat().st_mtime)]
        note = "Several files share this name, using the newest:"
    if not matches:
        top = retrieve(fragment)
        if not top:
            return {"msg": "Nothing is indexed yet."}
        matches = [top[0][0]]
        note = f"No filename matches '{fragment}'. Closest by content:"
    if len(matches) > 1:
        return {"msg": "Several matches, be more specific:\n" + "\n".join(f"  {m}" for m in matches[:10])}
    path = matches[0]
    if progress:
        short = path.replace(str(Path.home()), "~", 1)
        progress(f"reading {Path(path).name}", 0.08, "Reading the file",
                 f"{note + ' ' if note else 'Found '}{short}")
    parts = [c[2] for c in sorted((c for c in CHUNKS if c[0] == path), key=lambda c: c[1])]
    groups = ["\n".join(parts[i:i + 4]) for i in range(0, len(parts), 4)]
    if progress:
        progress(f"reading {Path(path).name}", 0.09, "Reading the file", "Reading the document")
        progress(f"{len(parts)} passages", 0.1, "Reading the file",
                 f"Finished reading ({len(parts)} passages)" +
                 (f", going through it in {len(groups)} sections" if len(groups) > 1 else ""))
    if len(groups) <= 1:
        prompt = f"Summarise this document clearly and concisely. {SUMMARY_FORMAT}\n\n{groups[0] if groups else ''}"
    else:
        partials = []
        for i, g in enumerate(groups, 1):
            if progress:
                progress(f"ollama.chat {LLM} · section {i}/{len(groups)}", 0.1 + 0.5 * (i - 1) / len(groups), "Finding key points",
                         f"Picking out the key points, section {i} of {len(groups)}")
            partials.append(chat(f"Summarise this passage in a few sentences:\n\n{g}"))
            if progress:
                progress(f"ollama.chat {LLM} · finished section {i}/{len(groups)}", 0.1 + 0.5 * i / len(groups), "Finding key points")
        while len(partials) > 6:
            if progress:
                progress(f"ollama.chat {LLM} · merging {len(partials)} section notes", 0.6, "Combining the notes",
                         f"Combining the notes from {len(partials)} sections")
            joined = ["\n".join(partials[i:i + 6]) for i in range(0, len(partials), 6)]
            partials = [chat(f"Combine these notes into one concise summary:\n\n{j}") for j in joined]
        prompt = ("Combine these notes about one document into a clear, concise summary. "
                  f"{SUMMARY_FORMAT}\n\n" + "\n".join(partials))
    return {"prompt": prompt, "name": path.replace(str(Path.home()), "~", 1), "note": note}


