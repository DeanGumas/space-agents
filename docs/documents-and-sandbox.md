# Documents, the Python sandbox, and proposed changes

How agents read files, run code, and change files safely. Design rationale is in PLAN.md §2.7–§2.10.

## Tools the model sees

| Tool | What it does | Code |
|---|---|---|
| `list_files(folder?, pattern?)` | Recursive listing of granted folders | `tools/registry.py` |
| `read_document(path, pages?)` | Text of PDF/DOCX/XLSX/PPTX/text, a page range at a time | `documents/` |
| `search_document(path, query)` | BM25 search over a document's pages (SQLite FTS5) | `documents/library.py` |
| `run_python(description, code, input_files?, output_folder?)` | Python in the Pyodide sandbox; outputs become proposed changes | `sandbox/`, `changes.py` |

Every path goes through `FolderAccess` (`tools/access.py`):
- **Resolution:** `realpath` + containment in a granted folder.
- **Deny-list:** `.ssh`, keychains, `*.pem`, and the app's own data folder.
- **Writes:** only into folders granted *read & write*.

Errors come back to the model as tool results, never crashes. The user sees a plain-language step for each call
("Read PO Tracker.xlsx"), and "Show details" reveals arguments, code, and results.

## Reading documents (local, no OCR)

`documents/extract.py` turns a file into pages:

| Format | How it's read | Pages |
|---|---|---|
| PDF | `pypdfium2` | Real pages, each classified `text` / `no_text` (scanned) / `garbled` |
| DOCX | stdlib `zipfile` + XML | Headings → `#`, tables → Markdown, chunked ~3k chars |
| XLSX | stdlib, streaming | Markdown tables, 100 rows per page; date-formatted cells shown as ISO dates |
| PPTX | stdlib | One page per slide |
| Text-like files | stdlib | Chunked ~3k chars |

`documents/library.py` caches pages per file *content* (sha256) in SQLite with an FTS5 index:
- A file is extracted once per version.
- A renamed copy is recognized by hash.
- Reads are instant afterwards.

Extraction runs on one dedicated thread, because pypdfium2 isn't thread-safe. Moving it to a separate worker process
is planned hardening. Measured: ~460 text pages/s.

## The Pyodide sandbox

```
registry.run_python ──► PyodideSandbox.run(code, sources, aliases)       space_agents/sandbox/pyodide.py
                          copies inputs to data/workspaces/<token>/in/, mints a one-run token
                        ──► run_js("window.__sandbox.run(job)")            ui/js/sandbox.js (main thread)
                        ──► Web Worker                                       ui/sandbox/worker.js
                              GET  /workspace/<token>/in/<name>            space_agents/server.py
                              runs code in Pyodide (in-memory FS, cwd /work)
                              PUT  /workspace/<token>/out/<name>
                        ◄── call("sandbox_done", run_id, result)           bridge.sandbox_done → future resolves
```

### Isolation
- **Disk:** Pyodide has an in-memory filesystem; the host disk isn't visible.
- **Network:** blocked by the worker's own CSP header (`connect-src 'self'`, sent by `server.py`). WebKit leaves a
  blocked `fetch` pending rather than rejecting it, so it simply never returns.
- **Bridge:** unreachable from a worker. Messages from the worker are validated by the main thread.
- **Runaway code:** a timeout terminates the worker, which is respawned on next use.
- **Packages:** only allow-listed ones can load (trimmed `ui/vendor/pyodide/pyodide-lock.json`). When code uses
  pandas, the worker also loads openpyxl/xlsxwriter (and matplotlib for plots), because pandas imports them lazily.

`--sandbox-selftest` verifies all of this in the real webview (run it on each OS).

### Guardrails for small models

Learned from live runs with gpt-5-nano:
- **Files opened by their real path:** string literals in the code that name files in the agent's folders (host
  paths, `in/X`, `out/X`) are found by parsing the code with `ast` (never executing it on the host). Those files are
  copied in automatically and mirrored at the path the code uses. A file edited in place comes back as an output.
- **Chained edits:** if the agent already proposed a change to a file in this message, its next run works on that
  pending version, and the new output *replaces* the earlier proposal (one card per file per message).
- **Feedback:** the tool result reports exactly what changed ("9 cells changed; G3: Open → Delayed…"). An output
  identical to the original is reported as "no changes" (catches e.g. matching `'45821'` against the number
  `45821`).
- **Dates written as text:** if a cell held a date and now holds an ISO date string, it's converted back to a real
  date with the original number format, and both the model and the card are told.
- **Formatting loss:** lost number formats/fills (typical of rewriting a workbook with pandas) trigger a warning
  telling the model to use openpyxl.

## Proposed changes (`changes.py`)

Every output file becomes a row in `changes`. Policy comes from the agent's `file_changes`:
- **New files:** saved automatically, or "ask".
- **Existing files:** "ask" (the default), or applied automatically with a backup.

Statuses: `pending → applied | discarded | conflicted → applied`, and `applied → undone`.

**Apply:**
1. Re-hash the target. If it changed since the proposal, mark it `conflicted`; the user can "Apply anyway".
2. Snapshot the current file into `data/history/<sha256>` (`file_versions` table).
3. Write via temp file + `os.replace`.

**Undo** restores the snapshot, after snapshotting the current file too. A *created* file can't be undone, because
that would be a delete, and nothing is ever deleted. History older than 30 days is pruned on startup.

## Trying it

```sh
.venv/bin/python dev/make_sample_docs.py      # → dev/sample-docs/ (PO tracker, supplier PDFs, a scan, docx, pptx)
```

1. Create an agent and grant `dev/sample-docs` as **Read & write**.
2. Ask: "Read the Acme supplier update PDF and update PO Tracker.xlsx to match it."
3. Review the card, then Apply or Undo.

A run like this with gpt-5-nano is ~10–40k tokens.
