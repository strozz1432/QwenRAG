"""What the assistant picks up about the user over time, kept in preferences.json next to the index.

It learns from use (never asks): the document format they usually want, how deep they like to go,
where they keep working, what they keep asking about, and anything they say outright
("I prefer short answers", "always use PDF"). It is shown to the model so replies and suggestions fit them.
Everything stays on this machine; /prefs shows it and /prefs clear wipes it.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import config
from .config import DB_PATH

PATH = Path(DB_PATH).with_name("preferences.json")
STOP = set("summarise summarize explain write wrote files tell the and for with that this from have what which about file files document documents please could would can you "
           "show look read tell give make into your them they there their then than just some more also want need".split())
DATA: dict = {"formats": {}, "depth": {}, "folders": {}, "topics": {}, "offers": {"yes": 0, "no": 0}, "notes": []}
STATED = re.compile(r"(?i)\b(i (always |usually |really )?(prefer|like|want|need|love|hate)|always|never|from now on|"
                    r"by default|please (always|never)|don'?t ever|stop (using|doing|adding))\b")


def load():
    try:
        DATA.update(json.loads(PATH.read_text()))
        config.LLM = DATA.get("model") or config.LLM
    except Exception:
        pass


def save():
    try:
        PATH.write_text(json.dumps(DATA, indent=1))
    except OSError:
        pass


def _bump(group: str, key: str, n: int = 1):
    if key:
        DATA[group][key] = DATA[group].get(key, 0) + n


def _top(group: str, minimum: int = 2):
    items = Counter(DATA[group]).most_common(1)
    return items[0][0] if items and items[0][1] >= minimum else ""


def learn_message(text: str):
    """Called for every natural-language message."""
    t = text.strip()
    for w in re.findall(r"[a-zA-Z]{5,}", t.lower()):
        if w not in STOP:
            _bump("topics", w)
    if len(DATA["topics"]) > 80:   # keep it small: drop the one-offs
        DATA["topics"] = {k: v for k, v in DATA["topics"].items() if v > 1}
    if STATED.search(t) and 8 < len(t) < 200 and not t.endswith("?") and t not in DATA["notes"]:
        DATA["notes"] = (DATA["notes"] + [t])[-10:]   # something the user said about how they want things
    save()


def set_model(name: str):
    config.LLM = DATA["model"] = name
    save()


def note_format(fmt: str, explicit: bool):
    _bump("formats", fmt, 2 if explicit else 0)
    save()


def note_depth(depth: str, explicit: bool):
    if explicit:
        _bump("depth", depth)
        save()


def note_folder(path):
    p = Path(path).parent
    _bump("folders", str(p).replace(str(Path.home()), "~", 1))
    save()


def note_offer(accepted: bool):
    DATA["offers"]["yes" if accepted else "no"] += 1
    save()


def favorite_format() -> str:
    return _top("formats") or "docx"


def depth_default() -> str:
    return _top("depth", 3)


def summary() -> str:
    """A short block for the model's system prompt; empty until there is something real to say."""
    bits = []
    if _top("formats"):
        bits.append(f"usually wants documents as {_top('formats').upper()}")
    if _top("depth", 3):
        bits.append(f"usually prefers {'thorough reads' if _top('depth', 3) == 'full' else 'quick skims'} of files")
    folders = [f for f, n in Counter(DATA["folders"]).most_common(2) if n >= 2]
    if folders:
        bits.append("often works with files in " + " and ".join(folders))
    topics = [t for t, n in Counter(DATA["topics"]).most_common(5) if n >= 3]
    if topics:
        bits.append("keeps coming back to: " + ", ".join(topics))
    o = DATA["offers"]
    if o["yes"] + o["no"] >= 4:
        bits.append("often takes your suggestions" if o["yes"] >= o["no"] else "rarely takes your suggestions, so offer less")
    out = ""
    if bits:
        out += "What you have learned about this user from using it together: " + "; ".join(bits) + ". "
    if DATA["notes"]:
        out += "Things they have told you they want (follow these): " + " | ".join(DATA["notes"]) + ". "
    return out.strip()


def describe() -> str:
    s = summary()
    return s if s else "I haven't picked up anything about your habits yet. I learn as you use me."


def clear():
    DATA.update({"formats": {}, "depth": {}, "folders": {}, "topics": {}, "offers": {"yes": 0, "no": 0}, "notes": []})
    save()


load()
