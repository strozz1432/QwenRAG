"""A plain record of what the assistant has actually done this session.

It is given to the model on every reply, so "did you save that?" is answered from facts, not guesses.
"""
from __future__ import annotations

import time

LOG: list = []                 # (clock time, what happened, succeeded)
ASKED: list = []               # every message the user sent this session, in order
LAST = {"path": None}
OFFER = {"text": ""}           # the last "want me to...?" we put to the user; a bare "yes" accepts it
LAST_TEXT = {"md": ""}         # the last document or answer text, so "save that as a PDF" can reuse it exactly          # the file most recently read, written or summarised: what "it" means
LAST_FILES: list = []          # the files most recently listed or looked at: what "these" / "those" / "them" mean
MAX_ENTRIES = 25


def record(what: str, ok: bool = True, path=None, status: str | None = None):
    LOG.append((time.strftime("%H:%M"), what, status or ("DONE" if ok else "FAILED")))
    del LOG[:-MAX_ENTRIES]
    if path:
        LAST["path"] = str(path)
        from . import preferences
        preferences.note_folder(path)


def asked(text: str):
    ASKED.append((time.strftime("%H:%M"), text.strip()[:200]))
    del ASKED[:-40]


def last_file() -> str | None:
    return LAST["path"]


def take_offer() -> str:
    text, OFFER["text"] = OFFER["text"], ""
    return text


def reset():
    LAST_FILES.clear()
    OFFER["text"] = ""
    LAST_TEXT["md"] = ""
    ASKED.clear()
    LOG.clear()
    LAST["path"] = None


def _known() -> str:
    from . import preferences
    s = preferences.summary()
    return f"\n\n{s} Let this shape your tone and suggestions quietly; never recite it back unless asked." if s else ""


def context() -> str:
    """System-prompt text describing the session's real actions."""
    rows = "\n".join(f"- {t} {st}: {w}" for t, w, st in LOG) or "- (nothing yet)"
    said = "\n".join(f"{i}. {t} {w}" for i, (t, w) in enumerate(ASKED, 1)) or "(nothing yet)"
    return ("Everything the user has asked this session, oldest first (answer 'what did I ask first / before / earlier' "
            "from this list, exactly):\n" + said + "\n\n"
            "Things you have actually done in this session, oldest first. This list is the truth: "
            "if the user asks whether you did something and it is not listed, say plainly that you did not; "
            "if it is listed, confirm it and give the details (file, place, what changed). "
            "Do not claim to have done anything that is not here.\n" + rows + _known())
