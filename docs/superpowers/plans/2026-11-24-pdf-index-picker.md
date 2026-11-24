# PDF Index Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Summarising a PDF with an index shows a tick-list of sections (space ticks, enter confirms); only the ticked sections are summarised.

**Architecture:** A new `localdocs/pdfindex.py` produces an index (bookmarks first, printed-Contents-page fallback) and reads page ranges. A new `localdocs/tui/picker.py` holds a modal checklist that `handle()` routes keys to and `draw()` renders. `build_summary` (runs in a worker thread) asks the picker through the event loop, then builds the prompt from the chosen pages only.

**Tech Stack:** Python 3.12, pypdf (existing), reportlab (existing, used to make test PDFs), the project's raw-mode TUI. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-24-pdf-index-picker-design.md`

## Global Constraints

- No new dependencies; no database or schema change; no re-indexing.
- Index only for `.pdf`; shown only when the index has 2 or more entries.
- A PDF with no usable index summarises whole, exactly as today.
- Fallback-detected indexes are labelled "detected from the contents page"; if page-offset calibration fails, return `[]` (never guess).
- Picker keys: up/down move, space ticks, `a` ticks or unticks all, enter confirms, esc cancels. Ticking a parent ticks its children.
- Index entries deeper than level 3 display at level 3 indentation.
- Detected indexes are cached in memory per `(path, mtime)`.
- Tests are plain `test_*` functions in `tests/test_offline.py`, run with `uv run python tests/test_offline.py`; they need no model or network.

## Review Focus

- A bookmark whose destination can't be resolved (`get_destination_page_number` raises): skip that entry, don't crash.
- Bookmarks out of page order, or two entries on the same page: sort by start page; an entry that would be empty gets `end = start`.
- Enter pressed with nothing ticked: don't confirm; show a toast "Tick at least one section".
- Terminal shorter than the list (e.g. 40 entries, 12 rows): the list scrolls and the cursor row stays visible.
- Esc or ctrl-c while the picker is open: esc cancels only the picker (summary aborted with a plain message), and does not cancel the running job in a confusing way; ctrl-c still exits.
- The model returns non-JSON or nonsense page numbers for the contents page: fallback returns `[]`.
- Encrypted or unreadable PDF: `get_index` returns `[]`.
- A scanned PDF with bookmarks: page text comes from the existing OCR path, not empty.

---

### Task 1: Bookmark index and page reading

**Files:**
- Create: `localdocs/pdfindex.py`
- Test: `tests/test_offline.py`

**Interfaces:**
- Produces: `@dataclass Entry(level: int, title: str, start: int, end: int)`, with pages 1-based and inclusive.
- Produces: `bookmark_index(path: Path) -> list[Entry]`.
- Produces: `read_pages(path: Path, start: int, end: int) -> str`.

- [ ] **Step 1: Write failing tests** in `tests/test_offline.py`. Add helper `make_pdf(path, pages: list[str], bookmarks: list[tuple[int, str, int]])` that builds a reportlab canvas with one text line per page and `addOutlineEntry(title, key, level)` per bookmark (level 0-based, page 1-based; call `bookmarkPage(key)` on that page). Tests:
  - `test_bookmark_index`: 6 pages, bookmarks Intro p1, Setup p2 (level 1), Usage p4, Appendix p6. Assert titles, levels `[0,1,0,0]`, and ranges `(1,1),(2,3),(4,5),(6,6)`. Note that a parent's range stops at the next entry of any level, which is the simple rule.
  - `test_bookmark_index_out_of_order_and_same_page`: bookmarks listed Usage p4, Intro p1, Extra p4. Assert sorted by start, and the second p4 entry has `end == 4` and its sibling `end == 4`.
  - `test_bookmark_index_none`: a PDF with no bookmarks returns `[]`; a non-PDF/garbage file returns `[]`.
  - `test_read_pages`: `read_pages(p, 2, 3)` contains page 2 and 3 markers and not page 1.
- [ ] **Step 2: Run** `uv run python tests/test_offline.py`. Expected: FAIL, `localdocs.pdfindex` missing.
- [ ] **Step 3: Implement `bookmark_index` and `read_pages` in `localdocs/pdfindex.py`.**
  - `bookmark_index` flattens `PdfReader.outline` recursively (nested lists are children, level +1). It uses `reader.get_destination_page_number(item) + 1` and skips entries that raise. It sorts by start page (stable). Each `end` is the next entry's start minus 1, clamped to `>= start`. The last entry's end is the page count. Wrap the whole function in try/except, returning `[]`.
  - `read_pages` should apply the same per-page logic as `ocr.read_pdf`. Refactor `ocr.read_pdf(path)` into `ocr.read_pdf(path, pages: range | None = None)`, defaulting to all pages and keeping behaviour identical. `read_pages` calls it with `range(start - 1, end)`. Keep the `n` used in the figure-note text equal to the real page number.
