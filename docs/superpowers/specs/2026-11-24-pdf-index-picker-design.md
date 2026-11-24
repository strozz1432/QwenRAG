# PDF index picker: summarise chosen sections

## Intent
When summarising a PDF that has an index, show the sections as a checklist. The user ticks the ones they want and the model summarises only those. This avoids a slow, diluted whole-book summary when they only care about a few parts.

## Goals
- Summarising a PDF with an index opens a checklist: up/down move, space ticks, `a` ticks or unticks all, enter confirms, esc cancels.
- Chapters are indented. Ticking a parent ticks its children.
- Enter summarises only the ticked sections, one heading per section.
- PDFs with no usable index summarise whole, exactly as today.

## Non-goals
- Restricting question answering (`answer()`) to sections.
- Changing the database or re-indexing.
- Non-PDF files.

## Anti-goals
- A wrong index silently summarising the wrong pages. Fallback-derived indexes are labelled "detected from the contents page".
- Breaking the existing completion menu or scrolling.

## Approaches considered
- **A:** bookmarks only. Reliable, but misses PDFs with only a printed contents page.
- **B (chosen):** bookmarks first, then a fallback that parses the printed Contents page.
- **C:** page numbers on every chunk. Needs a schema change and a re-index of every PDF, which is too heavy for this.

## Design
1. `localdocs/pdfindex.py`
   - `get_index(path)` returns a list of `(level, title, start_page, end_page)`, or `[]`.
   - Source 1 is `pypdf` `reader.outline`, flattened. End page is the next entry's start, or the last page for the final entry.
   - Source 2 (fallback, only if there are no bookmarks) finds a "Contents" or "Table of contents" page in the first ~15 pages and has the model return titles with printed page numbers. The page offset is calibrated by locating the first listed title's heading in the pages after the contents page. If calibration fails, return `[]` rather than guess.
   - `read_pages(path, a, b)` reuses the existing PDF text/OCR reader per page.
2. Picker UI (`tui/picker.py`)
   - A modal state on `S` (`S.picker`). `handle()` routes keys to it first; `draw()` renders it in place of the input box. It resolves an asyncio future with the chosen indices, or `None` on esc.
3. Summarise flow (`build_summary` and `summarise()`)
   - For a `.pdf` with an index of 2 or more entries, emit the picker, then build the prompt from the ticked sections' pages.
   - Long selections reuse the existing group, partial-summary and merge path.
   - If every section is ticked, behave like a normal whole-file summary.
4. Tests in `tests/test_offline.py` cover index flattening and page ranges, the offset calibration, and picker key handling. No model is needed for these.

## Calls I made (not discussed)
- The picker only appears for 2 or more index entries. An index entry deeper than 3 levels is shown but not indented further.
- A PDF whose index is detected is cached in memory per file and mtime, not in the database.
- The `a` key and esc behaviour above.

## Left to the builder
The exact row styling, and the prompt wording for parsing the contents page.
