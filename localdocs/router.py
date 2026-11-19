"""Understands a plain-English message and decides what the user wants done, so no slash command is needed."""
from __future__ import annotations

import json
import re

from .llm import chat
from .retrieval import MEMORY, needs_docs

FIELDS = ("format", "target", "request", "query", "position", "intent", "count", "depth", "doing")
ACTIONS = {"chat", "docs", "sum", "look", "find", "recall", "unclear", "write", "add", "files", "reindex", "clear", "help"}

PROMPT = """You are the router for a private document assistant on the user's laptop. Decide what the user's latest message asks for.
Reply with ONLY a JSON object: {{"action": "...", "format": "...", "target": "...", "request": "...", "query": "...", "position": "...", "intent": "...", "count": "...", "depth": "...", "doing": "..."}}

actions:
- chat: greetings, thanks, general knowledge, opinions, or follow-ups that only work on the earlier conversation (rewrite, shorten, explain, translate, "what is this / what is that" with no file named: they refer to the previous message)
- docs: a question whose answer is in the user's own files, including "find the part about X", "is there anything about X in my files", "what does my X say about Y". These want the ANSWER, not a list of files
- sum: summarise, look at, open, read or give an overview of exactly ONE file. target = the file name or topic; if the user lets you choose ("you pick", "any file", "surprise me") target = "random"
- write: create a NEW document, report, note, letter, table or file (also "save that as a pdf / word / file"). format = docx, pdf, md or txt (docx if unspecified). request = a complete self-contained instruction for what to write
- add: append or add something to an EXISTING file. target = the file name as the user said it. request = what to add
- look: look at, open, skim or summarise SEVERAL files (plural: "some files", "a few", "3 files", "files in Downloads", or two or more named files, or "all of them / those / these files"). count = how many (a number, default 3). target = "random", a folder or name fragment, or a topic; for several NAMED files put every name separated by ";" (e.g. "a.pdf; b.md; c.pdf"); when the user means the files just shown ("these", "those", "them", "every one of these files", "all of them") target = "previous". depth = "full" when asked to read or summarise them, "skim" for a quick look
- recall: ONLY questions about this chat session itself: what the assistant did or didn't do ("did you delete anything", "did you add the title", "what did you just do") or what the user asked earlier in this chat ("what was my first question"). A question about what is written in the user's files is docs, never recall
- unclear: you genuinely cannot tell (NOT for "ok", "cool", "got it", "nice", which are chat) what they want, or the message is too vague or garbled to act on. request = ONE short friendly clarifying question that offers two concrete options
- find: asks WHICH FILES (a list of file names) are relevant / related / useful / important / worked on, "what files should I look at", "what files matter right now". request = the topic, or "right now" if none given
- files: ONLY when asked to list or show the files themselves (target = "all" if they want every one). Looking at / reading a file is sum, not files
- reindex: rescan folders for new or changed files
- clear: clear this conversation
- help: ONLY when asking what the assistant can do or which commands exist ("what can you do", "help"). Knowledge questions like "what's a pdf" or "how do I summarise a book" are chat

query: only when the answer or the document should draw on the user's files (actions docs, write, add): a short standalone search query for those files, with "it", "that" and "the same one" resolved using the recent conversation. For write/add, ALWAYS give a query when the content depends on the user's own things ("my invoices", "my notes", "the report"). Empty string otherwise.
position: for add only: "start" if the user wants it at the top/beginning/front (a title, heading, intro), otherwise "end".
intent: ALWAYS fill this in. ONE complete sentence, in plain English, saying what the user wants, with "it", "that" and "the same one" replaced by what they refer to (e.g. "The user wants to know when the Cursor invoice 7K4HZTMC-0013 is due.").
A short reply like "yes", "sure", "go ahead", "do it", "yeah" accepts the LAST thing the assistant offered in the recent conversation: choose the action that offer describes (and fill target/request/format from it). "no", "no thanks", "not now" is chat.
doing: ALWAYS fill this in for every action except chat/recall/unclear: ONE short first-person line, present tense, saying what you are about to do and where, as a person would narrate it ("Checking whether Downloads has other DigitalOcean receipts", "Pulling up the Cursor invoice"). Never start with "I" or "The user".
depth: for look/find only: "skim" (default; quick glance at the openings) or "full" when they ask to read thoroughly / in depth / carefully / fully / every detail.
Use empty strings for fields that don't apply.

Examples:
"hello" -> {{"action":"chat","format":"","target":"","request":"","query":""}}
"what does my lease say about notice?" -> {{"action":"docs","format":"","target":"","request":"","query":"lease notice period termination"}}
"give me the gist of invoice-0013.pdf" -> {{"action":"sum","format":"","target":"invoice-0013.pdf","request":"","query":""}}
"make a pdf comparing python and rust" -> {{"action":"write","format":"pdf","target":"","request":"A comparison of Python and Rust","query":"","position":""}}
"turn my invoices into a one page pdf summary" -> {{"action":"write","format":"pdf","target":"","request":"A one-page summary of the user's invoices","query":"invoices totals dates","position":""}}
"save that as a word doc" -> {{"action":"write","format":"docx","target":"","request":"Save the previous answer as a document","query":"","position":""}}
"add a table of open tasks to notes.docx" -> {{"action":"add","format":"","target":"notes.docx","request":"A table of open tasks","query":"","position":"end"}}
"add a title to notes.txt" -> {{"action":"add","format":"","target":"notes.txt","request":"A title for the document","query":"","position":"start"}}
"and when is it due?" (after talking about the Cursor invoice) -> {{"action":"docs","format":"","target":"","request":"","query":"Cursor invoice due date"}}
"look through more than one file" -> {{"action":"look","format":"","target":"random","request":"","query":"","position":"","count":"3","depth":"skim"}}
"read these two files carefully" -> {{"action":"look","format":"","target":"random","request":"","query":"","position":"","count":"2","depth":"full"}}
"hmm do the thing with the stuff" -> {{"action":"unclear","format":"","target":"","request":"Do you want me to summarise a file, or look through a few of them?","query":"","position":"","count":"","depth":"","intent":""}}
"look at some files" -> {{"action":"look","format":"","target":"random","request":"","query":"","position":"","count":"3"}}
"show me 5 random documents" -> {{"action":"look","format":"","target":"random","request":"","query":"","position":"","count":"5"}}
"have a look at a couple of files in downloads" -> {{"action":"look","format":"","target":"downloads","request":"","query":"","position":"","count":"2"}}
"yes" (after: "Want me to save this summary as a PDF?") -> {{"action":"write","format":"pdf","target":"","request":"Save the previous summary as a PDF","query":"","position":"","count":"","intent":"The user wants the previous summary saved as a PDF."}}
"sure, go ahead" (after: "Want me to summarise the top one?") -> {{"action":"sum","format":"","target":"the top file just listed","request":"","query":"","position":"","count":"","intent":"The user wants the top listed file summarised."}}
"what did I write about the sequence model" -> {{"action":"docs","format":"","target":"","request":"","query":"sequence model","position":"","count":"","depth":"","intent":"The user wants to know what their files say about the sequence model."}}
"what's a pdf" -> {{"action":"chat","format":"","target":"","request":"","query":"","position":"","count":"","depth":"","intent":"The user asks what a PDF is."}}
"what else" (right after the assistant listed what it has learned about the user) -> {{"action":"chat","format":"","target":"","request":"","query":"","position":"","count":"","depth":"","intent":"The user asks whether there is more to what was just shown."}}
"ok" -> {{"action":"chat","format":"","target":"","request":"","query":"","position":"","count":"","depth":"","intent":"The user acknowledges."}}
"which documents relate to invoices" -> {{"action":"find","format":"","target":"","request":"invoices","query":"","position":"","count":"","depth":"","intent":"The user wants the documents related to invoices."}}
"how many files do you know about" -> {{"action":"files","format":"","target":"","request":"","query":"","position":"","count":""}}
"what is this?" (after you summarised a file) -> {{"action":"chat","format":"","target":"","request":"","query":"","position":"","count":"","intent":"The user wants to know what the file just summarised is."}}
"and when is it due?" (after talking about the Cursor invoice 7K4HZTMC-0013) -> {{"action":"docs","format":"","target":"","request":"","query":"Cursor invoice 7K4HZTMC-0013 due date","position":"","count":"","intent":"The user wants to know when the Cursor invoice 7K4HZTMC-0013 is due."}}
"yes please" (after: "Should I check if there are other payment receipts from DigitalOcean in the same folder?") -> {{"action":"find","format":"","target":"","request":"other payment receipts from DigitalOcean","query":"","position":"","count":"","depth":"","intent":"The user wants any other DigitalOcean payment receipts.","doing":"Checking for other DigitalOcean payment receipts"}}
"yes" (after: "Want me to read all 3 in full? They are: 156652198.pdf, README.md, Gymap.pdf") -> {{"action":"look","format":"","target":"156652198.pdf; README.md; Gymap.pdf","request":"","query":"","position":"","count":"3","depth":"full","intent":"The user wants all three listed files read in full.","doing":"Reading all three files in full"}}
"can you explain every one of these files fully?" (after a list of files) -> {{"action":"look","format":"","target":"previous","request":"","query":"","position":"","count":"","depth":"full","intent":"The user wants each of the files just shown explained in full.","doing":"Going through each of those files in full"}}
"summarise a.pdf and b.pdf" -> {{"action":"look","format":"","target":"a.pdf; b.pdf","request":"","query":"","position":"","count":"2","depth":"full","intent":"The user wants both files summarised.","doing":"Reading both files"}}
"which files did we work on" -> {{"action":"find","format":"","target":"","request":"right now","query":"","position":"","count":""}}
"is there anything in my files about payroll" -> {{"action":"docs","format":"","target":"","request":"","query":"payroll","position":"","count":""}}
"find the part about order blocks in my plan" -> {{"action":"docs","format":"","target":"","request":"","query":"order blocks","position":"","count":""}}
"what files are relevant right now?" -> {{"action":"find","format":"","target":"","request":"right now","query":"","position":"","count":""}}
"which of my files are about trading?" -> {{"action":"find","format":"","target":"","request":"trading","query":"","position":"","count":""}}
"what is the Scarlet Letter file about" -> {{"action":"sum","format":"","target":"Scarlet Letter","request":"","query":"","position":"","count":"","depth":"","intent":"The user wants to know what the Scarlet Letter file is about."}}
"look at a file, you pick which one" -> {{"action":"sum","format":"","target":"random","request":"","query":"","position":""}}
"list every single file" -> {{"action":"files","format":"","target":"all","request":"","query":"","position":""}}
"what files do you have" -> {{"action":"files","format":"","target":"","request":"","query":""}}

Recent conversation:
{recent}

Your last offer to the user (a bare "yes"/"sure"/"go ahead" accepts THIS): {offer}

Latest message: {message}"""


