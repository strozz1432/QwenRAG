"""Planning for /write and /add: parse the command, find files, gather context, pick filenames."""
from __future__ import annotations

import re
from pathlib import Path

from .config import OUTPUT_DIR
from .docwriter import WRITABLE
from .retrieval import needs_docs, retrieve
from .store import indexed_paths, read_text

FORMATS = {"docx": ".docx", "word": ".docx", "pdf": ".pdf", "md": ".md", "markdown": ".md", "txt": ".txt", "text": ".txt"}

DRAFT_RULES = (
    "Write the document in markdown. Use '# ' for the title, '## ' for sections, '- ' bullets, "
    "'1. ' numbered steps, **bold** for key terms, and markdown tables (| a | b | with a |---|---| row) "
    "whenever you compare items or list records with several attributes. "
    "Output only the document itself: no preamble, no closing remarks, no code fence around it."
)


def parse_write(arg: str) -> tuple[str, str]:
    """'/write pdf summary of X' -> ('.pdf', 'summary of X'); default format is Word."""
    first, _, rest = arg.strip().partition(" ")
    if first.lower() in FORMATS and rest.strip():
        return FORMATS[first.lower()], rest.strip()
    return ".docx", arg.strip()


def split_add(arg: str) -> tuple[str, str]:
    """'/add notes.docx a summary of X' or '/add "my notes.docx" a summary of X' -> (file fragment, request)."""
    arg = arg.strip()
    if arg[:1] in "\"'" and arg[:1]:
        end = arg.find(arg[0], 1)
        if end > 0:
            return arg[1:end], arg[end + 1:].strip()
    frag, _, rest = arg.partition(" ")
    return frag, rest.strip()


def resolve_target(fragment: str) -> tuple[Path | None, str]:
    """Find exactly one writable, indexed file for `fragment`, else explain why not."""
    frag = fragment.lower()
    paths = [p for p in indexed_paths() if Path(p).suffix.lower() in WRITABLE]
    hits = [p for p in paths if frag in Path(p).name.lower()] or [p for p in paths if frag in p.lower()]
    if not hits:
        return None, f"No .docx, .pdf, .md or .txt file matches '{fragment}'. Type /files to see what's indexed."
    exact = [p for p in hits if Path(p).name.lower() == frag]
    if len(exact) == 1:
        hits = exact
    if len(hits) > 1:
        shown = "\n".join(f"  {h}" for h in hits[:8])
        return None, f"Several files match '{fragment}', add more of the name or folder:\n{shown}"
    return Path(hits[0]), ""


def gather_context(request: str, limit: int = 7000, query: str | None = None) -> str:
    """Text the model can draw on: files named in the request, plus a search when the request needs your files."""
    parts, used = [], 0
    low = request.lower()
    for p in indexed_paths():
        name = Path(p).name
        if len(name) > 4 and name.lower() in low and used < limit:
            try:
                text = read_text(Path(p))[:limit - used]
            except Exception:
                continue
            parts.append(f"[{name}]\n{text}")
            used += len(text)
    # `query` comes from the router (already understood); None means ask the older yes/no check instead
    search = query if query is not None else (request if needs_docs(request) else "")
    if not parts and search:
        for c in retrieve(search):
            parts.append(f"[{Path(c[0]).name}]\n{c[2]}")
    return "\n\n".join(parts)


def title_of(markdown: str, request: str) -> str:
    m = re.search(r"^#\s+(.+)$", markdown, re.M)
    return re.sub(r"[*`]", "", m.group(1)).strip() if m else request


def new_path(markdown: str, request: str, ext: str) -> Path:
    slug = re.sub(r"[^\w\- ]+", "", title_of(markdown, request)).strip()[:60].strip() or "Document"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path, n = OUTPUT_DIR / f"{slug}{ext}", 2
    while path.exists():
        path = OUTPUT_DIR / f"{slug} ({n}){ext}"
        n += 1
    return path


def tidy(markdown: str) -> str:
    """Drop a stray outer code fence or <think> block the model may add."""
    md = re.sub(r"<think>.*?</think>", "", markdown, flags=re.S).strip()
    md = re.sub(r"^```(?:markdown|md)?\s*\n", "", md)
    return re.sub(r"\n```\s*$", "", md).strip()
