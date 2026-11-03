"""Settings, prompts and the command list."""
from __future__ import annotations

from pathlib import Path

LLM = "qwen3:8b"
EMBED = "qwen3-embedding:0.6b"
OLLAMA = "http://localhost:11434"
ROOTS = [Path.home() / d for d in ("Documents", "Desktop", "Downloads")]
EXTS = {".pdf", ".docx", ".txt", ".md"}
SKIP_DIRS = {".venv", "node_modules", ".git", "site-packages", "chroma_db", "Library"}
MAX_BYTES = 30 * 1024 * 1024
CHUNK, OVERLAP = 2500, 300  # characters
NUM_CTX = 8192
OUTPUT_DIR = Path.home() / "Documents" / "Local Docs"  # where /write puts new files (and /add backups)
DB_PATH = Path(__file__).resolve().parent.parent / "index.db"
SPINNER = "◜◠◝◞◡◟"
LONG_AFTER = 20  # seconds before the detailed status (stages, bar) appears
SHOW_PROGRESS = True  # set False to hide the progress bar
BAR_WIDTH = 8
FORMAT = ("Format the reply for a terminal so it is easy to scan: short paragraphs separated by blank lines; "
          "'- ' bullet lists for several points and '1. ' lists for steps; **bold** on key terms, names and numbers; "
          "short '## ' headings only when the answer is long. Use a markdown table (with a |---| separator row) when comparing items or listing records with several attributes. Never write one long block of text, "
          "and never wrap the reply in a code block.")
SUMMARY_FORMAT = ("Structure it like this: a one-sentence overview in **bold**, a blank line, then the key points as "
                  "'- ' bullets with **bold** lead-ins, grouped under short '## ' headings if there are several topics. "
                  "Use a table only if the document is clearly tabular. Never wrap the reply in a code block.")

COMMANDS = [
    ("/sum", "/sum ", "summarise a file (filename or topic)"),
    ("/write", "/write ", "create a doc: /write [pdf|docx|md|txt] <what to write>"),
    ("/add", "/add ", "add to a file: /add <filename> <what to add>"),
    ("/files", "/files", "list every indexed file"),
    ("/reindex", "/reindex", "rescan your folders for new or changed files"),
    ("/clear", "/clear", "forget this conversation and clear the screen"),
    ("/help", "/help", "show these commands"),
    ("/quit", "/quit", "exit"),
]