def _ask(message: str, recent: str, offer: str) -> dict | None:
    try:
        data = json.loads(chat(PROMPT.format(recent=recent, message=message, offer=offer or "(none)"), as_json=True))
        action = str(data.get("action", "")).lower().strip()
        if action in ACTIONS:
            return {"action": action, **{k: str(data.get(k) or "").strip() for k in FIELDS}}
    except Exception:
        pass
    return None


SELF_QUESTION = re.compile(r"^\s*(did|have|had|were|was)\s+you\b|^\s*what\s+(did|have)\s+you\b|^\s*(why|how)\s+did\s+you\b", re.I)


PLURAL_FILES = re.compile(r"\b(more than (1|one)|multiple|several|a few|a couple( of)?|some|a bunch of|bunch of|few|many|\d+|two|three|four|five)\b"
                          r"[^.?!]*\b(files?|documents?|docs?|pdfs?|notes|papers|things)\b", re.I)
LOOK_VERB = re.compile(r"\b(look|looks|check|read|skim|browse|go through|open|show|peek|glance|scan|have a look|take a look|review)\b", re.I)
IMPORTANT = re.compile(r"\b(important|relevant|matter|matters|useful|related|relate|worth)\b", re.I)
SKIM_WORDS = re.compile(r"\b(skim|glance|quick|quickly|briefly|overview|peek|just the gist)\b", re.I)
FULL_WORDS = re.compile(r"\b(thorough|thoroughly|in depth|in-depth|carefully|fully|every detail|detailed|read all)\b", re.I)


