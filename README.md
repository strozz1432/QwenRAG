# Local Docs

A private, terminal-based assistant for the documents on your computer. Ask questions in plain English, summarise files, find what's relevant, and write or extend documents. It all runs locally on [Ollama](https://ollama.com), so your files never leave your machine.

## Features

- **Ask your files.** Questions are answered from your PDFs, Word docs, Markdown and text files, using retrieval over a local vector index.
- **Plain-language routing.** You don't need commands. "Summarise my Mouser order" or "what's the VAT on the invoice?" is routed to the right action. Greetings and general questions skip the document search.
- **Summaries, skimming and multi-file reads.** Read one file or several, each shown in its own box.
- **Write and edit.** Create new PDF, DOCX, Markdown or text files, or append to existing ones. Backups are kept when you add to a file.
- **Learns your habits.** It remembers the folders, topics and answer styles you use and adjusts. `/prefs` shows what it has learned, and `/prefs clear` forgets it.
- **Terminal UI.** Markdown and table rendering, tab completion, drag-to-select with copy, a live activity log, a message queue, and `/steer` to redirect a running task.
- **Local or cloud models.** Runs on Ollama by default. Optionally switch to Anthropic, OpenAI or any OpenAI-compatible server with `/model`. Cloud models are marked with ☁ so you always know when text leaves your machine.
- **OCR.** Scanned PDFs and images (PNG, JPG, TIFF, BMP, WebP) are read with free, on-device OCR and become searchable like any other file.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com) running locally on `http://localhost:11434`
- macOS or Linux (a terminal with Unicode support)
- Optional: [Tesseract](https://github.com/tesseract-ocr/tesseract) for OCR (`brew install tesseract` or `apt install tesseract-ocr`). Without it, images and scanned pages are skipped and everything else works.

## Setup

```bash
# 1. Pull the models
ollama pull qwen3:8b
ollama pull qwen3-embedding:0.6b

# 2. Clone and run
git clone https://github.com/strozz1432/QwenRAG.git
cd QwenRAG
uv run rag.py
```

On first launch it scans `~/Documents`, `~/Desktop` and `~/Downloads` and builds `index.db`. Later launches only re-index new or changed files. Run `/reindex` to rescan manually.

## Usage

Type naturally, or use a command:

| Command | What it does |
|---|---|
| `/sum <file or topic>` | Summarise a file |
| `/look [count] [folder or topic]` | Look at several files |
| `/find <topic>` | Find the files relevant to a topic |
| `/write [pdf\|docx\|md\|txt] <what to write>` | Create a new document |
| `/add <filename> <what to add>` | Add to an existing file |
| `/steer <instruction>` | Add an instruction while it's working |
| `/files` | List every indexed file |
| `/reindex` | Rescan your folders |
| `/model <name>` | Switch the AI model (`/model anthropic:<model>`, `/model openai:<model>` for cloud) |
| `/prefs` | Show what it has learned about you (`/prefs clear` resets it) |
| `/clear` | Forget this conversation and clear the screen |
| `/help` | Show the commands |
| `/quit` | Exit |

Example prompts:

```
what's the total on my cursor invoice?
summarise the gymap documentation
find the files about payroll
write a docx with a one-page project plan for the new website
look at 3 random files in Downloads
```

## Using a cloud model (optional)

Local is the default and nothing is sent anywhere. To use a hosted model, export a key before starting:

```bash
export ANTHROPIC_API_KEY=...        # then: /model anthropic:claude-sonnet-5-5
export OPENAI_API_KEY=...           # then: /model openai:<model>
export OPENAI_BASE_URL=http://localhost:1234/v1   # optional: LM Studio, llama.cpp, Groq, OpenRouter, vLLM...
```

Keys are read from the environment and never written to disk. With a cloud model, your questions and the passages retrieved from your files are sent to that provider. Embeddings and the index always stay local on Ollama. Switch back any time with `/model <local model>`.

## OCR

If Tesseract is installed, images are indexed and PDF pages with no embedded text are OCR'd. Everything runs locally. `OCR_LANGS` (e.g. `"eng+fra"`), `OCR_MAX_PAGES` and `OCR_MIN_PIXELS` in `config.py` tune it. The first scan after installing Tesseract re-reads files that previously came out empty.

## Configuration

Settings live in `localdocs/config.py`:

| Setting | Default | Purpose |
|---|---|---|
| `LLM` | `qwen3:8b` | Chat model |
| `EMBED` | `qwen3-embedding:0.6b` | Embedding model |
| `ROOTS` | Documents, Desktop, Downloads | Folders to index |
| `EXTS` | `.pdf .docx .txt .md` | File types to index |
| `CHUNK` / `OVERLAP` | 2500 / 300 | Chunk size and overlap, in characters |
| `MAX_BYTES` | 30 MB | Skip files larger than this |
| `OUTPUT_DIR` | `~/Documents/Local Docs` | Where `/write` saves files |

Your preferences are stored in `preferences.json`. Both it and the index (`index.db`) are git-ignored.

## How it works

1. **Index.** Files are extracted to text, split into overlapping chunks, embedded with Ollama, and stored in SQLite (`index.db`).
2. **Route.** A small LLM call classifies each message: chat, ask the docs, summarise, find, write, add and so on.
3. **Retrieve.** For document questions, the query is embedded and compared against the in-memory chunk vectors by cosine similarity. The top chunks go to the chat model as context.
4. **Answer.** The reply is streamed back and rendered as Markdown in the terminal.

## Project layout

```
rag.py              launcher
localdocs/
  config.py         settings, prompts, command list
  store.py          SQLite vector store, scanning, indexing
  retrieval.py      chat memory, search, summaries
  router.py         plain-language intent routing
  browse.py         multi-file reads and skim mode
  authoring.py      writing and editing documents
  docwriter.py      PDF / DOCX / MD / TXT output
  preferences.py    learned user preferences
  followup.py       follow-up offers
  llm.py            model providers: Ollama, Anthropic, OpenAI-compatible
  ocr.py            on-device OCR for images and scanned PDFs (Tesseract)
  markdown.py       terminal markdown rendering
  tui/              terminal UI (app, input, rendering, commands)
tests/
  eval_router.py    does the router understand plain English?
  eval_e2e.py       end-to-end scenarios through the real pipeline
  test_offline.py   providers and OCR, no models or network needed
```

## Tests

`test_offline.py` needs nothing running. The two evals call the real models, so Ollama must be running:

```bash
uv run python tests/test_offline.py
uv run python tests/eval_router.py [filter]
uv run python tests/eval_e2e.py [filter]
```

## Privacy

Everything runs on your machine by default. The only network traffic is to your local Ollama server, unless you opt into a cloud model (see above). The index, your preferences and the chat memory (RAM only) stay local.

## License

MIT. See [LICENSE](LICENSE).
