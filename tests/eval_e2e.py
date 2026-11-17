"""End-to-end: plain-English requests through the real pipeline (no terminal). Run: uv run python tests/eval_e2e.py [filter]

Each scenario is a list of steps (message, checks). Checks are regexes that must match / not match the reply text.
"""
import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from localdocs import activity                       # noqa: E402
from localdocs.retrieval import MEMORY               # noqa: E402
from localdocs.store import load_store               # noqa: E402
from localdocs.tui import app, file_commands, flow  # noqa: E402
from localdocs.tui.state import S                    # noqa: E402

# files the tests create go to a throwaway folder (inside Documents so it gets indexed), deleted at the end
import shutil, tempfile                              # noqa: E402
from localdocs import authoring, docwriter           # noqa: E402
from localdocs.store import index                    # noqa: E402
TMP = Path(tempfile.mkdtemp(prefix="localdocs-test-", dir=Path.home() / "Documents"))
docwriter.OUTPUT_DIR = authoring.OUTPUT_DIR = TMP
app.PACE = flow.PACE = file_commands.PACE = 0       # no theatre in tests
# must NOT match: the failure modes we've seen
OFFER_RE = r"(?i)(want me to|should i|shall i|would you like)[^?]*\\?"
BAD = r"(?i)(I can only view|I can't save|I cannot save|! Error|Traceback)"

SCENARIOS = {
    "chat": [("hello", {"yes": r"(?i)hi|hello|hey", "no": r"Sources:|Looking"})],
    "one-random": [("look at a file, you pick which one", {"yes": r"(?i)picked|random", "no": r"(?m)^\s*/Users.*\n\s*/Users.*\n\s*/Users"})],
    "some-files": [("look at some files", {"yes": r"Picked 3 files", "count": (r"(?m)^\*\*.+\*\*  ·", 3)})],
    "five-files": [("show me 5 random documents", {"yes": r"Picked 5 files"})],
    "downloads": [("have a look at a couple of files in downloads", {"yes": r"Picked 2 files.*Downloads|Downloads"})],
    "relevant-now": [("look at a file, you pick which one", {}),
                     ("what files are relevant right now?", {"yes": r"\| File \|", "no": r"I don't have anything"})],
    "relevant-topic": [("which of my files are about invoices?", {"yes": r"(?i)invoice|order"})],
    "list": [("what files do you have?", {"yes": r"By folder", "no": r"(?m)^\s*/Users.*\n\s*/Users.*\n\s*/Users.*\n\s*/Users"})],
    "list-all": [("list all of my files", {"yes": r"/Users/sanca/Downloads/vmls.pdf"})],
    "ask-docs": [("what's the total on my cursor invoice?", {"yes": r"97\.04"})],
    "followup": [("what's the total on my cursor invoice?", {"yes": r"97\.04"}),
                 ("and when is it due?", {"yes": r"(?i)july 1|1 july"})],
    "what-is-this": [("summarise Order_39169838.pdf", {"yes": r"(?i)order|display|CHF"}),
                     ("what is this?", {"yes": r"(?i)order|display|mouser|TFT", "no": r"private, local assistant"})],
    "memory-first": [("hello", {}), ("what is the capital of france", {"yes": r"Paris"}),
                     ("what was the first thing I asked you?", {"yes": r"(?i)hello", "no": r"(?i)capital"})],
    "did-you": [("did you delete any of my files?", {"yes": r"(?i)\bno\b|did not|didn't|haven't"})],
    "write-then-pdf": [("write a markdown note with three tips for focus", {"yes": r"Saved ~/Documents/[^\n]*\.md"}),
                       ("now save that as a pdf", {"yes": r"Saved ~/Documents/[^\n]*\.pdf", "no": r"Drafting"}),
                       ("did you make a pdf?", {"yes": r"(?i)\byes\b", "no": r"I then asked"})],
    "add-title": [("write a text note with three tips for focus", {"yes": r"Saved"}),
                  ("add a title to it", {"yes": r"(?i)Added to the top|already has"}),
                  ("did you add a title?", {"yes": r"(?i)already|didn't|did not|no change|left it|not modif|\bno\b", "no": r"(?i)^\W*yes\b"})],
    "offer-after-sum": [("summarise Order_39169838.pdf", {"yes": OFFER_RE})],
    "offer-after-look": [("look at some files", {"yes": OFFER_RE})],
    "offer-after-find": [("what files are relevant right now?", {"yes": r"\| File \|"}) if False else ("which files are about invoices?", {"yes": OFFER_RE})],
    "yes-after-offer": [("summarise Order_39169838.pdf", {"yes": OFFER_RE}), ("yes", {"no": r"private, local assistant|\? *$", "yes": r"(?s).{40,}"})],
    "yes-pdf": [("write a markdown note with two tips for focus", {"yes": r"Saved"}), ("yes", {"no": r"private, local assistant"})],
    "no-thanks": [("summarise Order_39169838.pdf", {}), ("no thanks", {"no": r"Saved|Picked|Looking for", "yes": r"(?i)ok|sure|no problem|alright|anything|let me know"})],
    "topic-look": [("look at some files about invoices", {"yes": r"Picked", "no": r"! "})],
    "folder-look": [("look at two files in the trade folder", {"yes": r"Picked 2 files from", "count": (r"(?m)^\*\*.+\*\*  ·", 2)})],
    "what-else-after-full": [("read two files in the finance folder carefully", {"yes": r"Summarising ", "count": (r"What else|Want me to take", 1), "no": r"! Error"})],
    "yes-reads-all": [("look at 3 files in downloads", {"yes": r"Picked 3 files"}), ("yes", {"count": (r"Summarising ", 3), "no": r"! Error"})],
    "relevant-topic2": [("which files are about trading?", {"yes": r"\| File \|", "no": r"! "})],
    "relevant-after-work": [("summarise Order_39169838.pdf", {}), ("what files are relevant right now?", {"yes": r"Order_39169838"})],
    "second-one": [("which files are about invoices?", {"yes": r"\| File \|"}), ("summarise the first one", {"no": r"! |Nothing is indexed", "yes": r"(?s).{80,}"})],
    "unknown-file": [("summarise zzzz-not-a-file.pdf", {"no": r"Traceback|! Error"})],
    "add-missing-file": [("add a title to nosuchfile.docx", {"yes": r"(?i)no .*file|couldn't|which file", "no": r"Traceback"})],
    "how-many": [("how many files do you know about?", {"yes": r"\b\d{2,3}\b"})],
    "stats-chat": [("how many pdfs do you have?", {"yes": r"\d"})],
    "more-than-one": [("look through more than 1 file", {"yes": r"Picked [2-9] files", "count": (r"(?m)^\*\*.+\*\*  ·", 2), "no": r"! "})],
    "more-than-one-b": [("can you check more than one document", {"yes": r"Picked [2-9] files"})],
    "skim-important": [("skim my files and tell me what's important", {"yes": r"\| File \|", "no": r"! "})],
    "skim-vs-full": [("read two files carefully", {"yes": r"Picked 2 files", "count": (r"Summarising ", 2), "no": r"! Error"})],
    "unclear": [("hmm do the thing with the stuff", {"yes": r"\?", "no": r"private, local assistant|Picked|Saved"})],
    "gibberish": [("asdf qwer zxcv", {"yes": r"\?", "no": r"private, local assistant"})],
    "no-repeat": [("hello", {}), ("hello", {"no": r"private, local assistant"})],
    "help": [("what can you do?", {"yes": r"/write|/look|just say"})],
    "clear": [("start over", {"yes": r"cleared"})],
}