COUNT_WORDS = {"couple": "2", "two": "2", "three": "3", "four": "4", "five": "5", "few": "3", "some": "3", "several": "4", "bunch": "5", "many": "5"}


def plural_count(message: str) -> str:
    m = re.search(r"more than (\d+|one)", message, re.I)
    if m:
        return str((int(m.group(1)) if m.group(1).isdigit() else 1) + 1)
    n = re.search(r"\b(\d+)\b", message)
    if n:
        return n.group(1)
    w = re.search(r"\b(couple|two|three|four|five|few|some|several|bunch|many)\b", message, re.I)
    return COUNT_WORDS[w.group(1).lower()] if w else "3"


def rules(message: str) -> dict | None:
    """Plain rules for phrasings the model keeps fumbling ('which files matter')."""
    if re.search(r"\b(files|documents|docs|pdfs)\b", message, re.I) and IMPORTANT.search(message) \
            and re.search(r"\b(which|what|any)\b", message, re.I) \
            and not re.search(r"\b(about|relate to|related to|relevant to|regarding|mention\w*|on|for)\s+\S+", message, re.I):
        return {"action": "find", **{k: "" for k in FIELDS}, "request": "right now", "intent": message}
    return None


def repair(d: dict, message: str) -> dict:
    """Fix what the model gets wrong about 'more than one file' without throwing away its other choices."""
    plural = bool(LOOK_VERB.search(message) and PLURAL_FILES.search(message))
    if plural and d["action"] in ("chat", "sum", "unclear", "files") and not re.search(r"(?i)\b(summari[sz]e|write|make|create|save|add|list)\b", message):
        d["action"] = "look"
        d["target"] = d["target"] or "random"
    if d["action"] == "find" and not IMPORTANT.search(message) and re.search(r"\b(\d+|two|three|four|five)\b[^.?!]*\b(files|documents|docs|pdfs|notes)\b", message, re.I):
        d["action"], d["count"], d["target"] = "look", plural_count(message), d["target"] or "random"   # "what 3 of my files are"
    if d["action"] == "find" and not IMPORTANT.search(message) and re.search(r"\b(some|a few|several|a couple)( of)?( my| the)? (files|documents|docs|pdfs|notes)\b", message, re.I):
        d["action"], d["count"], d["target"] = "look", plural_count(message), d["target"] or "random"   # "what are some of my files like"
    if d["action"] == "look" and SINGULAR_FILE.search(message) and not PLURAL_FILES.search(message) \
            and not re.search(r"\b(files|documents|docs|pdfs|notes)\b", message, re.I):
        d["action"], d["count"] = "sum", ""   # "a random file" is one file, not several
        d["target"] = d["target"] or "random"
    if d["action"] == "look":
        if re.search(r"more than (\d+|one)|\b(\d+|two|three|four|five|couple)\b", message, re.I):
            d["count"] = plural_count(message)      # a number the user actually said beats the model's guess
        elif plural and d["count"] in ("", "1"):
            d["count"] = plural_count(message)
        d["target"] = d["target"] or "random"
        d["depth"] = "full" if FULL_WORDS.search(message) else (d["depth"] if d["depth"] in ("skim", "full") else "skim")
    return d


