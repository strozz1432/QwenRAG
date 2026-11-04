"""Does the router understand plain-English requests? Run: uv run python tests/eval_router.py [filter]"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from localdocs.retrieval import MEMORY, remember   # noqa: E402
from localdocs.router import route                  # noqa: E402

# (message, expected action, extra checks as {field: substring that must appear (lowercase)})
CASES = [
    # chat
    ("hello", "chat"), ("hey, how are you?", "chat"), ("thanks!", "chat"), ("explain recursion simply", "chat"),
    ("what's the capital of france", "chat"), ("who are you?", "chat"), ("what can you do?", "help"),
    ("tell me a joke", "chat"),  ("translate that to french", "chat"),
    # ask the files
    ("what's the total on my cursor invoice?", "docs"), ("what does the gymap documentation say about auth?", "docs"),
    ("when is my order from mouser arriving", "docs"), ("how much is the VAT on the invoice", "docs"),
    ("what did I write about the sequence model", "docs"), ("find the part about order blocks in my plan", "docs"),
    ("is there anything in my files about payroll", "docs"),
    # one file
    ("summarise Order_39169838.pdf", "sum", {"target": "order_39169838"}), ("give me the gist of the cursor invoice", "sum"),
    ("tldr of vmls.pdf", "sum", {"target": "vmls"}), ("what's in the PRIVACY.md file", "sum"),
    ("look at a file, you pick which one", "sum", {"target": "random"}), ("open any file and tell me what it is", "sum", {"target": "random"}),
    ("summarise a random file", "sum", {"target": "random"}), ("pick a document and summarise it", "sum", {"target": "random"}),
    # several files
    ("look at some files", "look"), ("look at a few files", "look"), ("show me 5 random documents", "look", {"count": "5"}),
    ("have a look at a couple of files in downloads", "look", {"target": "download"}), ("skim three of my pdfs", "look", {"count": "3"}),
    ("check out some of my notes", "look"), ("what are a few of the files in desktop about", "look"),
    ("summarise all the files in the Trade folder", "look", {"target": "trade"}),
    # relevant files
    ("what files are relevant right now?", "find"), ("which files matter right now", "find"),
    ("which of my files are about trading?", "find", {"request": "trading"}), ("what should I be looking at", "find"),
    ("are there any documents related to hardware", "find"), ("what files are relevant to the gym app", "find"),
    ("which files did we work on", "find"),
    # list files
    ("what files do you have?", "files"), ("list all of my files", "files", {"target": "all"}),
    ("show me everything you've indexed", "files"), ("how many files do you know about", "files"),
    # write / add
    ("write a one page pdf on why sleep matters", "write", {"format": "pdf"}), ("make a word doc comparing python and rust", "write", {"format": "docx"}),
    ("create a markdown note with my packing list", "write", {"format": "md"}), ("draft a cover letter as a pdf", "write", {"format": "pdf"}),
    ("turn my invoices into a pdf table", "write", {"format": "pdf"}),
    ("add a section on risks to the plan.md", "add"), ("add a title to notes.txt", "add", {"position": "start"}),
    ("put a heading at the top of my todo.md", "add", {"position": "start"}), ("append a summary table to report.docx", "add"),

    # ---- many more file phrasings
    ("check out a few documents", "look"), ("take a look at 4 files", "look", {"count": "4"}), ("skim a few of my pdfs", "look"),
    ("browse some files for me", "look"), ("show me a couple of random notes", "look", {"count": "2"}), ("open two files", "look", {"count": "2"}),
    ("give me an overview of several files", "look"), ("look through my downloads folder", "look", {"target": "download"}),
    ("peek at a few things on my desktop", "look", {"target": "desktop"}), ("read a few documents and tell me what they are", "look"),
    ("look at some of the trade files", "look", {"target": "trade"}), ("tell me what 3 of my files are", "look", {"count": "3"}),
    ("surprise me with a few files", "look"), ("what are some of my files like", "look"),
    ("show me some pdfs", "look"), ("what's in a handful of my documents", "look"),
    ("what should I look at next", "find"), ("which files are important", "find"), ("which documents relate to invoices", "find", {"request": "invoice"}),
    ("show me files related to hardware", "find"), ("which files mention payroll", "find"), ("what have I been working on recently", "find"),
    ("what's relevant to what we're talking about", "find"), ("what files are relevant to the pcb", "find"),
    ("any files I should know about right now", "find"),
    ("summarise vmls.pdf", "sum"), ("what is the Scarlet Letter file about", "sum"), ("give me a quick summary of the feynman paper", "sum"),
    ("tl;dr the gymap documentation", "sum"), ("what does the research paper in TestENV say overall", "sum"),
    ("summarise the newest file", "sum"), ("pick any pdf and summarise it", "sum", {"target": "random"}),
    ("list my files", "files"), ("show all indexed files", "files", {}), ("what do you have indexed", "files"), ("what's in your index", "files"),
    ("can you look at this joke: why did the chicken cross the road", "chat"), ("what's a pdf", "chat"), ("how do I summarise a book", "chat"),
    ("did you delete anything", "recall"), ("have you changed any of my files", "recall"), ("what did you just do", "recall"),
    ("what was my first question", "recall"), ("what is this?", "chat"), ("explain that", "chat"), ("ok", "chat"), ("no thanks", "chat"),
    ("how much is the shipping on the mouser order", "docs"), ("who is the employee with the highest salary in the register", "docs"),
    ("what dates are mentioned in the oxbridge handbook", "docs"), ("what does the feynman paper say about quantum simulation", "docs"),
    ("what's the plan for the labeling model", "docs"),
    ("make me a table of my invoices as a word doc", "write", {"format": "docx"}), ("write a txt file with a to do list", "write", {"format": "txt"}),
    ("create a summary of the gym docs in a pdf", "write", {"format": "pdf"}), ("put together a cheat sheet on eigenvectors", "write"),
    ("add a conclusion to the research paper md", "add"), ("add a new heading at the start of README.md", "add", {"position": "start"}),
    ("append the date to notes.txt", "add"),
    ("look through more than 1 file", "look", {"count": "2"}), ("can you check more than one document", "look", {"count": "2"}),
    ("look at more than 2 files", "look", {"count": "3"}), ("a couple of files in downloads please", "look", {"target": "download", "count": "2"}),
    ("look at two files in the trade folder", "look", {"target": "trade", "count": "2"}),
    ("read three files thoroughly", "look", {"count": "3", "depth": "full"}), ("skim a few files quickly", "look", {"depth": "skim"}),
    ("look through some files in the patrick folder", "look", {"target": "patrick"}),
    ("which files are important?", "find"), ("skim my files and tell me what's important", "find"),
    # housekeeping
    ("rescan my folders", "reindex"), ("reindex", "reindex"), ("check for new files", "reindex"),
    ("start over", "clear"), ("clear the chat", "clear"), ("forget everything", "clear"), ("help", "help"),
]

# follow-ups that depend on the conversation
FOLLOWUPS = [
    ([("what's on my cursor invoice?", "The Cursor invoice 7K4HZTMC-0013 totals $97.04.")], "and when is it due?", "docs", {"intent": "invoice"}),
    ([("summarise vmls.pdf", "VMLS is a long linear algebra textbook covering vectors, matrices and least squares.")], "make it shorter", "chat", {}),
    ([("summarise Order_39169838.pdf", "It is a Mouser order for a TFT display.")], "what is this?", "chat", {}),
    ([("summarise vmls.pdf", "VMLS is a linear algebra textbook.")], "save that as a pdf", "write", {"format": "pdf"}),
    ([("write a note about sleep", "Saved Sleep Tips.md")], "add a title to it", "add", {"position": "start"}),
    ([("look at some files", "I looked at a.md, b.pdf")], "tell me more about the second one", "sum", {}),
    ([("summarise vmls.pdf", "VMLS is a linear algebra textbook.\n\nWant me to save this summary as a PDF?")], "yes", "write", {"format": "pdf"}),
    ([("look at some files", "I looked at a.md, b.pdf, c.txt.\n\nWant me to go deeper on any of these?")], "sure, the second one", "sum", {}),
    ([("which files are relevant", "The relevant files are: plan.md, notes.txt.\n\nWant me to summarise the top one?")], "yes please", "sum", {}),
    ([("write a note about focus", "Saved Focus.md\n\nWant me to save a copy as a PDF?")], "go ahead", "write", {"format": "pdf"}),
    ([("summarise vmls.pdf", "It is a textbook.\n\nWant me to save this summary as a PDF?")], "no thanks", "chat", {}),
]


def check(d, action, extra):
    errs = []
    if d["action"] != action:
        errs.append(f"action {d['action']!r} != {action!r}")
    for k, want in (extra or {}).items():
        if want.lower() not in d.get(k, "").lower():
            errs.append(f"{k}={d.get(k)!r} lacks {want!r}")
    return errs


def main():
    flt = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    fails, total, t0 = [], 0, time.time()
    for case in CASES:
        msg, action, *rest = case
        if flt and flt not in msg.lower() and flt != action:
            continue
        MEMORY.clear()
        d = route(msg)
        total += 1
        errs = check(d, action, rest[0] if rest else None)
        if errs:
            fails.append((msg, errs, d))
    for history, msg, action, extra in FOLLOWUPS:
        if flt and flt not in msg.lower():
            continue
        MEMORY.clear()
        offer = ""
        for u, a in history:
            body, _, last = a.rpartition("\n\n")
            if re.match(r"(?i)(want me|should i|shall i)", last):   # the offer travels separately, like in the app
                a, offer = body, last
            remember(u, a)
        d = route(msg, offer)
        total += 1
        errs = check(d, action, extra)
        if errs:
            fails.append((f"{msg}   [after: {history[-1][0]}]", errs, d))
    print(f"\n{total - len(fails)}/{total} passed in {time.time() - t0:.0f}s")
    for msg, errs, d in fails:
        print(f"  FAIL {msg!r}: {'; '.join(errs)}   -> {d['action']} target={d['target']!r} count={d['count']!r} req={d['request'][:40]!r}")


if __name__ == "__main__":
    main()