- [ ] **Step 4: Run tests.** Expected: all `ok`, including the existing `test_ocr*` tests (no regression from the refactor).
- [ ] **Step 5: Commit** `feat: read a PDF's bookmark index and page ranges`.

### Task 2: Contents-page fallback and cached `get_index`

**Files:**
- Modify: `localdocs/pdfindex.py`
- Test: `tests/test_offline.py`

**Interfaces:**
- Consumes: `bookmark_index`, `read_pages`, `Entry` (Task 1); `llm.chat(prompt: str, as_json: bool = False) -> str`.
- Produces: `contents_index(path: Path, ask=llm.chat) -> list[Entry]`. `ask` is injectable for tests.
- Produces: `get_index(path: Path) -> tuple[list[Entry], str]`, returning entries and a source of `"bookmarks"`, `"contents page"` or `""`. It is cached on `(str(path), mtime)`.

- [ ] **Step 1: Write failing tests:**
  - `test_contents_index_offset`: a PDF with no bookmarks. Page 1 is a cover. Page 2 reads "Contents / Intro .... 1 / Setup .... 3". Page 3 starts "Intro" and page 5 starts "Setup" (printed page 1 = PDF page 3, offset 2). A fake `ask` returns `[{"title":"Intro","page":1,"level":0},{"title":"Setup","page":3,"level":0}]`. Assert the entries start at 3 and 5.
  - `test_contents_index_garbage`: a fake `ask` returns "sorry" and then `[{"title":"X","page":999}]`. Both give `[]`.
  - `test_contents_index_calibration_fails`: the first title never appears as a heading in later pages, so the result is `[]`.
  - `test_get_index_prefers_bookmarks_and_caches`: with bookmarks, `get_index` returns source `"bookmarks"`. A mock on `contents_index` is not called. A second call doesn't re-read the file (patch `bookmark_index` with a counter).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement `contents_index`.**
  - Find the first page within the first 15 whose text starts with, or has a line equal to, "contents" or "table of contents" (case-insensitive).
  - Send that page's text (and the next page's text if the list looks truncated) to `ask`. The prompt requests a JSON array of `{title, page, level}` and nothing else.
  - Parse leniently: extract the first `[...]` block and `json.loads` it. Return `[]` on any failure.
  - Drop entries whose page is not an int `>= 1`.
  - Calibrate: for the first entry, scan PDF pages after the contents page for one whose first 200 characters contain the title (case-insensitive). `offset = found_page - entry.page`. Require `offset >= 0`. Apply it to all entries, and return `[]` if any shifted start exceeds the page count. Build `Entry` with ends as in Task 1.
  - `get_index` tries `bookmark_index`, then `contents_index`, and uses a module-level dict cache keyed on `(str(path), mtime)`.
- [ ] **Step 4: Run tests.** Expected: all `ok`.
- [ ] **Step 5: Commit** `feat: fall back to the printed contents page for the PDF index`.

### Task 3: Picker state, key handling and drawing

**Files:**
- Create: `localdocs/tui/picker.py`
- Modify: `localdocs/tui/state.py` (add `picker = None` to `S`), `localdocs/tui/controls.py` (route keys first in `handle()`), `localdocs/tui/render.py` (draw it in `draw()`)
- Test: `tests/test_offline.py`

**Interfaces:**
- Consumes: `pdfindex.Entry` (Task 1); `S`, `emit` (`tui/state.py`).
- Produces: `class Picker(entries: list[Entry], label: str)`.
  - Attributes: `cursor: int`, `ticked: set[int]`, `top: int`, `future: asyncio.Future`.
  - `Picker.key(name: str, rows: int) -> None` handles `up`, `down`, `pgup`, `pgdn`, `home`, `end`, `space`, `a`, `enter`, `esc`. `rows` is the visible row count; it keeps the cursor within the window by adjusting `top`.
  - `Picker.chosen() -> list[int]` returns sorted indices of ticked entries.
- Produces: `async ask(entries: list[Entry], label: str) -> list[int] | None`. It sets `S.picker`, awaits `picker.future`, then clears `S.picker`. The result is `None` on esc.
- Produces: `picker_rows(p: Picker, W: int, H: int) -> list[str]` for `draw()`.

