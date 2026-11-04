"""Picking and ranking files: "look at some files", "which files are relevant right now"."""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

from . import activity
from .llm import chat, embed
from .retrieval import MEMORY
from .store import HEADS, indexed_paths, read_text, search_scored

VAGUE = re.compile(r"^(|random|any|some|anything|whatever|whichever|you pick|a few|several|files?|docs?|documents?|them|all)$")
WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "a few": 3, "few": 3,
         "some": 3, "several": 4, "couple": 2, "both": 2}


def to_count(value: str, default: int = 3, cap: int = 8) -> int:
    v = (value or "").strip().lower()
    if v.isdigit():
        return max(1, min(int(v), cap))
    return max(1, min(WORDS.get(v, default), cap))


def short(path: str) -> str:
    return path.replace(str(Path.home()), "~", 1)


def rank_files(text: str, limit: int = 40) -> list:
    """Files most related to `text`: [(similarity, path, best snippet)], best first, one row per file."""
    seen, out = set(), []
    for score, c in search_scored(embed([text])[0], limit * 3):
        if c[0] not in seen:
            seen.add(c[0])
            out.append((score, c[0], c[2]))
        if len(out) >= limit:
            break
    return out


STOP = {"other", "others", "another", "more", "files", "file", "documents", "document", "docs", "same", "folder", "about", "related",
        "from", "with", "that", "this", "the", "and", "any", "else", "similar"}


def on_topic(topic: str, n: int = 6, exclude: str = "") -> list:
    """Files that really match `topic`: a strong similarity, or one of its key words in the name/opening. May be empty."""
    words = [w[:-1] if w.endswith("s") and len(w) > 4 else w for w in re.findall(r"[\w-]{4,}", topic.lower()) if w not in STOP]
    ranked = rank_files(" ".join(words) or topic, 40)
    heads = [h.lower() for h in HEADS.values()] or [""]
    words = [w for w in words if sum(w in h for h in heads) / len(heads) < 0.15]   # a word in every file tells us nothing
    keep = []
    for score, p, sn in ranked:
        if p == exclude:
            continue
        hay = (Path(p).name + " " + head(p, 1500)).lower()
        if score >= 0.66 or any(w in hay for w in words):
            keep.append((score, p, sn))
    return keep[:n]


def pick_files(target: str, count: int) -> tuple[list, str]:
    """Choose `count` files: random, from a folder, or about a topic. Returns (paths, how they were chosen)."""
    paths = indexed_paths()
    t = (target or "").strip().lower()
    if not paths:
        return [], ""
    if t in ("previous", "those", "these", "them", "the same", "same files", "all of them") and activity.LAST_FILES:
        return list(activity.LAST_FILES)[:8], "from before"
    if ";" in t:  # several named files: "a.pdf; b.pdf; c.md"
        chosen = []
        paths = sorted(paths, key=lambda p: p not in activity.LAST_FILES)   # a name means the file we just showed, if there is one
        for part in [x.strip() for x in t.split(";") if x.strip()]:
            hit = next((p for p in paths if part in Path(p).name.lower() and p not in chosen), None) \
                or next((p for p in paths if part in p.lower() and p not in chosen), None)
            if hit:
                chosen.append(hit)
        if chosen:
            return chosen[:8], "you named"
    if VAGUE.match(t):
        return random.sample(paths, min(count, len(paths))), "at random"
    in_folder = [p for p in paths if t in p.lower()]
    if in_folder:  # a folder or name fragment: "downloads", "TraderAI", "invoice"
        return random.sample(in_folder, min(count, len(in_folder))), f"from {target}"
    ranked = rank_files(target, count)
    return [p for _, p, _ in ranked], f"about {target}"


def brief(path: str) -> str:
    """Two plain sentences on what a file is."""
    try:
        text = read_text(Path(path))[:3000]
    except Exception:
        return "I couldn't read this one."
    if not text.strip():
        return "This file is empty or has no readable text."
    return chat("In two short plain sentences, say what this file is and what it is for. "
                "No preamble, no markdown headings.\n\nFile name: " + Path(path).name + "\n\n" + text)


def relevant_now(topic: str, n: int = 6) -> tuple[list, str]:
    """Files relevant to `topic`, or, if none is given, to what we've been talking about and working on."""
    t = (topic or "").strip()
    if t and not VAGUE.match(t.lower()) and not re.match(r"^(right now|currently|at the moment|now|today)$", t.lower()):
        other = re.match(r"(?i)^(the )?(other|others|another|more|similar)\b", t) is not None
        found = on_topic(t, n, exclude=(activity.last_file() or "") if other else "")
        subject = re.sub(r"(?i)^(the )?(other|others|another|more|similar)\s+", "", t)
        return found, f"about {subject}"
    recent = " ".join(m["content"][:400] for m in MEMORY[-4:])
    last = activity.last_file()
    seed = (recent + " " + (Path(last).name if last else "")).strip()
    if seed:
        ranked = rank_files(seed, n)
        if last and last not in [p for _, p, _ in ranked]:
            ranked.insert(0, (1.0, last, ""))
        return ranked[:n], "to what we've been working on"
    # nothing said yet: what you touched most recently
    paths = sorted(indexed_paths(), key=lambda p: Path(p).stat().st_mtime if Path(p).exists() else 0, reverse=True)
    return [(0.0, p, "") for p in paths[:n]], "(recently changed)"


def age(path: str) -> str:
    try:
        days = (time.time() - Path(path).stat().st_mtime) / 86400
    except OSError:
        return ""
    return "today" if days < 1 else f"{int(days)}d ago" if days < 60 else f"{int(days // 30)}mo ago"


def head(path: str, n: int = 600) -> str:
    """The start of a file, straight from the index (no disk, no PDF parsing)."""
    return re.sub(r"\s+", " ", HEADS.get(path, "")).strip()[:n]


def first_sentence(text: str, n: int = 140) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.{20,%d}?[.!?])(\s|$)" % n, text)
    return m.group(1) if m else (text[:n].rstrip() + ("…" if len(text) > n else ""))


def skim(paths: list) -> list:
    """One plain sentence per file from just their openings, in ONE model call (fast; not a full read)."""
    paths = list(paths)
    if not paths:
        return []
    listing = "\n\n".join(f"{i}. {Path(p).name}\n{head(p, 450) or '(no readable text)'}" for i, p in enumerate(paths, 1))
    try:
        data = json.loads(chat(
            "Below are the openings of several files. For each one write ONE short plain sentence (max 20 words) "
            "saying what the file is. Reply as JSON: {\"files\": [\"sentence for 1\", \"sentence for 2\", ...]} "
            "with exactly one entry per file, in order.\n\n" + listing, as_json=True))
        out = [str(x).strip() for x in data.get("files", [])]
        if len(out) == len(paths) and all(out):
            return out
    except Exception:
        pass
    return [first_sentence(head(p)) or "No readable text." for p in paths]
