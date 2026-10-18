# Space Agents — Plan

> An open-source, local-first "agent management" desktop app: a Slack-style window where
> non-technical users create, configure, and chat with AI agents that run against self-hosted,
> OpenAI-compatible models and organization MCP servers.

---

## 1. Goals

1. **Approachable for non-technical users.** Creating an agent should feel like filling out a form, not writing config.
   Anyone in supply chain, finance, engineering, etc. can stand up an agent like "Purchase Order Manager" in minutes.
2. **Slack-like experience.** Agents live in a sidebar with names, avatars, status, and a preview of what they did
   recently. Clicking one opens its "channel": a timeline of chat messages and run reports.
3. **Configurable via a manifest.** Each agent has a manifest defining its purpose, instructions, model, data sources
   (MCP servers), local file access, and schedule.
4. **Read-only by default; changes are proposed, not made.** Agents can read from MCP servers, allowed folders, and
   files the user drops into chat. When an agent creates or edits a document, it works on copies in a sandbox and the
   result comes back as a **proposed change**. Changes to existing files need the user's OK by default, every
   overwrite keeps a restorable version, and nothing is ever deleted. No outbound actions (emails, Teams messages,
   etc.) in v1, but the design leaves room for them later behind human approval.
5. **Documents are first-class and local-first.** PDFs, Word, Excel, and PowerPoint files are read on the user's
   machine by default (fast, offline, cached). The org's GPU document server is available for scans and hard cases.
6. **Local-first.** Agents run on the user's machine while the app is open. No central agent hosting. Model inference
   and MCP calls go to the organization's own servers, so data stays on the company network.
7. **Lightweight and cross-platform.** Windows is the primary target and macOS is fully supported. No Node/npm. Minimal Python
   dependencies. Users get one clickable app icon, distributed as an installer.

### Non-goals (v1)

- Agents that act externally (sending email/Teams, updating Jira/Costpoint records).
- Running agents while the computer is off or asleep, or any cloud/server-side agent hosting.
- Multi-user collaboration or shared agents across machines (export/import of manifests is enough for now).
- A plugin marketplace, shell access, or **unsandboxed** code execution. Agents can run Python only inside the Pyodide
  sandbox (§2.9).
- Local OCR. Scanned pages go to the org's document server (§2.8).

---

## 2. Architecture

