"""Chat memory, the question router, search and file summaries."""
from __future__ import annotations

import difflib
import random
import re
from pathlib import Path
from . import config
from .config import FIGURE_MAX_VISION, SUMMARY_FORMAT
from . import ocr, pdfindex
from .llm import chat, embed, tag
from .vision import describe, vision_ready
from .store import CHUNKS, db, indexed_paths, search

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


def _describe_figures(path: str, progress) -> str:
    """What the pictures in a PDF show ('' if it has none or no model can see). Cached until the file changes."""
    p = Path(path)
    mtime = p.stat().st_mtime
    row = db.execute("SELECT text FROM figdesc WHERE path=? AND mtime=?", (path, mtime)).fetchone()
    if row is not None:
        return row[0]
    try:
        figs = ocr.pdf_figures(p, FIGURE_MAX_VISION)
    except Exception:
        figs = []
    if not figs or not vision_ready():
        return ""
    lines = []
    for i, (page, img) in enumerate(figs, 1):
        if progress:
            progress(f"vision model · figure {i}/{len(figs)}", 0.1 + 0.05 * i / len(figs), "Looking at the pictures",
                     f"Looking at picture {i} of {len(figs)} (page {page})")
        try:
            seen = describe(img)
        except Exception:
            seen = ""
        if seen:
            lines.append(f"- Picture on page {page}: {seen}")
    text = "Pictures in this document, described by looking at them:\n" + "\n".join(lines) if lines else ""
    db.execute("INSERT OR REPLACE INTO figdesc VALUES (?,?,?)", (path, mtime, text))
    db.commit()
    return text


def _named_file(text: str, paths: list) -> str:
    """The indexed file name mentioned anywhere in `text` (longest first), or ''."""
    low = text.lower()
    names = sorted({Path(p).name for p in paths}, key=len, reverse=True)
    return next((n for n in names if len(n) > 3 and n.lower() in low), "")


def _summarise_picture(path: str, progress, note) -> dict:
    """A picture with no text: look at it with a vision model instead of asking the user for anything."""
    name = Path(path).name
    short = path.replace(str(Path.home()), "~", 1)
    if progress:
        progress(f"looking at {name}", 0.2, "Looking at the image", f"{note + ' ' if note else 'Found '}{short}")
        progress(f"vision model · {name}", 0.3, "Looking at the image", "It has no text, so I'm looking at the picture itself")
    try:
        seen = describe(Path(path))
    except Exception as e:
        return {"msg": f"{name} is a picture with no text, and I couldn't look at it ({e}).\n\n"}
    if not seen:
        return {"msg": f"{name} is a picture with no readable text, and none of your installed models can see images. "
                       "Install a vision model (for example `ollama pull qwen2.5vl`) or connect a cloud model and I can describe it.\n\n"}
    return {"prompt": f"Below is a description of an image file called {name}. Present it as a short, clear summary of what the "
                      f"image shows. Say it is an image. {SUMMARY_FORMAT}\n\n{seen}",
            "name": short, "note": note}