async def run(name, steps):
    problems = []
    MEMORY.clear()
    activity.reset()
    for msg, checks in steps:
        start = len(S.text)
        S.text += ""
        t0 = time.time()
        await app.turn(msg)
        out = S.text[start:]
        # strip the echoed user line so checks look at the reply only
        reply = out.split("\n", 2)[-1] if out.startswith("\n❯") else out
        if re.search(BAD, reply):
            problems.append(f"{msg!r}: bad phrase {re.search(BAD, reply).group(0)!r}")
        if "yes" in checks and not re.search(checks["yes"], reply, re.S):
            problems.append(f"{msg!r}: expected /{checks['yes']}/")
        if "no" in checks and re.search(checks["no"], reply, re.S):
            problems.append(f"{msg!r}: should NOT match /{checks['no']}/")
        if "count" in checks:
            pat, n = checks["count"]
            got = len(re.findall(pat, reply))
            if got < n:
                problems.append(f"{msg!r}: expected {n} file blocks, saw {got}")
        print(f"   [{time.time() - t0:4.0f}s] {msg}")
        if "-v" in sys.argv:
            print("      " + reply.strip().replace("\n", "\n      ")[:1200])
    return problems


async def main():
    flt = [a for a in sys.argv[1:] if not a.startswith("-")]
    app.lock = asyncio.Lock()
    S.stop, S.wake, S.loop = asyncio.Event(), asyncio.Event(), asyncio.get_running_loop()
    load_store()
    bad = {}
    for name, steps in SCENARIOS.items():
        if flt and not any(f in name for f in flt):
            continue
        print(f"\n== {name}")
        S.text, S.rows_key = "", None
        probs = await run(name, steps)
        if probs:
            bad[name] = probs
            for p in probs:
                print("   FAIL", p)
    shutil.rmtree(TMP, ignore_errors=True)
    await asyncio.to_thread(index)                   # drop the throwaway files from the index again
    print("\nRESULT:", "all passed" if not bad else f"{len(bad)} scenario(s) failed: {', '.join(bad)}")


asyncio.run(main())
