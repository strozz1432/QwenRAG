# Local Docs

A private, terminal-based assistant for the documents on your computer. Ask questions in plain English, summarise files, find what's relevant, and write or extend documents. It all runs locally on [Ollama](https://ollama.com), so your files never leave your machine.

## Features

- **Ask your files.** Questions are answered from your PDFs, Word docs, Markdown and text files, using retrieval over a local vector index.
- **Plain-language routing.** You don't need commands. "Summarise my Mouser order" or "what's the VAT on the invoice?" is routed to the right action. Greetings and general questions skip the document search.
- **Summaries, skimming and multi-file reads.** Read one file or several, each shown in its own box.
- **Write and edit.** Create new PDF, DOCX, Markdown or text files, or append to existing ones. Backups are kept when you add to a file.
- **Learns your habits.** It remembers the folders, topics and answer styles you use and adjusts. `/prefs` shows what it has learned, and `/prefs clear` forgets it.
- **Terminal UI.** Markdown and table rendering, tab completion, drag-to-select with copy, a live activity log, a message queue, and `/steer` to redirect a running task.
- **Local or cloud models.** Runs on Ollama by default. Optionally connect Anthropic, OpenAI, Gemini, Groq, OpenRouter, Mistral, DeepSeek, xAI and more (or any OpenAI-compatible server) with `/connect`, then pick one with `/model`. Cloud models are marked with ☁ so you always know when text leaves your machine.
- **OCR.** Scanned PDFs and images (PNG, JPG, TIFF, BMP, WebP) are read with free, on-device OCR and become searchable like any other file.

## Quick install

```bash
git clone https://github.com/strozz1432/QwenRAG.git
cd QwenRAG
./install.sh
```

The installer (macOS and Linux) checks for `uv`, Ollama and Tesseract and offers to install whatever is missing. It then shows a menu where you choose which models to download, with sizes, and lets you pick a default. It can also add a `localdocs` command you can run from anywhere. Options: `--yes` (no questions), `--models qwen3:8b,qwen3.5:9b --default qwen3.5:9b`, `--dry-run` (show what it would do), `--no-ocr`. The manual steps are below if you prefer to do it yourself.

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
| `/connect <provider> <key> [url]` | Save an API key (checked, then stored in the Keychain) |
| `/disconnect <name>` | Remove a saved key |
| `/model <name>` | Switch the AI model (`provider:model` for cloud, e.g. `/model groq:<model>`) |
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

Local is the default and nothing is sent anywhere. To use a hosted model, connect it from inside the app:

```
/connect anthropic <your key>
/connect gemini <your key>
/connect groq <your key>
/connect                                    # shows what's connected and what's available
/model                                      # now lists the models your keys can use
/model groq:llama-3.3-70b-versatile         # provider:model
```

Built in: **Claude plan (via the `claude` command), Anthropic, OpenAI, Gemini, Groq, OpenRouter, Mistral, DeepSeek, xAI, Together, Perplexity, Fireworks, Cerebras**. Anything else that speaks the OpenAI-style chat API (LM Studio, llama.cpp, vLLM, a company gateway...) can be added under a name of your choice:

```
/connect mygateway <key> https://llm.example.com/v1
/model mygateway:some-model
```

### Use your Claude plan (like OpenClaw)

If you have Claude Code installed and logged in, you can use that login instead of an API key:

```
/connect claude-cli
/model claude-cli:sonnet        # or opus, haiku
```

Local Docs runs your own `claude` command for each request, with no tools, no project files and no saved session, so it never sees or stores your login. It bills your plan, not an API key (it ignores `ANTHROPIC_API_KEY` for these calls). Replies are slower because each one starts a process, questions and retrieved passages are sent to Anthropic, and they count against your plan's usage limits. Anthropic's rules for plan use inside other tools have changed before, so check their current terms; an API key (`/connect anthropic <key>`) is the safe alternative.

`/connect` checks the key against the provider, then stores it in the macOS Keychain (on other systems, `~/.config/localdocs/keys.json` with owner-only permissions). The key is masked on screen and never written to the project folder, the command history or the activity log. `/disconnect <name>` removes a key. Environment variables (`GROQ_API_KEY`, `GEMINI_API_KEY`, ... and `<NAME>_BASE_URL`) also work and take priority over a saved key.

With a cloud model, your questions and the passages retrieved from your files are sent to that provider, and the banner shows ☁. Embeddings and the index always stay local on Ollama. Switch back any time with `/model <local model>`.

## OCR

If Tesseract is installed, images are indexed, PDF pages with no embedded text are OCR'd, and the words inside figures on normal PDF pages are read too. When you summarise a PDF or an image with no text, a vision model (an installed Ollama model that can see, such as `qwen3.5`, or your active cloud model) also looks at the pictures, so the summary covers both the text and the figures. Descriptions are cached until the file changes. Everything runs locally. `OCR_LANGS` (e.g. `"eng+fra"`), `OCR_MAX_PAGES` and `OCR_MIN_PIXELS` in `config.py` tune it. The first scan after installing Tesseract re-reads files that previously came out empty.

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
  keys.py           API key storage (Keychain) and masking
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