def build_summary(fragment: str, progress=None, forced_note: str | None = None, choose=None) -> dict:
    """Return {'msg': ...} on failure, else {'prompt', 'name', 'note'} ready to stream.
    `choose(entries, source) -> list[int] | None` lets the user pick which sections of a PDF with an index to summarise."""
    paths = indexed_paths()
    fragment = fragment.strip().strip("\"'")
    if fragment.startswith("~"):
        fragment = str(Path.home()) + fragment[1:]
    frag = fragment.lower()
    named = _named_file(fragment, paths)      # "wallpaper.jpg see the image" -> the file it names
    if named:
        fragment, frag = named, named.lower()
    if paths and re.search(r"\brandom\b|\byou pick\b|\bpick one\b|\bwhichever\b|\byour choice\b|^(any|some|an?|one)( \w+)? (file|doc|document)$", frag):
        picked = random.choice(paths)
        return build_summary(picked, progress, "Picked one at random:")
    matches = [p for p in paths if frag in Path(p).name.lower()]
    if not matches:  # also allow typing part of the folder path
        matches = [p for p in paths if frag in p.lower()]
    note = forced_note
    if len(matches) > 1 and len({Path(p).name for p in matches}) == 1:
        # same filename in several folders: use the most recently modified copy
        matches = [max(matches, key=lambda p: Path(p).stat().st_mtime)]
        note = "Several files share this name, using the newest:"
    if not matches and re.search(r"\.[A-Za-z0-9]{2,5}\b", fragment):   # it names a file that isn't indexed: don't substitute another one
        close = difflib.get_close_matches(Path(fragment).name, sorted({Path(p).name for p in paths}), n=3, cutoff=0.6)
        return {"msg": f"I couldn't find a file called '{Path(fragment).name}' in the folders I index."
                       + (f" Closest names: {', '.join(close)}." if close else "") + "\n\n"}
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
    if Path(path).suffix.lower() in ocr.IMAGE_EXTS and (not parts or parts[0].startswith("Image file ")):
        return _summarise_picture(path, progress, note)
    is_pdf = Path(path).suffix.lower() == ".pdf"
    sections = None                      # the ticked sections, when the user picked part of a PDF's index
    if choose and is_pdf:
        entries, source = pdfindex.get_index(Path(path))
        if len(entries) >= 2:
            if progress:
                progress("waiting for your pick", 0.1, "Waiting for your pick",
                         "This PDF has an index. Pick the sections you want summarised")
            picked = choose(entries, source)
            if picked is None:
                return {"msg": "Cancelled, nothing summarised.\n\n"}
            if source == "contents page":
                note = (note + " " if note else "") + "(index detected from the contents page)"
            if set(picked) != set(range(len(entries))):
                sections = [entries[i] for i in sorted(picked)]
    figures = _describe_figures(path, progress) if is_pdf and not sections else ""
    focus = ""
    if sections:
        size, groups, empty = config.CHUNK * 4, [], []
        for k, e in enumerate(sections):
            if progress:
                progress(f"reading {e.title}", 0.1 + 0.05 * k / len(sections), "Reading the sections", None)
            text = pdfindex.read_pages(Path(path), e.start, e.end).strip()
            if not text:                 # scanned and unreadable: never hand the model a bare heading to invent from
                empty.append(e.title)
                continue
            for j in range(0, len(text), size):
                groups.append(f"## {e.title}{' (continued)' if j else ''}\n{text[j:j + size]}")
        if not groups:
            return {"msg": f"The ticked sections ({', '.join(empty)}) have no readable text. If the PDF is scanned, "
                           "install Tesseract so I can read it, then try again.\n\n"}
        focus = "This is only some sections of a larger document. Give each section its own short heading, using the section titles. "
        if empty:
            focus += f"There was no readable text for: {', '.join(empty)}. Say so in one line for those sections instead of describing them. "
    else:
        groups = ["\n".join(parts[i:i + 4]) for i in range(0, len(parts), 4)]
    if figures:
        groups.append(figures)
    if progress:
        progress(f"reading {Path(path).name}", 0.09, "Reading the file", "Reading it")
        progress(f"{len(parts)} passages", 0.1, "Reading the file",
                 (f"Read {len(sections)} sections" if sections else f"Read it all ({len(parts)} passages)") +
                 (f", in {len(groups)} parts" if len(groups) > 1 else ""))
    if len(groups) <= 1:
        prompt = f"Summarise this document clearly and concisely. {focus}{SUMMARY_FORMAT}\n\n{groups[0] if groups else ''}"
    else:
        partials = []
        for i, g in enumerate(groups, 1):
            if progress:
                progress(f"{tag()} · section {i}/{len(groups)}", 0.1 + 0.5 * (i - 1) / len(groups), "Finding key points",
                         f"Key points, part {i} of {len(groups)}")
            partials.append(chat(f"Summarise this passage in a few sentences:\n\n{g}"))
            if progress:
                progress(f"{tag()} · finished section {i}/{len(groups)}", 0.1 + 0.5 * i / len(groups), "Finding key points")
        while len(partials) > 6:
            if progress:
                progress(f"{tag()} · merging {len(partials)} section notes", 0.6, "Combining the notes",
                         "Pulling the notes together")
            joined = ["\n".join(partials[i:i + 6]) for i in range(0, len(partials), 6)]
            partials = [chat(f"Combine these notes into one concise summary:\n\n{j}") for j in joined]
        prompt = ("Combine these notes about one document into a clear, concise summary. "
                  f"{focus}{SUMMARY_FORMAT}\n\n" + "\n".join(partials))
    return {"prompt": prompt, "name": path.replace(str(Path.home()), "~", 1), "note": note}