- [ ] **Step 1: Write failing tests** (pure logic; build a `Picker` with 5 entries, levels `[0,1,1,0,1]`):
  - `test_picker_toggle_parent_ticks_children`: space on entry 0 ticks 0,1,2. Space again unticks all three. Space on a child ticks only the child.
  - `test_picker_all_and_enter`: `a` ticks all, `a` again clears. Enter with nothing ticked doesn't resolve the future and sets a toast (`S.toast` contains "Tick at least one"). Enter with entries ticked resolves the future to the sorted indices.
  - `test_picker_esc_and_scroll`: esc resolves `None`. With 40 entries and `rows=10`, pressing `down` 25 times keeps `top <= cursor < top + 10`.
  - `test_picker_rows_indent_and_marks`: `picker_rows` output contains `[x]` for ticked entries and `[ ]` for others, indents by level capped at 2 steps, and fits within `W` (use `fit`'s visible width, not raw length).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement `tui/picker.py`** as above.
  - Ticking a parent: ticking sets the entry and every following entry with a greater level until the next entry of level `<=` its own. Unticking clears the same range. Ticking a child never changes its parent.
  - Rows show `❯` on the cursor row, a title line "Pick sections to summarise · {label}" and a footer line "↑↓ move · space tick · a all · enter summarise · esc cancel".
  - Reuse `fit` and the `"sel"`, `"dim"` and `"bluebold"` styles from `render.py` / `state.py`.
- [ ] **Step 4: Wire it.**
  - In `handle()` (controls.py), at the top, after the mouse branch is skipped: if `S.picker` is set, translate `("char", " ")` to `"space"` and other `("char", c)` to `c`. Pass `("key", name)` through to `S.picker.key(name, rows)` and `return`. This must run before the esc/`S.busy` logic so esc doesn't cancel the job. ctrl-c and ctrl-d still set `S.stop`.
  - In `draw()` (render.py), when `S.picker` is set, render `picker_rows` in place of the input area, hide the cursor, and skip the completion menu.
- [ ] **Step 5: Run tests.** Expected: all `ok`. Then run the app for a manual check that the screen draws and `ctrl-c` exits (`uv run python rag.py`).
- [ ] **Step 6: Commit** `feat: modal checklist picker for the TUI`.

### Task 4: Hook the picker into summarising

**Files:**
- Modify: `localdocs/retrieval.py` (`build_summary`), `localdocs/tui/app.py` (`summarise`)
- Test: `tests/test_offline.py`

**Interfaces:**
- Consumes: `pdfindex.get_index(path) -> tuple[list[Entry], str]`, `pdfindex.read_pages(path, start, end) -> str`, `picker.ask(entries, label)`.
- Changes: `build_summary(fragment, progress=None, forced_note=None, choose=None)`. `choose` is an optional callable `(entries, source) -> list[int] | None`. When it's `None`, behaviour is unchanged.
- Produces: `build_summary` returns `{"msg": "Cancelled, nothing summarised.\n\n"}` when `choose` returns `None`.

- [ ] **Step 1: Write failing tests** (patch `pdfindex.get_index`, `pdfindex.read_pages`, `retrieval.chat` and `retrieval.CHUNKS`/`indexed_paths` as needed):
  - `test_summary_uses_only_chosen_sections`: with an index of 3 entries, `choose` returns `[1]`. The returned `prompt` contains section 1's text and its title, and not the text of sections 0 and 2.
  - `test_summary_all_ticked_is_whole_file`: `choose` returns all indices, and the prompt equals the normal whole-file path.
  - `test_summary_cancel`: `choose` returns `None`, so `msg` says "Cancelled".
  - `test_summary_no_index_unchanged`: `get_index` returns `([], "")`, and `choose` is never called.
  - `test_summary_one_entry_no_picker`: with an index of 1 entry, `choose` is never called.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement in `build_summary`.**
  - After the file is resolved and the path is a PDF (and `choose` is given), call `get_index`. If it has 2 or more entries and not every entry is chosen, build `groups` from the chosen sections only: for each, `"## {title}\n" + read_pages(path, start, end)`, split into the existing 4-chunk-style groups by character count of `config.CHUNK * 4`.
  - Sections are kept in page order. Skip figure descriptions for partial selections.
  - Reuse the existing partial-summary and merge path, with a prompt line asking for one headed section per ticked title.
  - If `source == "contents page"`, include the note "Index detected from the contents page" in the returned `note`.
- [ ] **Step 4: Implement in `summarise()` (app.py).** Pass `choose=lambda entries, source: asyncio.run_coroutine_threadsafe(picker.ask(entries, source), S.loop).result()` to the `asyncio.to_thread(build_summary, ...)` call. Emit a one-line activity note before the picker opens ("This PDF has an index, pick the sections you want").
- [ ] **Step 5: Run tests.** Expected: all `ok`.
- [ ] **Step 6: Manual check.** Make a bookmarked PDF with `make_pdf`, run `uv run python rag.py`, then `/reindex` and `summarise <file>`. Confirm the picker, a partial selection, esc, and enter with nothing ticked. State what you saw.
- [ ] **Step 7: Update README** with a short "Summarise by section" paragraph, then commit `feat: summarise only the ticked sections of a PDF`.