### 2.1 Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 (dev and bundled) | Best agent/MCP ecosystem; mature package support; fast iteration |
| Dev tooling | [uv](https://docs.astral.sh/uv/) | Manages the Python version and venv; `pyproject.toml` + `uv.lock` give reproducible installs |
| Window | [pywebview](https://pywebview.flowrl.com/) | Native window using the OS webview (Edge WebView2 on Windows, WKWebView on macOS); no browser, no Chromium bundle |
| UI | HTML + CSS + [Preact](https://preactjs.com/) + [htm](https://github.com/developit/htm), vendored ES modules | React-style components and hooks with **no build step and no npm** (see 2.4) |
| UI ↔ Python | pywebview `js_api` bridge | JS calls Python directly (`await pywebview.api.x()`); Python pushes events via `run_js`. No Flask or app HTTP API |
| Local server | small WSGI app (`server.py`, using `bottle`, which pywebview already bundles) | Serves `ui/` on 127.0.0.1 with correct `.mjs`/`.wasm` types, the sandbox worker's CSP header, and token-scoped sandbox file routes |
| HTTP | `httpx` | Async, streaming, HTTP/2-capable; used for both LLM and MCP |
| TLS | `truststore` | Uses the OS certificate store so internal servers signed by a corporate CA just work |
| Secrets | `keyring` | API keys go in Windows Credential Manager / macOS Keychain, never in plain files |
| Storage | `sqlite3` (stdlib) + JSON files | Chat history, runs, audit log in SQLite; agent manifests as portable JSON |
| Documents | [`pypdfium2`](https://github.com/pypdfium2-team/pypdfium2) + stdlib (`zipfile`, `xml`) | Fast local text extraction: PDFium for PDFs, OOXML parsing for Word/Excel/PowerPoint; SQLite FTS5 for search (§2.8) |
| Code execution | [Pyodide](https://pyodide.org/) (Python on WebAssembly) in a Web Worker, vendored | Sandboxed Python with pandas/openpyxl/python-docx; no disk or network access; no install or admin (§2.9) |
| Packaging | PyInstaller (onedir) → Inno Setup (Win) / `.dmg` (macOS) | One clickable icon; users don't install Python |

**Runtime dependencies (target):** `pywebview`, `httpx`, `truststore`, `keyring`, `pypdfium2`.
pywebview pulls in `pythonnet` on Windows and `pyobjc` on macOS automatically.
**Dev-only:** `pytest`, `pytest-asyncio`, `ruff`, `pyinstaller`.
**Front-end (vendored, no npm):** `preact`, `preact/hooks`, `htm`, `marked`, `DOMPurify`, about 5 files in total.
**Sandbox (vendored, no npm):** Pyodide 314.0.7 (Python 3.14) plus the allowed wheels (§2.9), 34 MB. Fetched by
`scripts/vendor.py` and git-ignored; hashes are pinned in `ui/vendor/vendor.lock.json`.

We deliberately **do not** depend on:
- the `openai` SDK: we only need a few endpoints, so `httpx` is enough.
- the official `mcp` SDK: it pulls in pydantic, starlette, uvicorn, etc. that a client-only app doesn't need. We
  implement thin MCP and OpenAPI clients behind one interface (see 2.6) and can swap in the official SDK later if the
  protocol surface grows.
- PyMuPDF: it's fast, but its AGPL license is awkward for an open-source app other organizations will ship.
  `pypdfium2` (Apache/BSD) is comparably fast.
- a local OCR stack: CPU OCR is slow (roughly 0.5–3 s/page) and heavy, and the org's GPU document server does it better.

**Developer setup:**
```sh
uv python install 3.12
uv sync                      # creates .venv from pyproject.toml + uv.lock
uv run python -m space_agents --debug
```

### 2.2 Process and threading model

```
┌───────────────────────────── SpaceAgents.exe / SpaceAgents.app ─────────────────────────────┐
│                                                                                           │
│  Main thread: pywebview GUI loop (required by both WebView2 and Cocoa)                    │
│     └── Window: ui/index.html  ──js_api──►  Bridge (space_agents/bridge.py)                │
│                                  ◄─events──  (batched evaluate_js pushes)                 │
│                                                                                           │
│  Background thread: asyncio event loop  ("core")                                          │
│     ├── Scheduler           – decides when each agent runs                                │
│     ├── Runner              – executes agent loops, enforces concurrency/cancellation     │
│     ├── LLM client          – OpenAI-compatible Chat Completions (streaming + tools)      │
│     ├── Tool sources        – MCP (Streamable HTTP) + OpenAPI servers, lazily connected   │
│     ├── Tool registry       – filesystem, documents, sandbox, built-ins, MCP tools        │
│     ├── Changes             – proposed changes, apply/discard, file version history       │
│     └── Store               – SQLite (single writer), manifests on disk                   │
│                                                                                           │
│  Document extraction: dedicated thread (pypdfium2 + OOXML); worker process planned        │
│  Sandbox: Pyodide in a Web Worker inside the webview (no bridge, no disk, no network)     │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- Bridge methods are called on pywebview worker threads and hand work to the core loop with
  `asyncio.run_coroutine_threadsafe`. Long operations return immediately with an id and stream progress as events.
- **Event push:** core emits events (`message.delta`, `run.started`, `tool.called`, `agent.status`, `notification.new`)
  into a queue. A flusher sends them to JS in batches every ~50 ms with one `evaluate_js("window.__events(...)")` call,
  so token streaming doesn't flood the bridge.

### 2.3 On-disk layout

| | Windows | macOS |
|---|---|---|
| App data | `%LOCALAPPDATA%\SpaceAgents\` (local, not roaming: SQLite doesn't belong in roaming profiles) | `~/Library/Application Support/SpaceAgents/` |
| Logs | `%LOCALAPPDATA%\SpaceAgents\logs\` | `~/Library/Logs/SpaceAgents/` |

```
SpaceAgents/
  settings.json          # endpoint overrides (no secrets), cached remote config + catalog, UI prefs, autostart flag
  agents/
    po-manager.json      # one manifest per agent (shareable; contains no secrets)
  avatars/<id>.png       # optional agent pictures
  staging/<change-id>/   # sandbox outputs waiting to be applied (proposed changes)
  history/<sha256>       # previous versions of files the app overwrote (restorable, pruned after 30 days)
  workspaces/<token>/    # per-run sandbox copy-in/copy-out folders (deleted after each run and on startup)
  attachments/<sha256>/  # (planned) snapshots of files the user dropped into chat
  logs/                  # only when the data dir is overridden (dev); otherwise the OS log folder
  space.db               # SQLite. Built: messages (+ token usage), tool_calls (audit), changes, file_versions,
                         #   doc_cache + doc_pages + doc_fts (FTS5). Planned: attachments, runs, notifications,
                         #   agent_memory
```

The API key will be stored in the OS keychain under service `SpaceAgents`. During development it comes from
`SPACE_AGENTS_API_KEY` in `.env`.

### 2.4 UI

**Preact + htm, no build step.** Preact gives React's component and hooks model in about 4 KB. htm replaces JSX with
tagged template literals that the browser runs as-is, so there's nothing to compile:

```js
import { html } from "htm/preact";
import { useState } from "preact/hooks";

export function AgentRow({ agent, active, onSelect }) {
  return html`
    <li class=${active ? "agent active" : "agent"} onClick=${() => onSelect(agent.id)}>
      <span class="avatar" style=${{ background: agent.avatar.color }}>${agent.avatar.emoji}</span>
      <span class="name">${agent.name}</span>
      ${agent.unread > 0 && html`<span class="badge">${agent.unread}</span>`}
    </li>`;
}
```

- **Vendoring:** pinned ES module builds live in `ui/vendor/` and are wired up with an import map in `index.html`
  (`"preact": "./vendor/preact.module.js"`, etc.). `scripts/vendor.py` (stdlib Python) downloads the pinned versions
  and verifies SHA-256 hashes, so upgrading is one command and still needs no npm. Total vendored JS is under 100 KB.
- **Why not React?** JSX needs a compiler, and a compiler means npm and its dependency tree. Preact + htm keeps the
  same mental model without that.
- **Why not vanilla?** A Slack-like UI with live streaming, unread counts, status dots, and big forms is mostly state
  management. Components keep that sane without adding real weight.
- **Styling:** hand-written CSS with custom properties (design tokens), grid/flex, and native nesting. No CSS framework.
  Icons are a vendored subset of [Lucide](https://lucide.dev/) SVGs.
- **Markdown:** `marked` renders agent output.
- **Sanitization:** DOMPurify is **mandatory** for all rendered model output. Model output can echo untrusted
  email/document content, and since the webview has a bridge into Python, XSS would be a privilege escalation. Also set
  a strict Content-Security-Policy in `index.html` (no inline script, no remote origins).
- **Serving the files:** our WSGI app (`server.py`), passed to pywebview as the window URL, serves `ui/` on
  `127.0.0.1` (ES modules can't load from `file://`). Every app action goes through the `js_api` bridge. The only other
  routes are the sandbox's file transfer routes (§2.9), scoped to a single run by an unguessable token.
- **CSP:**
  - **Page:** adds `worker-src 'self'`.
  - **Sandbox worker:** gets its own CSP as an HTTP header, with `'wasm-unsafe-eval'` to compile WebAssembly and
    `connect-src 'self'`.
  - **Don't add `worker-src 'none'` to the worker:** WebKit applies it to the worker's own module imports, so
    Pyodide can't load.

Layout (Slack-style):

```
┌──────────────────────┬─────────────────────────────────────────────────────────────┐
│ Space Agents      ⚙  │  📦 Purchase Order Manager   ● idle · glm-5.3     [Run now] ⋯│
│──────────────────────│─────────────────────────────────────────────────────────────│
│ 🔔 Activity      (3) │  ┌ Run report · Today 9:02 AM ───────────────────────────┐  │
│──────────────────────│  │ 3 POs delivered, 1 supplier delay                      │  │
│ AGENTS               │  │ ⚠ PO 45821 – Acme: ship date slipped to 10/14          │  │
│ 📦 PO Manager    (2) │  │   → Suggest: notify program lead, check alt supplier   │  │
│   3 deliveries, 1 …  │  │ ▸ 6 tool calls (costpoint ×3, outlook ×2, files ×1)    │  │
│ 🐞 Jira Triage       │  └────────────────────────────────────────────────────────┘  │
│   Idle · 2h ago      │                                                             │
│ 📅 Meeting Prep  ⟳   │  You: what did Acme say exactly?                            │
│   Running…           │  📦: Acme's email from 9/30 says … [source: Outlook]        │
│                      │                                                             │
│ + New agent          │  ┌─────────────────────────────────────────────────────┐    │
│                      │  │ Message Purchase Order Manager…                ⏎    │    │
└──────────────────────┴──┴─────────────────────────────────────────────────────┴────┘
```

- **Sidebar:** agent avatar (emoji or initials + color), name, status dot (idle / running / error / paused),
  last-activity snippet and relative time, unread badge. "Activity" is a cross-agent inbox of things waiting on the
  user.
  - ✅ **Activity:**
    - One entry per agent awaiting approval, with its change cards (Apply/Discard right there) and "Open
      conversation".
    - Entries clear once nothing is pending and the agent is done (including after revising on feedback).
    - A red count badge on the nav item, hidden at 0.
    - Scheduled-run reports join this inbox in Phase 3.
  - ✅ **Resizable:** drag the edge, snap to an icon-only rail, Ctrl/⌘ B toggle; the width is remembered.
  - ✅ **Right-click an agent:** Edit agent, or Duplicate (copies its setup as "<name> (Clone)").
- **Channel view:** a single timeline mixing chat messages and run-report cards. Tool calls are collapsed by default
  ("▸ 6 tool calls") and expand to show server/tool, arguments, duration, and a truncated result.
- **Agent settings drawer:** form-based manifest editor (Basics, Instructions, Model, Data sources, Files, Schedule,
  Notifications), with an "Advanced: view JSON" tab.
- **Attachments:** a paperclip button in the composer and drag-and-drop anywhere on the channel (§2.11). Attached
  files show as chips with type icon, name, size, page count, and reading progress.
- **Proposed-change cards:** "📊 Updated PO Tracker.xlsx: 3 rows added [Review] [Apply] [Discard]", with a review
  view and "Restore previous version" after applying (§2.10).
- **Plain-language activity:** sandbox runs appear as steps ("Read 12 invoices → Calculated totals → Created
  Summary.xlsx"). A "Show details" toggle reveals the code for power users.
- **App settings:** API key, available servers, start-at-login toggle, data folder location, about/version.
  Endpoint URLs are under an "Advanced" section (see 2.5).
- Light/dark theme following the OS.

### 2.5 Endpoints, identity, and the LLM client

**One API key per user.** The key the admin issues identifies the individual user everywhere:
- the model gateway (`/api/chat/completions`, `/api/models`)
- every MCP/OpenAPI server
- M365 data: the Outlook/Teams/SharePoint servers use the key to authenticate the user with Azure OAuth, then obtain an
  on-behalf-of (OBO) token to read *that user's* data.

The app itself never does an Entra/OAuth sign-in. It only holds the key (in the OS keychain).

**Endpoints are configurable, with the admin in control.** Two base URLs are configured separately, even though today
both are on the same site:

| Setting | Today | Used for |
|---|---|---|
| `llm_base_url` | `https://<site>/api` | `POST {llm_base_url}/chat/completions`, `GET {llm_base_url}/models` |
| `mcp_base_url` | `https://<site>` | `GET {mcp_base_url}/mcp` (catalog), `{mcp_base_url}/mcp/{id}` (servers) |

Effective values are resolved in layers. The first two are under admin control, so users normally just paste their key:
1. **Build defaults:** a `defaults.json` shipped inside the app. The org sets its URLs once per build; other
   organizations using the open-source project ship their own.
2. **Remote service config (optional):** on launch, the app fetches a tiny config document from the service, e.g.
   `GET {mcp_base_url}/space-agents/config.json`, which can override endpoint URLs, the minimum app version, and so on.
   If the URLs ever move, the admin changes this one file and every client picks up the change on its next launch,
   with no reinstall. The last good copy is cached, so the app still starts if the service is briefly unreachable.
3. **User overrides:** Settings → Advanced, for testing or special cases. Hidden from most users.

For development and testing, a `.env` file, environment variables, and CLI flags sit above these layers (see
`docs/configuration.md`).

**LLM client**
- Targets the **OpenAI Chat Completions** API with `stream: true` and `tools`, which the vLLM-backed gateway serves.
  The Responses API can be added later as an alternative backend behind the same interface.
- `GET {llm_base_url}/models` populates the model dropdown. There's no default model for now; the user picks one per
  agent. If the gateway returns context length (vLLM reports `max_model_len`), we use it for context budgeting.
- **Connection test** (onboarding and Settings) checks:
  - reachability
  - auth
  - the model list
  - **whether the selected model actually emits tool calls**, using a trivial tool-call probe

  Tool calling depends on vLLM launch flags per model (`--enable-auto-tool-choice --tool-call-parser <parser>`) and on
  the gateway passing `tools` and streamed `tool_calls` through, so we surface this clearly instead of failing silently.
- Retries with backoff on 429/5xx; respects `Retry-After`.
- **Built** (`llm/client.py`):
  - streaming with tool calls
  - token usage logged per run
  - `max_completion_tokens` cap
  - `reasoning_effort` (default `low`) to bound reasoning tokens
- **Parameter compatibility:** optional parameters a model rejects are dropped automatically and remembered per model.
  For example, GPT-5 models reject a custom `temperature`.
- **Streaming fallback:** if a provider refuses streaming for a model, the client falls back to non-streamed responses.
- **Dev API key:** comes from `SPACE_AGENTS_API_KEY` in `.env` until onboarding and the keychain exist.
- **Tested:** OpenAI's API with `gpt-5-nano`.

### 2.6 Tool sources (MCP and OpenAPI servers)

The organization's servers come in two flavors, and we support both behind one interface:

- **Streamable HTTP MCP** at `{mcp_base_url}/mcp/{id}`
- **OpenAPI** with the spec at `{mcp_base_url}/mcp/{id}/openapi.json`

 **Users never see the difference.** They pick "Costpoint" or "Outlook" from a list, and the transport is
resolved from the catalog or auto-detected.

```python
class ToolSource(Protocol):
    id: str

    async def list_tools(self) -> list[ToolSpec]: ...  # name, description, JSON schema, read_only flag
    async def call_tool(self, name: str, args: dict) -> ToolResult: ...
```

Both use `Authorization: Bearer <same API key>` and share an `httpx.AsyncClient` (connection pooling, the OS trust
store, timeouts, and retries).

**`McpSource`: Streamable HTTP**
- JSON-RPC 2.0 over POST. Responses come back as `application/json` or `text/event-stream`. Tracks `Mcp-Session-Id`.
- Client-side methods: `initialize`, `notifications/initialized`, `tools/list` (paginated), `tools/call`.
  Resources/prompts are deferred until needed.
- Read-only comes from the catalog's `read_only` flag, falling back to the `readOnlyHint` tool annotation.
- Tool lists are refreshed on reconnect or on `notifications/tools/list_changed`.

**`OpenApiSource`: OpenAPI 3.x**
- Loads the spec from `{mcp_base_url}/mcp/{id}/openapi.json` and turns each operation into a tool:
  - name: `operationId` (sanitized, ≤64 chars, falling back to `method_path`)
  - description: `summary` + `description`
  - parameters: path, query, and JSON request-body schemas merged into one object schema
- `$ref`s are resolved and inlined (with a depth limit), and keywords that local models' tool parsers choke on are
  stripped. The goal is small, flat schemas.
- Calls substitute path params, encode the query, send the JSON body, and return the JSON/text response.
- Read-only is determined from, in order:
  1. the catalog's `read_only` flag
  2. an `x-read-only: true` / `x-readOnlyHint` extension on the operation
  3. the HTTP method: `GET`/`HEAD` are read-only, everything else is not.

**Discovery: the `/mcp` catalog endpoint** (to be added on the server side)

`GET {mcp_base_url}/mcp` with `Authorization: Bearer <key>` returns the servers available *to this user*. That lets the
server hide servers a user isn't entitled to. Proposed response, for us to finalize together:

```jsonc
{
  "catalog_version": 1,
  "servers": [
    {
      "id": "costpoint",
      "name": "Costpoint",
      "description": "Purchase orders, receipts, vendors",
      "icon": "https://<site>/mcp/costpoint/icon.svg",   // optional
      "type": "openapi",                                  // "openapi" | "mcp"
      "tools": [                                          // overview, so the UI can show tools without connecting
        { "name": "search_receipts", "description": "Find PO receipts by date/vendor", "read_only": true },
        { "name": "update_po",       "description": "Modify a PO",                     "read_only": false }
      ]
    },
    { "id": "outlook", "name": "Outlook", "type": "mcp", "tools": [ … ] },
    { "id": "docs", "name": "Document Processing", "type": "mcp", "role": "document_processing", "tools": [ … ] }
  ]
}
```

- `read_only` in the catalog is the **authoritative** read-only flag for both server types. That matters most for
  OpenAPI servers whose operations are all `POST`.
- `role: "document_processing"` identifies the GPU document server (see 2.8), so its id isn't hardcoded.
- The catalog is cached and refreshed on launch and from Settings ("Refresh servers").
- Fallback, if the catalog is unavailable or for testing unlisted servers: the user adds a server by id under Advanced,
  and we **auto-detect** the type. We try `/mcp/{id}/openapi.json` first, then an MCP `initialize` at `/mcp/{id}`.

**Common behavior**
- Tools are namespaced for the model as `{server}__{tool}` (OpenAI function names allow `[a-zA-Z0-9_-]{1,64}`).
- Tool lists are cached per session. Connections are opened lazily on an agent's first use.
- Each source is a small module, so we can swap `McpSource` for the official SDK if the protocol surface grows.

### 2.7 Tools available to agents

The model sees a few clear, task-shaped tools. Users see plain-language activity steps ("Read PO Tracker.xlsx"), with
"Show details" revealing arguments, code, and results.

| Tool group | Tools | Status | Notes |
|---|---|---|---|
| Files (read) | `list_files(folder?, pattern?)` | ✅ built | Recursive listing of granted folders |
| Documents | `read_document(path, pages?)`, `search_document(path, query)` | ✅ built | Local extraction, cached and indexed (§2.8) |
| Python | `run_python(description, code, input_files?, output_folder?)` | ✅ built | Pyodide sandbox on copies of the inputs; output files become proposed changes (§2.9–2.10) |
| Documents | `list_pages(path)` (outline: headings, sheets, slides) | planned | |
| Writing | `append_rows`, `create_document`, … | deferred | Structured writers. With the guardrails in §2.9, `run_python` handled spreadsheet edits reliably even with gpt-5-nano; revisit if needed |
| Org servers | whatever the selected MCP/OpenAPI servers expose | planned (Phase 2a) | Read-only tools only by default (§4). Includes the GPU document server's `convert_document` |
| Built-in | `post_report`, `remember`, `recall`, `current_time` | planned (Phase 3) | `post_report` is how a scheduled run delivers its result |

- **Errors are results, not crashes.** Tool errors (access denied, unsupported file, code failure, even an unexpected
  bug) go back to the model as the tool's result, so it can correct itself or explain.
- **Truncation:** results over 12k characters are cut, with a note telling the model how to page or narrow the query.

### 2.8 Documents: local-first ✅

Most business documents are digital: PDFs exported from Word or ERP systems, plus Office files. These have real text
that can be extracted in seconds on a laptop, with no OCR. Scanned pages are the exception, and those belong on the GPU
document server.

**Extraction** (`space_agents/documents/extract.py`)

| Format | How | Notes |
|---|---|---|
| PDF | `pypdfium2` (PDFium, Chrome's PDF engine) | Per-page text; **measured ~460 text pages/s** (1,000 pages ≈ 2 s). Each page is classified as *text*, *no text layer* (scanned), or *garbled* (broken font encoding) |
| `.docx` | stdlib `zipfile` + XML | Headings → `#`, tables → Markdown. Chunked into ~3k-character "pages" |
| `.xlsx` | stdlib, streaming | Per sheet: Markdown table, 100 rows per page, header repeated. Date-formatted cells shown as ISO dates (not Excel serial numbers) |
| `.pptx` | stdlib | One page per slide |
| `.csv`, `.txt`, `.md`, `.json`, `.eml`, logs | stdlib | `.msg` (Outlook) and legacy `.doc/.xls/.ppt` aren't supported locally; they're reported so the agent can use the document server |
| Images | none locally | Reported as unsupported; the agent can send them to the document server |

Extraction currently runs on **one dedicated thread**, because `pypdfium2` isn't thread-safe. **Planned hardening:** a
separate worker process with a per-file timeout, so a malformed or malicious file can't hang or crash the app.

**Cache and search** (`space_agents/documents/library.py`)
- Extracted text is stored per page in SQLite (`doc_cache` keyed by content SHA-256, `doc_pages`, and an **FTS5**
  index `doc_fts`). Each file is extracted once per version; a renamed copy is found by hash; later reads are instant.
- Lookups go `(path, size, mtime)` → SHA-256 → cached pages.
- **Planned:** LRU eviction above a size cap (e.g. 1 GB), and optional background indexing of granted folders while
  the computer is idle.

**Tools**
- **`read_document(path, pages="1-20")`** returns text with page markers and a header, e.g.
  `"Q3 Supplier Report.pdf: 1,000 pages (pdf)"` plus notes like *"Pages 412–450 have no text layer (likely scanned
  images)"*. Output stops at ~12k characters with a hint for the next page range. A 1,000-page PDF is ~500k+ tokens,
  so documents are never dumped whole.
- **`search_document(path, query)`** does a BM25 search over the cached pages and returns the best pages with
  snippets. This is how agents answer questions about big documents.

**The GPU document server.** It stays in the catalog (`role: "document_processing"`) and will be available to every
agent as a regular tool (`convert_document`, with optional VLM mode) once tool sources are wired up (Phase 2a). The
model decides when to use it, guided by `read_document` flagging scanned or garbled pages. Automatic routing of scanned
pages, and skills that teach models when local processing isn't good enough, come later.

### 2.9 Code execution: Pyodide sandbox ✅

Agents can run Python, but only inside **Pyodide** (Pyodide 314.0.7, which runs CPython 3.14 compiled to WebAssembly)
in a **Web Worker** in the app's webview. Details: `docs/documents-and-sandbox.md`.

```
agent loop ──run_python(code, input_files)──► ToolBox (tools/registry.py)
                                                 1. resolve inputs via FolderAccess; find file paths in the code
                                                 2. PyodideSandbox.run (sandbox/pyodide.py): copy inputs to
                                                    workspaces/<token>/in/, mint a one-run token
                                                 3. run_js("window.__sandbox.run(job)") → ui/js/sandbox.js
                                                          │
                                          Web Worker (ui/sandbox/worker.js)
                                                 4. GET /workspace/<token>/in/* → in-memory FS /work/in (+ aliases)
                                                 5. load allow-listed packages the code needs; run it (cwd /work)
                                                 6. PUT outputs → /workspace/<token>/out/*
                                                 7. call("sandbox_done", run_id, result) via the main thread
                                                          │
ToolBox ◄─────────────────────────────────────────────────┘
  8. outputs → diff/repair step (in the sandbox) → proposed changes (§2.10); stdout + change summary → tool result
  9. token revoked, workspace deleted
```

- **Isolation:**
  - **Disk:** Pyodide only has an in-memory filesystem. The worker sees only copies of the inputs.
  - **Network:** the worker's own CSP header (sent by `server.py`) allows `connect-src 'self'` only. The only routes
    that accept anything are the run's own `/workspace/<token>/…`, with a 192-bit token valid for one run. WebKit
    leaves a blocked fetch pending rather than rejecting it, so external requests simply never complete.
  - **Bridge:** none. Workers can't reach `window.pywebview`, and messages from the worker are validated as untrusted
    data.
- **Limits:**
  - **Time:** wall-clock timeout per run (default 90 s, including first-use load), after which the worker is
    terminated and respawned.
  - **Outputs:** max 20 output files and 100 MB of outputs per run, plus the WebAssembly memory ceiling.
- **File transfer** happens over token-scoped HTTP on the app's local server: binary, not base64 through
  `evaluate_js`.
- **Packages** (vendored, allow-listed, 34 MB):
  - `pandas`, `numpy`, `openpyxl` (+ `et_xmlfile`), `python-docx` (+ `lxml`), `pypdf`, `xlsxwriter`, `matplotlib`
  - Nothing installs at runtime.
  - Pandas' lazily imported engines (openpyxl/xlsxwriter, matplotlib for plots) are loaded with it.
- **Runs:** one worker, **warmed at launch** (a background preload of Pyodide + openpyxl + pandas, ~420 MB; mode
  `full`/`light`/`off`) and re-warmed after a kill. The first agent run is <1 s instead of ~20 s. Every run gets a fresh
  `/work` directory and a fresh global namespace, and runs are serialized.
- **Guardrails for small models**, learned from live gpt-5-nano runs:
  - **File paths in code:** files the code names are auto-mapped into the sandbox (host paths, `in/X`, `out/X`). This
    is detected with `ast`, never by running the code on the host. In-place edits come back as outputs.
  - **Chained edits:** they build on the agent's own pending version of the file.
  - **Feedback in the tool result:** it reports exactly which cells or lines changed. An identical output is reported
    as "no changes".
  - **Date cells:** text written into date cells as ISO dates is repaired to real dates.
  - **Formatting:** lost formatting (e.g. a pandas rewrite) is flagged so the model redoes the edit with openpyxl.
- **Backend interface:** planned. `ExecutionBackend.run(code, inputs, limits) -> RunResult`, so Docker/Podman or a
  server-side sandbox can be added later without changing tools or the changes flow. Today `PyodideSandbox` is the only
  backend.
- **Verification:** `python -m space_agents --sandbox-selftest` runs 9 checks in the real webview. All pass on macOS;
  Windows/WebView2 is still to be run.

### 2.10 Proposed changes and file history ✅

Agents **never write directly** to user files. Every file an agent produces becomes a **proposed change**
(`space_agents/changes.py`):

```
Proposed change
  target:   ~/Documents/PO Reports/PO Tracker.xlsx      (must be inside a read_write folder)
  kind:     modify | create
  staged:   <data>/staging/<change-id>/PO Tracker.xlsx
  base:     sha256 of the target when proposed            (detects edits made in the meantime)
  summary:  model's description + computed diff (cells/lines changed, samples, repairs, warnings)
  status:   pending → applied → undone
            pending → discarded
            pending → conflicted → applied ("Apply anyway") | discarded
```

- **Policy (per agent, `file_changes` in the manifest):**
  - **New files:** `allow` (default: saved immediately and shown as a card) or `ask`.
  - **Existing files:** `ask` (default: Apply/Discard card) or `allow_with_backup`.
  - **Deletes, renames, and moves are never possible.**
- **Review (inline on the card):**
  - **Excel:** sheets changed, row counts, and a cell-by-cell before/after table (up to 12 samples).
  - **Text/CSV/Markdown/JSON:** a line diff.
  - **Anything else:** size only.

  Excel diffs are computed in the sandbox. **Planned:** a Word paragraph diff, and a full-screen review view for big
  changes.
- **Apply:**
  1. Re-check the target hash. If the file changed since the proposal, it's marked *conflicted* and the user decides.
  2. Snapshot the current file into `history/`.
  3. Write via a temp file and atomic rename.
- **Discard → "What should be different?"** (optional):
  - **With feedback:** the change is discarded and the feedback is posted as the user's next message, so the agent
    revises and a new card appears.
  - **Without feedback:** it's just discarded.
- **The agent learns outcomes.** When history is built, each earlier reply is annotated with what happened to its file
  changes (applied, discarded, undone, still pending).
- **Revisions in one reply:** if the agent edits the same file again before the user acts, the new version replaces
  the pending proposal, so there's one card per file per reply.
- **File history:** every overwrite keeps the previous version (`file_versions` table). **Undo** on the card restores
  it, after snapshotting the current file too. A created file can't be undone, since that would be a delete. History
  older than 30 days is pruned on startup.
  - **Planned:** a per-agent "Files changed" list and a size cap.
- **Scheduled runs (planned, Phase 3):** pending changes go to the Activity inbox with a notification ("PO Manager
  wants to update PO Tracker.xlsx") and expire after 7 days.
- **Planned: a permission card for read-only folders.** If an agent tries to save into a folder granted *read only*,
  it shows *"Allow writing to this folder?"* instead of a dead end.

### 2.11 Attachments (drag-and-drop and upload): planned (Phase 2a)

Users can give an agent files directly in chat, no folder grant needed.

- **How to attach:**
  - The **paperclip** button opens the native file dialog (multi-select).
  - **Dragging files** anywhere onto the channel shows a drop overlay ("Drop files for Purchase Order Manager").
    pywebview's DOM drop event provides the real file paths (`pywebviewFullPath`) on both platforms, so files aren't
    streamed through the bridge.
- **Snapshot on attach:** the file is copied into `attachments/<sha256>/` (deduplicated by content), so the agent works
  on a stable copy and the original is never touched. Size limit: 200 MB per file (configurable).
- **Pre-extraction starts immediately** in the background. The chip shows progress ("Reading… 340/1,000 pages"),
  so the text is usually ready by the time the user hits Send.
- **Grant:** the agent can read that attachment for the rest of the conversation. Attachments are listed in the
  agent's context as `attachment://<id>/<filename>` and accepted by `read_document`, `search_document`, and
  `run_python`.
- **Agent-produced files** appear in the same timeline as cards (proposed changes), with "Open" and "Show in folder"
  once applied.
- **Images:** attachable now. They're readable through the document server and, later, sent directly to vision-capable
  models.

---

## 3. Agents

### 3.1 Manifest schema (v1)

```jsonc
{
  "schema_version": 1,
  "id": "po-manager",
  "name": "Purchase Order Manager",
  "avatar": { "emoji": "📦", "color": "#3b82f6" },
  "description": "Tracks PO deliveries and supplier updates.",

  // Who the agent is and how it behaves (system prompt). Plain English.
  "instructions": "You help a supply chain buyer manage open purchase orders. ...",

  // Any model from GET {llm_base_url}/models. The API key and endpoints are app-wide, not per agent.
  "model": {
    "name": "glm-5.3",
    "temperature": 0.2
  },

  // MCP or OpenAPI servers. Transport comes from the catalog; the document server is always attached implicitly.
  "mcp_servers": [
    { "id": "costpoint", "tools": "read_only" },          // or an explicit list of tool names
    { "id": "outlook",   "tools": ["search_messages", "get_message"] }
  ],

  "filesystem": [
    { "path": "~/Documents/Purchase Orders", "access": "read" },
    { "path": "~/Documents/PO Reports",      "access": "read_write" }
  ],

  // How proposed changes are applied in read_write folders (§2.10). Deletes are never possible.
  "file_changes": {
    "new_files": "allow",          // allow | ask
    "existing_files": "ask"        // ask | allow_with_backup
  },

  // What to do on each scheduled run. Optional: agents without a schedule are chat-only.
  "schedule": {
    "enabled": true,
    "every_minutes": 60,
    "active_hours": { "start": "07:00", "end": "18:00" },
    "weekdays": ["mon", "tue", "wed", "thu", "fri"],
    "run_on_startup_if_missed": true
  },
  "task": "Check Costpoint for POs delivered since the last run. Check my email for supplier updates about open POs. Report anything new, flag delays, and suggest next actions.",

  "notifications": { "desktop": true, "min_level": "action" },   // info | action | urgent
  "memory": { "enabled": true },
  "limits": { "max_tool_calls": 30, "max_minutes": 10 },
  // Summarize earlier work when a run gets long (§3.2 step 6). Tokens; the summary uses the agent's model.
  "compaction": { "enabled": true, "trigger_tokens": 100000, "summary_tokens": 2000 },

  // Forward-compatible: external actions will require approval. Unused in v1.
  "permissions": { "external_actions": "never" }                  // never | ask | allow
}
```

Validation uses plain dataclasses plus hand-written checks (no pydantic). Manifests are versioned and migrated on load.
New fields with safe defaults (like `file_changes`) are additive and don't bump `schema_version`.

### 3.2 Agent loop

**The app is the orchestrator.** The model gateway only generates text and tool-call requests. Space Agents executes
every tool call itself, on the user's machine, with the user's key, then feeds the results back to the model. This is
the standard OpenAI-style client-side tool loop, and it needs nothing new on the server beyond tool calling being
enabled in vLLM:

```
 Space Agents (laptop)                      Model gateway (vLLM)          MCP / OpenAPI servers
 ─────────────────────                      ────────────────────          ─────────────────────
 messages + tool definitions ──────────────► POST /api/chat/completions
                              ◄────────────── assistant: tool_calls[costpoint__search_receipts(...),
                                                                    outlook__search_messages(...)]
 check policy, run both calls in parallel ─────────────────────────────────► POST /mcp/costpoint/...
                                          ─────────────────────────────────► POST /mcp/outlook (JSON-RPC)
                              ◄───────────────────────────────────────────── results (user's own data, via OBO)
 append tool results ──────────────────────► POST /api/chat/completions
                              ◄────────────── assistant: more tool_calls … or the final answer
```

Why this design:
- **The server stays stateless and simple:** no per-user agent state or server-side tool execution.
- **Identity is correct by construction:** every MCP call carries the user's key, so OBO returns their data.
- **Local tools fit naturally:** files and documents run in the same loop as remote tools.
- **Policy is enforced before execution:** the read-only rules and the folder sandbox are checked in our code, before
  any call happens.

Each step resends the conversation so far. vLLM's automatic prefix caching makes that cheap, because the shared
prefix isn't recomputed. Even with 1M-token context windows we keep a working budget (default ~128k tokens,
configurable) for latency and to share the GPUs fairly.

Gateway requirements:
- Pass `tools`, `tool_choice`, and `parallel_tool_calls` through to vLLM.
- Stream `tool_calls` deltas back.
- Start each served model with tool calling enabled and the right `--tool-call-parser`.

Steps:

1. **Assemble context:** base system prompt (safety rules, how to use tools, how to cite sources, today's date/time,
   user's timezone) + manifest `instructions` + a description of granted sources + memory notes + summaries of the last
   N run reports (so the agent knows what it already reported). Built today: the system prompt plus the last 20
   messages, with each earlier reply annotated with the outcome of its proposed file changes (§2.10).
2. **Call model** with streaming and the agent's tool list.
3. **Execute tool calls** (independent calls in parallel, with a concurrency cap), record each one in the audit log,
   append results, and loop.
4. **Stop** when the model answers without tool calls, or when `max_tool_calls` / `max_minutes` is hit (the agent is
   then asked for a final answer with no tools).
5. **Scheduled runs** must end with `post_report`:
   ```json
   { "summary": "3 POs delivered, 1 supplier delay",
     "items": [ { "level": "action", "title": "PO 45821 ship date slipped", "detail": "...",
                  "suggested_actions": ["Notify program lead", "Check alternate supplier"],
                  "sources": ["outlook:msg/AAMk...", "costpoint:po/45821"] } ] }
   ```
   A tool call is more reliable across local models than JSON mode. If the model doesn't call it, its final text is
   wrapped as a single `info` item.
6. ✅ **Context management (compaction),** per agent (`compaction` in the manifest, Model tab → Long tasks):
   - **When:** before each model call, the run's size is the model's own `prompt_tokens` from the last call plus a
     chars/4 estimate of what was added since. Past `trigger_tokens` (default 100k), the run is compacted.
   - **How:** the agent's own model summarizes the earlier part in about `summary_tokens` (default 2k). It continues
     from `[system prompt, summary + the request verbatim, latest round of tool calls and results]`. Tool calls and
     results are never split.
   - **If the summary call fails:** old tool results are dropped instead, and compaction isn't retried in that run.
   - **Visible:** each compaction is an activity step ("Summarized earlier work…"), and "Show details" shows the
     summary.
   - **Tested** live with gpt-5-nano: models overshoot length targets (~1.5×), so the prompt asks for half a word per
     budget token and the summary is cut only past 1.5× the budget, with the request and remaining work listed
     first.
   - **Planned:** a default trigger from the model's context length once `/models` reports it.

### 3.3 Chat vs. scheduled runs

- **Chat:** the user messages the agent in its channel. Same loop, same tools, with conversation history.
- **Run:** triggered by the schedule or "Run now". Uses `task` as the prompt and produces a report card in the channel,
  plus notifications for items at or above `min_level`. The user can reply to a report to ask follow-ups, and that
  conversation has the report in context.

### 3.4 Memory and deduplication

- `remember(key, note)` / `recall(query)` give a small per-agent note store in SQLite (e.g. "Already notified about
  PO 45821 delay on 10/01"). Notes are visible and deletable in the agent's settings.
- Recent run summaries are always injected, so agents naturally avoid repeating themselves.

---

## 4. Security model

The threat to design against is **untrusted content steering the model** (prompt injection via emails, documents, Jira
tickets) plus **ordinary model mistakes**, not a malicious local user.

1. **Filesystem sandbox (enforced in code, not prompts).**
   - Every path is expanded, made absolute, and resolved with `realpath`. It must sit under a granted root after
     resolution, which blocks `..` and symlink escapes.
   - A deny-list applies even inside granted roots: `.ssh`, `.gnupg`, keychains, browser profiles, credential stores,
     and the app's own data directory.
   - Agents never write user files directly. Writes go through proposed changes (§2.10), only into `read_write`
     roots, with size limits, no deletes, conflict checks, and a restorable version for every overwrite.
   - ✅ **Writes have stricter rules,** because a file in the wrong place runs code without anyone opening it:
     - **Never in sensitive folders:** system folders, `~/Library` (cloud drives excepted), `AppData`, or hidden
       folders, even inside a read & write grant. This blocks LaunchAgents, the Startup folder, and shell configs.
     - **Allowed file types only:** documents, data, and images by default. Admins can add more with
       `extra_file_types`, but program launchers (`.exe`, `.bat`, `.lnk`, …) are never allowed.
     - **Marked as downloaded:** written files get Mark of the Web / quarantine, so Office opens them in Protected
       View.
   - Files the user attaches in chat are readable by that agent as snapshots, even outside granted folders. Attaching
     is an explicit, per-file grant.
2. **Server tool policy (MCP and OpenAPI).**
   - By default only tools determined to be read-only are exposed (see 2.6 for how each source type decides). Anything
     else is hidden unless a future approval mode is enabled.
   - **Recommended:** enforce read-only server-side too. For example, the M365 servers could request read-only Graph
     scopes in the OBO exchange, and write tools could be omitted from the catalog for keys not entitled to them. The
     client-side policy is a guardrail against model mistakes, not a security boundary.
3. **Code runs only in the Pyodide sandbox** (§2.9):
   - no host filesystem: only copies of explicitly passed files
   - no network: CSP `connect-src 'self'`; the only reachable routes are the run's own token-scoped workspace
   - no bridge access: a Web Worker can't reach `window.pywebview`
   - CPU and time limits: the worker is terminated on timeout
   - outputs become proposed changes, never direct writes

   There is no shell tool. Messages from the worker are untrusted data and are validated by the main thread.
4. **Untrusted content handling.** Tool results are wrapped in clear delimiters, and the system prompt states that
   content from tools is data, not instructions. With no outbound action tools in v1, the worst a successful injection
   can do is *propose* changes inside user-approved folders, which are reviewable, restorable, and never deletes.
   **Planned:** malformed or malicious documents are parsed in a separate worker process with timeouts, so a bad PDF
   can't hang or crash the app. Today extraction runs on a dedicated in-process thread.
5. **UI hardening.** All model output is sanitized with DOMPurify and the page has a strict CSP. Links open in the
   system browser, never inside the app webview.
   - ✅ **The bridge doesn't trust the page with permissions.** The sandbox worker shares the page's renderer
     process, so one browser-engine bug could hand the page to an attacker without breaking the OS sandbox. So:
     - new folder grants must come from the native folder picker
     - write access, auto-applying changes, and new server addresses need OK in a native dialog

     Details: `docs/architecture.md`.
   - **Planned:** run the sandbox in a separate renderer process with no bridge at all.
6. **Secrets.** The API key is the user's identity across the models, every server, and (via OBO) their mailbox and
   files, so it's treated like a password:
   - It lives only in the OS keychain.
   - It is never written to manifests, exported agents, logs, or the database.
   - It is redacted from error messages.
   - It is sent only to the configured `llm_base_url` / `mcp_base_url` hosts, never to a URL taken from tool output.
   - Settings has "Replace key" for rotation.
7. **Audit log.** Every tool call is recorded (agent, run, tool, arguments, duration, result size, status) and viewable
   per agent.
8. **Data locality.** Prompts, data, and documents go only to the configured company AI server, MCP/OpenAPI servers,
   and document-processing server. There is no telemetry.

---

## 5. Running in the background

Agents run **only while the app is open and the computer is awake**, by design. To make that a non-issue:

- **Start at login** toggle (offered during onboarding, default on). It launches with `--background`, i.e. no window
  shown or minimized.
  - Windows: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` via stdlib `winreg`.
  - macOS: a LaunchAgent plist in `~/Library/LaunchAgents/` with `RunAtLoad`.
- **Closing the window doesn't quit.** Agents keep running. On Windows the window minimizes to the taskbar. On macOS
  it hides and the Dock icon reopens it. A proper system-tray/menu-bar icon is a later enhancement (see Phase 5).
  "Quit Space Agents" exits for real.
- **Single instance.** Launching again focuses the existing window: macOS gets this for free with `.app` bundles; on
  Windows we use a named mutex plus `FindWindow`/`SetForegroundWindow` via `ctypes`.
- **Scheduler behavior.**
  - It ticks every 30 s and compares wall-clock time to each agent's `next_run_at`, so it handles sleep/wake.
  - After sleep or a closed app, a missed run executes **once** (not once per missed interval) if
    `run_on_startup_if_missed` is set.
  - Respects `active_hours` and `weekdays`.
  - **Jitter:** each agent gets a stable random offset within its interval, so that hundreds of users' hourly agents
    don't all hit the shared AI server at :00.
  - A global cap on concurrent runs (default 2) and a per-user request rate limit protect the shared server.
- The UI makes it clear when agents are not running (e.g. "Paused: outside active hours", "Will run at 10:14").

---

## 6. Distribution

- **Build:** PyInstaller in **onedir** mode, because onefile self-extraction is a common antivirus false-positive trigger.
- **Windows:** Inno Setup installer (`SpaceAgents-Setup-x.y.z.exe`) installs per-user to `%LOCALAPPDATA%\Programs`
  without admin rights, creating Start-menu and optional desktop shortcuts. WebView2 is preinstalled on Windows 10/11.
  The installer checks for it and links to the Evergreen bootstrapper if missing. MSIX for Intune can come later.
- **macOS:** `.app` in a `.dmg`, universal2 or separate arm64/x86_64 builds.
- **Size:** expected ~90–120 MB installed: Python + pywebview ~40–60 MB, Pyodide + packages 34 MB, PDFium ~5 MB.
  Trimming unused Lucide icons and Pyodide packages before release keeps this down.
- **Code signing:** Windows Authenticode certificate (avoids SmartScreen warnings) and Apple Developer ID plus
  notarization (avoids Gatekeeper blocks). To be arranged with IT/Security once a working build exists. Until then,
  unsigned builds work but need "Run anyway" / right-click → Open.
- **CI:** GitHub Actions matrix (`windows-latest`, `macos-latest`) builds, signs (when secrets exist), and attaches
  artifacts to a GitHub Release on tag push. Each platform must be built on that platform.
- **Updates:** v1 checks a releases endpoint on launch and shows "Update available" with a download link. Silent
  auto-update is out of scope for now.
- **Developer run:** `uv run python -m space_agents --debug` (`--debug` enables webview devtools).
- **CI builds** use `uv sync --frozen` so release builds match `uv.lock` exactly.

---

## 7. Repository layout

Current tree. Entries marked *(planned)* don't exist yet.

```
space-agents/
  pyproject.toml               # metadata, core deps, ruff/pytest config; requires-python = ">=3.12,<3.13"
  requirements.txt             # install list for now (incl. dev + "maybe" deps); uv.lock later
  .python-version              # 3.12 (read by uv)
  .env.example                 # dev config template (copy to .env, git-ignored)
  PLAN.md, README.md, LICENSE (planned)
  docs/                        # how the pieces fit together (start at docs/README.md)
  space_agents/
    __main__.py                # `python -m space_agents`: run(), then hard-exit (see docs/architecture.md)
    main.py                    # CLI, logging, wiring, window; --smoke-test, --sandbox-selftest
    bridge.py                  # js_api surface exposed to the UI
    core.py                    # asyncio "core" thread + EventBus (batched UI events)
    server.py                  # WSGI app: ui/ files, sandbox worker CSP header, token-scoped workspace routes
    config.py, settings.py     # layered config (defaults → user → .env → env → CLI); settings.json
    defaults.json              # build-time org defaults (URLs baked in for releases)
    paths.py                   # resource dir (dev vs bundle), per-OS data/log dirs
    store.py                   # agents/*.json, avatars, SQLite schema + migrations + query helpers
    manifest.py                # dataclasses + validation
    catalog.py                 # tool-server catalog (dev file for now) + agent templates
    changes.py                 # proposed changes: stage, supersede, apply/discard/undo, conflicts, file history
    llm/client.py              # OpenAI-compatible chat (streaming, tools, param-compat, retries)
    runtime/
      loop.py                  # AgentRunner: the tool-calling loop, history (with change outcomes), events
      prompts.py               # system prompt
      dummy.py                 # placeholder replies when no model is configured
      scheduler.py             # (planned) intervals, active hours, jitter, missed-run handling
    tools/
      registry.py              # tool specs + execution; run_python guardrails; xlsx diff/repair script
      access.py                # FolderAccess: realpath containment, deny-list, read vs write
      builtin.py               # (planned) post_report, remember, recall, current_time
    documents/
      extract.py               # PDF (pypdfium2) / DOCX / XLSX / PPTX / text → pages, page classification
      library.py               # doc cache (by sha256), FTS5 search, read with page ranges
    sandbox/
      pyodide.py               # host side: workspaces, run tokens, aliases, timeouts, outputs
      selftest.py              # --sandbox-selftest checks
    sources/                   # (planned) MCP (Streamable HTTP) + OpenAPI tool sources, catalog fetch
    attachments.py             # (planned) snapshot dropped/uploaded files, pre-extract
    platform/
      appearance.py            # OS dark mode, open folder in Finder/Explorer
      autostart.py, notify.py, single_instance.py   # (planned)
  ui/
    index.html                 # CSP + import map
    css/tokens.css, app.css
    js/
      main.js, app.js, api.js, state.js
      sandbox.js               # main-thread side of the sandbox (owns the Web Worker)
      views/                   # Sidebar, Channel, AgentEditor, SettingsDialog, Welcome (+ Activity placeholder)
      components/              # common.js, AvatarPicker.js, Activity.js (steps + change cards + deny box)
      lib/                     # theme, markdown, emoji, util
    sandbox/worker.js          # Pyodide Web Worker: load packages, copy-in, run, copy-out
    vendor/                    # preact, htm, signals, marked, purify, lucide/ (committed, hashed)
    vendor/pyodide/            # Pyodide runtime + allowed wheels (git-ignored; fetched by scripts/vendor.py)
  scripts/
    vendor.py                  # download + verify pinned front-end files and Pyodide (stdlib only)
    update_csp.py              # keep the CSP hash in sync with the import map
  templates/agents/            # starter agent manifests
  dev/
    catalog.json               # offline stand-in for GET /mcp
    make_sample_docs.py        # realistic test documents (also used as test fixtures)
  tests/                       # pytest: unit, bridge, loop (scripted model + fake sandbox), static UI checks
  packaging/                   # (planned) PyInstaller spec, Inno Setup script, macOS Info.plist/icons
  .github/workflows/           # (planned) ci.yml, release.yml
```

---

## 8. Roadmap

Each phase ends with something usable and demoable.

### Phase 0: Skeleton and de-risking: ✅ skeleton done (2026-10-01); packaging spike and CI still open

Done:
- Native window and Slack-style UI in light and dark mode.
- Agent create/edit/delete with the full manifest editor, emoji or picture avatars, and templates.
- Placeholder chat replies streamed through the real event pipeline.
- Layered config with `.env`, an offline `dev/catalog.json`, and `--smoke-test`.
- 44 tests.
- Docs in `docs/`.

Deviation: dependencies install from `requirements.txt` for now (offline-friendly); `uv.lock` comes later.

Original scope:
- Repo scaffolding: `uv` project (`pyproject.toml`, `uv.lock`, `.python-version` = 3.12), ruff, pytest, CI on
  Windows + macOS.
- `scripts/vendor.py` and the vendored Preact/htm/marked/DOMPurify files, wired up with an import map.
- pywebview window loads `ui/index.html` with a Preact-rendered Slack-style shell. Bridge round-trip (`ping`) works,
  and the event push path works.
- Core asyncio thread, paths, settings, SQLite schema v1.
- **Packaging spike:** produce an unsigned Windows `.exe` and macOS `.app` from CI and confirm they launch on a clean
  machine, ideally including a corporate-managed Windows laptop to check antivirus and WebView2. Doing this early
  catches deployment surprises before they're expensive.

**Exit:** double-click app opens the shell UI on both OSes.

### Progress (2026-10-05): vertical slice of Phases 1, 2a, and 2b working

Verified end to end in the real UI with gpt-5-nano:
1. The agent reads a supplier PDF and updates a PO tracker spreadsheet.
2. The user reviews the cell-level change card and clicks Apply; the real file is updated with its formatting intact.
3. Discarding with feedback makes the agent revise, and Undo restores the previous version.

A typical request costs ~10–40k tokens. There are 86 automated tests, plus `--sandbox-selftest` (9 checks) in the real
webview.

**Lessons from live runs.** Small models need guardrails (details in §2.9 and `docs/documents-and-sandbox.md`):
- **File paths:** paths in code are auto-mapped into the sandbox.
- **Revisions:** chained edits build on and replace the agent's own pending change.
- **Feedback:** exact diff feedback goes back to the model, and "no changes" is reported.
- **Dates:** text dates are repaired.
- **Formatting:** formatting loss is flagged.
- **Where to put instructions:** small models follow instructions placed in tool results more reliably than ones in
  the system prompt (e.g. "point the user to the change card").

**Deviations from the plan:**
- The tool is named `run_python`, not `analyze_files` (clearer to models).
- Structured writers (`append_rows`, …) are deferred; `run_python` plus guardrails was enough.
- Extraction runs on a dedicated thread; a worker process is planned hardening.
- The UI is served by our own small WSGI app instead of pywebview's static server (needed for the worker CSP header
  and the sandbox routes).
- There's no `ExecutionBackend` abstraction yet; `PyodideSandbox` is the only backend.

### Phase 1: Chat with an agent
- ⬜ Onboarding:
  - paste the API key (→ keychain)
  - endpoints pre-filled from build defaults + remote config
  - connection test with the tool-calling probe
  - model list from `GET /models`

  Today the API key and model list come from `.env`.
- ✅ Agent CRUD with the form-based manifest editor (name, avatar, instructions, model).
- ✅ Streaming chat in the channel view, sanitized Markdown, message persistence, Stop.
- ✅ Sidebar with agents, status, last message preview, unread badges.

**Exit:** a user can create an agent and chat with it against the company AI server. Met with an OpenAI-compatible
endpoint; still to verify against the company gateway.

### Phase 2a: Tools, data sources, and documents (read)
- ✅ Tool registry and the agent loop with tool calling; tool errors returned to the model.
- ✅ Filesystem read tools (`list_files`) with the folder policy and deny-list. Native folder picker; read vs
  read & write per folder.
- **Local document pipeline:**
  - ✅ `pypdfium2` and OOXML extraction with page classification
  - ✅ `doc_cache` + FTS5
  - ✅ `read_document` / `search_document`
  - ⬜ worker process
  - ⬜ `list_pages`
  - ⬜ idle-time indexing
  - ⬜ cache size cap
- ⬜ **Attachments:** paperclip + drag-and-drop, snapshots, pre-extraction with progress chips, `attachment://` grants.
- ⬜ Tool sources: `McpSource` (Streamable HTTP) and `OpenApiSource`, catalog fetch plus auto-detection, per-agent
  tool selection, read-only policy. The document server is wired up as a regular tool. (The catalog and editor UI
  already work from `dev/catalog.json`.)
- ✅ Tool-call rows ("activity steps") in the timeline with details.
- ⬜ A per-agent audit log view.

**Exit:** a chat agent can answer:
- "What POs were delivered this week?" from Costpoint (⬜, needs MCP)
- "Summarize the PDFs in my Downloads" (✅)
- questions about a 1,000-page PDF dropped into chat, with page citations, in seconds (⬜ attachments; search is ✅)

### Phase 2b: Sandbox and proposed changes (write)
1. ✅ **Spike:**
   - Pyodide in a Web Worker loading vendored wheels offline
   - `.xlsx` round-trip
   - token-scoped file routes
   - worker CSP (including the WebKit `worker-src` quirk)
   - cold start ~3 s

   Go on macOS; ⬜ repeat on Windows/WebView2.
2. ✅ The Pyodide sandbox and `run_python` with small-model guardrails. ⬜ The `ExecutionBackend` interface.
   Structured writers are deferred.
3. **Proposed changes:**
   - ✅ staging
   - ✅ Excel cell diffs and text diffs
   - ✅ conflict detection
   - ✅ atomic apply
   - ✅ `file_versions` with Undo and 30-day retention
   - ✅ the `file_changes` policy and editor UI
   - ✅ Discard with feedback
   - ✅ supersede within a reply
   - ✅ outcomes in history
   - ⬜ Word diffs
   - ⬜ the read-only folder permission card
4. ✅ Plain-language activity steps with "Show details" for code. ⬜ Inline charts (matplotlib figures as images).

**Exit:** "Add today's deliveries to my PO Tracker spreadsheet" produces a reviewable change card, applying it updates
the file, and Undo restores the old version. ✅ Met.

### Phase 3: Autonomous agents
- Scheduler (intervals, active hours, weekdays, jitter, missed-run catch-up, concurrency cap) plus "Run now".
- `task` field, `post_report`, report cards, Activity inbox, unread badges, sidebar status/snippets.
- Memory (`remember`/`recall`) and recent-run context.
- ✅ (pulled forward) **Desktop notifications** for approvals: "<agent> requests approval" plus a one-sentence
  summary. They're skipped when you're viewing that agent, and respect the agent's toggle. macOS uses `osascript`,
  Windows a PowerShell toast (untested on Windows yet).
  - ⬜ Run-report notifications.
  - ⬜ Clicking a notification opens the agent. This needs the packaged app's own notification identity
    (UNUserNotificationCenter on macOS, an AppUserModelID on Windows).
- Start at login, close-to-background, single instance.

**Exit:** the Purchase Order Manager example runs hourly on its own and notifies the user about deliveries and supplier
delays with suggested next actions.

### Phase 4: Non-technical polish
- **New Agent wizard:**
  1. Describe what you want in plain English.
  2. "Draft it for me": the model proposes name, instructions, task, MCP servers, and schedule as a manifest.
  3. Review data sources and folders.
  4. Schedule.
  5. Test run with live output.
- Template gallery (Purchase Orders, Jira triage, Meeting prep, Weekly status report, Document summarizer).
- Import/export agents as `.json` to share with coworkers (no secrets inside).
- Friendly error messages ("Your API key was rejected. Check Settings → API key").
- Accessibility pass (keyboard navigation, focus states, contrast).

### Phase 5: Distribution and hardening
- Signed Windows installer and notarized macOS dmg via CI. Update check.
- System tray (Windows) / menu bar (macOS) icon with status and quick actions. Needs care because pywebview and
  tray libraries both want the main thread on macOS.
- Performance: long-history virtualization in the timeline, SQLite indices, log rotation.
- Docs: user guide (with the "keep the app running / start at login" guidance), admin guide (catalog format, server
  flags for tool calling), contributor guide.

### Later / ideas
- External actions (send email/Teams, update Jira) behind **per-action human approval**: the agent proposes, the user
  clicks Approve in the channel. The `permissions.external_actions` manifest field reserves this.
- **More execution backends** behind the same `ExecutionBackend` interface: Docker/Podman for power users on
  IT-managed machines, or short-lived server-side sandboxes for heavy jobs. Same rules: copies in, proposals out.
- **Skills:** packaged guidance the model can load, e.g. "if local extraction comes back empty or garbled, use the
  document server's `convert_document`", or how to build a specific report format.
- **Smarter document routing:** automatically send scanned pages to the GPU document server and merge the results
  into the cache.
- Responses API backend; multiple model fallbacks.
- MCP resources and prompts; local stdio MCP servers.
- Agent-to-agent handoff (e.g. PO Manager asks Report Writer to produce the weekly summary).
- Event triggers (folder watch: "when a new file lands in Downloads/Invoices…").

---

## 9. Testing strategy

**Built** (`pytest`, 86 tests, no window or network needed):
- **Unit tests:**
  - manifest validation
  - config layers and `.env` parsing
  - store and migrations
  - folder policy: traversal, symlinks, deny-list, read vs write
  - document extraction (text PDF, scanned page, DOCX, XLSX with dates, PPTX, unsupported/corrupt)
  - cache and FTS5 search (extract once per version, rename detection, page ranges)
  - proposed changes: auto-apply policy, apply, undo, conflicts, discard, retention
  - the xlsx diff/repair script, run on the host with openpyxl
- **LLM client** against `httpx.MockTransport`:
  - streamed text and tool calls
  - usage
  - dropping rejected params
  - non-streaming fallback
  - auth/model errors
  - retries
- **Agent loop** with a scripted model (`ScriptedClient`) and a fake sandbox:
  - reading, then answering
  - `run_python` outputs becoming pending/applied changes
  - access errors returned to the model
  - read-only folders
  - the tool budget
  - path literals becoming inputs
  - no-op detection
  - superseding revisions
  - change outcomes in history
- **Bridge:** the same calls the UI makes (agents CRUD, avatars, chat, discard with feedback).
- **Static UI checks:**
  - the CSP hash matches the import map
  - vendored files match the lock
  - imports resolve
  - icon names exist
  - templates reference known servers
- **Fixtures:** `dev/make_sample_docs.py` (PO tracker, supplier PDFs, a scan, DOCX, PPTX) via the `sample_docs`
  fixture.

**In the real webview** (run on each OS; CI-ready):
- `--smoke-test`: the UI renders with no JS errors.
- `--sandbox-selftest`:
  - openpyxl and pandas round-trips
  - in-place edits via aliases
  - no network
  - no host disk
  - timeout kill and recovery

**Planned:**
- scheduler math (active hours, jitter, sleep/wake catch-up)
- OpenAPI → tool conversion with real specs as fixtures
- fake MCP/OpenAPI/document servers
- extraction timeouts once it runs in a worker process
- attachments
- Windows path quirks (drive letters, UNC, case-insensitivity)
- **Integration (opt-in):** env-var-gated tests against the real AI server and MCP servers.
- **CI:** ruff + pytest + `--smoke-test` + `--sandbox-selftest` on Windows and macOS for every PR; a packaging smoke
  build on tags.
- **Manual QA checklist** per release: fresh install, onboarding, create agent from template, scheduled run,
  notification, start at login, upgrade over previous version.

---

## 10. Decisions and open items

### Decided

| Topic | Decision |
|---|---|
| Name | **Space Agents** |
| Python | 3.12, managed with uv |
| UI | pywebview + Preact/htm (vendored, no npm), served by our small WSGI app on 127.0.0.1 |
| Server discovery | `GET {mcp_base_url}/mcp` catalog with the user's key (server side to be added; format proposed in 2.6) |
| Server types | Streamable HTTP MCP at `/mcp/{id}`; OpenAPI with the spec at `/mcp/{id}/openapi.json` |
| PDF speed | Measured: `pypdfium2` extracts ~460 text-heavy pages/s on an M-series Mac, so a 1,000-page PDF takes ~2 s |
| Document processing | **Local first:** `pypdfium2` + stdlib OOXML, cached, with FTS5 search. The GPU document server (`convert_document`, VLM optional) stays available to agents for scans and hard cases; automatic routing comes later (skills) |
| Code execution | **Pyodide** in a Web Worker: sandboxed Python (`run_python`), copies in, proposals out. Docker rejected as the default (admin rights, licensing, weight) |
| Sandbox warm-up | Preload at launch by default (`full`, ~420 MB, the same memory first use would cost anyway); `light`/`off` for low-memory machines |
| Small-model guardrails | Make the sandbox meet models halfway (path mapping, revisions, diff feedback, date repair) rather than relying on prompts alone; put key instructions in tool results |
| File writes | **Proposed changes:** new files are allowed by default; edits to existing files need approval by default; every overwrite is versioned; no deletes. Discard can carry feedback that the agent acts on |
| Attachments | Drag-and-drop or upload in chat; snapshotted into app data; per-file read grant for that agent |
| Identity | One API key per user; M365 servers do Azure OAuth + OBO server-side; no sign-in flow in the app |
| Tool execution | Client-side tool loop: the app runs all tool calls; the gateway/vLLM only generates (see 3.2) |
| Models | Large open models (e.g. Inkling Large, GLM 5.3) with up to 1M context. For testing: OpenAI `gpt-5-nano` via `.env`. The first entry in the model list is the default for agents without one |
| Endpoints | `llm_base_url` and `mcp_base_url` configured separately: build defaults → remote config → user override |
| Code signing | Handled with IT/Security once a working build exists |
| Git | The maintainer makes commits; tooling/assistants don't commit |

### Still open

1. **Catalog and remote config formats:** confirm or adjust the proposed `/mcp` catalog response (2.6) and the remote
   config document and its path (2.5) before implementing the server side.
2. **Document server spec:** argument names for `convert_document`, how file content is passed (base64, multipart,
   URL), size limits, and page selection. Needed when we wire it up as a regular tool (Phase 2a); it's no longer on
   the critical path.
3. **Gateway tool-calling passthrough:** confirm `/api/chat/completions` forwards `tools`, `tool_choice`, and
   `parallel_tool_calls`, and streams `tool_calls` deltas, and that each served model has a `--tool-call-parser` set.
   The connection-test probe (2.5) will verify this per model.
4. **Context length in `/api/models`:** does the gateway expose each model's `max_model_len`? If not, a small
   per-model override map in the remote config works.
5. **License:** Apache-2.0 (includes a patent grant, common for org-backed projects) or MIT.
6. **Sandbox on Windows:** run `--sandbox-selftest` in WebView2. On macOS (WKWebView) all checks pass, cold start is
   ~3 s, and we serve the UI from our own WSGI app.
7. **FTS5 availability:** ✅ confirmed on macOS (uv's CPython 3.12, SQLite 3.53.1). Still to confirm on Windows; if
   missing, fall back to a simple LIKE scan.

---

## 11. Definition of done for the MVP (end of Phase 3)

A non-technical user on Windows or macOS can:
1. Download and install the app and open it from an icon.
2. Paste the API key they were given, and see models and MCP servers appear.
3. Create a "Purchase Order Manager" agent by filling in a form: purpose, Costpoint + Outlook, a PO folder (read-only)
   and a reports folder (writable), hourly on weekdays.
4. Leave it running (with start-at-login on) and get notified about deliveries and supplier updates, each with
   suggested next actions and links to sources.
5. Click into the agent and ask follow-up questions in chat, including dropping in a 300-page PDF and getting an
   answer with page citations in seconds.
6. Ask the agent to update a tracking spreadsheet, review the proposed change, apply it, and restore the previous
   version if needed.
7. See exactly which tools the agent called, and know it cannot send email, change records, delete anything, or touch
   files outside the folders and attachments they granted.