FOLLOW_UP_EDIT = re.compile(r"^\s*(please\s+)?(can you\s+)?(make (it|that|this)|rewrite|reword|rephrase|shorten|lengthen|simplify|expand|"
                            r"translate|say (it|that)|explain (it|that|this)|try again|continue|go on|more|again|why)\b", re.I)
SINGULAR_FILE = re.compile(r"\b(a|an|one|any|that|the)\s+(random\s+|single\s+)?(file|document|doc|pdf|note)\b", re.I)


SMALL_TALK = re.compile(r"^\s*(hi+|hello+|hey+|yo|hiya|howdy|good (morning|afternoon|evening)|thanks?( you)?|thx|ty|cheers|ok(ay)?|cool|nice|great|"
                        r"got it|i see|lol|haha|who are you|how are you|what'?s up|sup)\W*(there|again|man|mate)?\W*$", re.I)


def route(message: str, offer: str = "") -> dict:
    if SMALL_TALK.match(message):   # no need to spend a model call deciding what "hello" means
        return {"action": "chat", **{k: "" for k in FIELDS}, "intent": message}
    if FOLLOW_UP_EDIT.match(message) and MEMORY:
        return {"action": "chat", **{k: "" for k in FIELDS}, "intent": message}
    hit = rules(message)
    if hit and not re.search(r"(?i)\b(write|make|create|save|add|summari[sz]e)\b", message):
        return hit
    if SELF_QUESTION.match(message) and not LOOK_VERB.search(message) \
            and not re.search(r"(?i)\b(write|make|create|add|summari[sz]e|save|find|list)\b", message):
        # "did you delete anything?" asks about what the assistant did: answer from its session log
        return {"action": "recall", **{k: "" for k in FIELDS}, "intent": message}
    recent = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in MEMORY[-4:]) or "(none)"
    d = _ask(message, recent, offer)
    if d and not d["intent"]:  # the model skipped its sentence about what the user wants: ask once more
        d = _ask(message, recent, offer) or d
    if d is None:
        return {"action": "docs" if needs_docs(message) else "chat", **{k: "" for k in FIELDS},
                "query": message, "intent": message}
    if not d["intent"]:
        d["intent"] = message
    d = repair(d, message)
    if d["query"]:  # the text we embed and search with is the model's own sentence about what the user wants
        d["query"] = f"{d['intent']} {d['query']}".strip()
    return d
