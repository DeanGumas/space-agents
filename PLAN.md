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
4. **Read-only by default.** Agents can read from MCP servers and allowed folders. Writing is limited to folders the user
   explicitly marks writable. No outbound actions (emails, Teams messages, etc.) in v1, but the design leaves room
   for them later behind human approval.
5. **Local-first.** Agents run on the user's machine while the app is open. No central agent hosting. Model inference
   and MCP calls go to the organization's own servers, so data stays on the company network.
6. **Lightweight and cross-platform.** Windows is the primary target and macOS is fully supported. No Node/npm. Minimal Python
   dependencies. Users get one clickable app icon, distributed as an installer.

### Non-goals (v1)

- Agents that act externally (sending email/Teams, updating Jira/Costpoint records).
- Running agents while the computer is off or asleep, or any cloud/server-side agent hosting.
- Multi-user collaboration or shared agents across machines (export/import of manifests is enough for now).
- A plugin marketplace or arbitrary code/shell execution by agents.

---

## 2. Architecture

### 2.1 Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 (dev and bundled) | Best agent/MCP ecosystem; mature package support; fast iteration |
| Dev tooling | [uv](https://docs.astral.sh/uv/) | Manages the Python version and venv; `pyproject.toml` + `uv.lock` give reproducible installs |
| Window | [pywebview](https://pywebview.flowrl.com/) | Native window using the OS webview (Edge WebView2 on Windows, WKWebView on macOS); no browser, no Chromium bundle |
| UI | HTML + CSS + [Preact](https://preactjs.com/) + [htm](https://github.com/developit/htm), vendored ES modules | React-style components and hooks with **no build step and no npm** (see 2.4) |
| UI ↔ Python | pywebview `js_api` bridge | JS calls Python directly (`await pywebview.api.x()`); Python pushes events via `evaluate_js`. No Flask or app HTTP API |
| HTTP | `httpx` | Async, streaming, HTTP/2-capable; used for both LLM and MCP |
| TLS | `truststore` | Uses the OS certificate store so internal servers signed by a corporate CA just work |
| Secrets | `keyring` | API keys go in Windows Credential Manager / macOS Keychain, never in plain files |
| Storage | `sqlite3` (stdlib) + JSON files | Chat history, runs, audit log in SQLite; agent manifests as portable JSON |
| Packaging | PyInstaller (onedir) → Inno Setup (Win) / `.dmg` (macOS) | One clickable icon; users don't install Python |

**Runtime dependencies (target):** `pywebview`, `httpx`, `truststore`, `keyring`.
pywebview pulls in `pythonnet` on Windows and `pyobjc` on macOS automatically.
**Dev-only:** `pytest`, `pytest-asyncio`, `ruff`, `pyinstaller`.
**Front-end (vendored, no npm):** `preact`, `preact/hooks`, `htm`, `marked`, `DOMPurify`. That's about 5 files in total.

We deliberately **do not** depend on:
- the `openai` SDK: we only need a few endpoints, so `httpx` is enough.
- the official `mcp` SDK: it pulls in pydantic, starlette, uvicorn, etc. that a client-only app doesn't need. We
  implement thin MCP and OpenAPI clients behind one interface (see 2.6) and can swap in the official SDK later if the
  protocol surface grows.
- a local document-parsing stack: the organization's document-processing server handles PDFs, Office files, and scans
  (see 2.7).

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
│     ├── Tool registry       – filesystem, document readers, built-ins, MCP tools          │
│     └── Store               – SQLite (single writer), manifests on disk                   │
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
| App data | `%APPDATA%\SpaceAgents\` | `~/Library/Application Support/SpaceAgents/` |
| Logs | `%LOCALAPPDATA%\SpaceAgents\logs\` | `~/Library/Logs/SpaceAgents/` |

```
SpaceAgents/
  settings.json          # endpoint overrides (no secrets), cached remote config + catalog, UI prefs, autostart flag
  agents/
    po-manager.json      # one manifest per agent (shareable; contains no secrets)
  space.db               # SQLite: messages, runs, tool_calls (audit), notifications, agent_memory
```

The API key is stored in the OS keychain under service `SpaceAgents`.

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
- **Serving the files:** pywebview serves `ui/` from its built-in static file server on `127.0.0.1` (ES modules can't
  load from `file://`). That server only serves our static UI files. Every app action goes through the `js_api`
  bridge, so nothing sensitive is reachable over HTTP.

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
  last-activity snippet and relative time, unread badge. "Activity" is a cross-agent inbox of notifications.
- **Channel view:** a single timeline mixing chat messages and run-report cards. Tool calls are collapsed by default
  ("▸ 6 tool calls") and expand to show server/tool, arguments, duration, and a truncated result.
- **Agent settings drawer:** form-based manifest editor (Basics, Instructions, Model, Data sources, Files, Schedule,
  Notifications), with an "Advanced: view JSON" tab.
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

### 2.6 Tool sources (MCP and OpenAPI servers)

The organization's servers come in two flavors, and we support both behind one interface:

- **Streamable HTTP MCP** at `{mcp_base_url}/mcp/{id}`
- **OpenAPI** with the spec at `{mcp_base_url}/mcp/{id}/openapi.json`

 **Users never see the difference.** They pick "Costpoint" or "Outlook" from a list, and the transport is
resolved from the catalog or auto-detected.

```python
class ToolSource(Protocol):
    id: str
    async def list_tools(self) -> list[ToolSpec]: ...     # name, description, JSON schema, read_only flag
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
- `role: "document_processing"` tells the app which server backs `read_document` (see 2.7), so its id isn't hardcoded.
- The catalog is cached and refreshed on launch and from Settings ("Refresh servers").
- Fallback, if the catalog is unavailable or for testing unlisted servers: the user adds a server by id under Advanced,
  and we **auto-detect** the type. We try `/mcp/{id}/openapi.json` first, then an MCP `initialize` at `/mcp/{id}`.

**Common behavior**
- Tools are namespaced for the model as `{server}__{tool}` (OpenAI function names allow `[a-zA-Z0-9_-]{1,64}`).
- Tool lists are cached per session. Connections are opened lazily on an agent's first use.
- Each source is a small module, so we can swap `McpSource` for the official SDK if the protocol surface grows.

### 2.7 Tools available to agents

| Tool group | Tools | Notes |
|---|---|---|
| Files (read) | `list_dir`, `find_files` (glob/name), `search_text`, `read_file`, `file_info` | Only inside granted folders |
| Files (write) | `write_file`, `append_file`, `make_dir` | Only inside folders granted **read-write**; no delete in v1; overwrites keep a `.bak` |
| Documents | `read_document` | Any local file in a granted folder → plain text via the document-processing server (below) |
| Org servers | whatever the selected MCP/OpenAPI servers expose | Read-only tools only by default (see §4) |
| Built-in | `post_report`, `remember`, `recall`, `current_time` | `post_report` is how a scheduled run delivers its result |

Tool results are truncated (configurable, e.g. 12k chars) with a note telling the model how to page or narrow the query.

**Document-processing server (always on).** The organization's document-processing server is an MCP-style server
(identified in the catalog by `role: "document_processing"`) whose `convert_document` tool turns any document (PDF,
Office files, images and scans via VLM) into plain text. It is attached to **every agent automatically**, does not
appear in the manifest, and can't be removed.
- `read_document(path, pages?, use_vlm?)` is a built-in tool:
  1. Sandbox-check the path.
  2. Read plain-text types (`.txt`, `.md`, `.csv`, `.json`, `.log`, …) locally.
  3. Send everything else to `convert_document`.
  4. Return text, paginated for large documents.

  `use_vlm` maps to the server's VLM option, so the model can ask for vision processing of scans and drawings when
  plain extraction comes back empty. Exact argument names and how file bytes are passed will follow the server's spec
  (to be provided in Phase 2).
- **Extraction cache:** results are cached in SQLite keyed by `(path, size, mtime, sha256)`. An hourly agent
  re-reading the same folder doesn't reprocess unchanged files, which matters with VLM-backed extraction.
- The server's own tools (e.g. converting a document fetched from SharePoint by URL) are also exposed to agents as a
  regular source.
- Limits: max file size (e.g. 50 MB, configurable) and a per-run cap on documents sent, so that a "read my whole
  Downloads folder" request can't swamp the server.
- Files only ever go to the organization's own document server, the same trust boundary as the AI models.

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

  // Forward-compatible: external actions will require approval. Unused in v1.
  "permissions": { "external_actions": "never" }                  // never | ask | allow
}
```

Validation uses plain dataclasses plus hand-written checks (no pydantic). Manifests are versioned and migrated on load.

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
   N run reports (so the agent knows what it already reported).
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
6. **Context management:** a rough token estimate (chars/4) is checked against the working budget, capped by the
   model's context length. Old tool results are elided first, then older turns are summarized.

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
   - Writes only to `read_write` roots, with size limits, no deletes, and `.bak` on overwrite.
2. **Server tool policy (MCP and OpenAPI).**
   - By default only tools determined to be read-only are exposed (see 2.6 for how each source type decides). Anything
     else is hidden unless a future approval mode is enabled.
   - **Recommended:** enforce read-only server-side too. For example, the M365 servers could request read-only Graph
     scopes in the OBO exchange, and write tools could be omitted from the catalog for keys not entitled to them. The
     client-side policy is a guardrail against model mistakes, not a security boundary.
3. **No code or shell execution tools** in v1.
4. **Untrusted content handling.** Tool results are wrapped in clear delimiters, and the system prompt states that
   content from tools is data, not instructions. With no outbound action tools in v1, the blast radius of a successful
   injection is limited to writes inside user-approved folders.
5. **UI hardening.** All model output is sanitized with DOMPurify and the page has a strict CSP. Links open in the
   system browser, never inside the app webview.
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

```
space-agents/
  app.py                       # entry point (thin: calls space_agents.main.run())
  pyproject.toml               # deps, ruff, pytest config; requires-python = ">=3.12,<3.13"
  uv.lock
  .python-version              # 3.12 (read by uv)
  PLAN.md
  README.md
  LICENSE
  space_agents/
    __init__.py
    __main__.py
    main.py                    # create window, start core thread, lifecycle, single-instance
    bridge.py                  # js_api surface exposed to the UI; event flusher
    paths.py                   # per-OS app data/log locations
    settings.py                # layered endpoint config (defaults → remote → user), settings.json, keyring access
    defaults.json              # build-time org defaults: llm_base_url, mcp_base_url, remote config path
    store.py                   # SQLite schema, migrations, queries
    manifest.py                # dataclasses, validation, migrations, templates
    llm/
      client.py                # OpenAI-compatible chat completions (stream, tools, retries)
    sources/
      base.py                  # ToolSource protocol, ToolSpec/ToolResult, shared httpx client
      mcp.py                   # Streamable HTTP MCP client
      openapi.py               # OpenAPI spec → tools, $ref inlining, calls
      catalog.py               # catalog fetch, manual add, type auto-detection
    tools/
      registry.py              # builds per-agent tool list, dispatch, truncation, audit
      filesystem.py            # sandboxed file tools
      documents.py             # read_document → document server, extraction cache
      builtin.py               # post_report, remember, recall, current_time
    runtime/
      loop.py                  # the agent tool-calling loop
      runner.py                # run lifecycle, cancellation, concurrency limits
      scheduler.py             # intervals, active hours, jitter, missed-run handling
      prompts.py               # base system prompt and context assembly
    platform/
      autostart.py             # Windows Run key / macOS LaunchAgent
      notify.py                # desktop notifications
      single_instance.py
  ui/
    index.html
    css/app.css
    js/
      main.js                  # boot, render <App/>, event dispatch
      api.js                   # wrapper over window.pywebview.api + event subscription
      state.js                 # app-wide store (agents, messages, unread) fed by bridge events
      views/Sidebar.js, Channel.js, AgentSettings.js, AppSettings.js, Wizard.js, Activity.js
      components/…             # Message, ReportCard, ToolCallRow, form controls
    vendor/                    # preact, preact/hooks, htm, marked, purify (pinned, hashed)
    icons/                     # Lucide SVG subset
  scripts/
    vendor.py                  # download + verify pinned front-end files (stdlib only)
  templates/
    agents/purchase-order-manager.json, jira-triage.json, meeting-prep.json, …
  packaging/
    pyinstaller/space_agents.spec
    windows/installer.iss, icon.ico
    macos/Info.plist, icon.icns, dmg settings
  tests/
  .github/workflows/ci.yml, release.yml
```

---

## 8. Roadmap

Each phase ends with something usable and demoable.

### Phase 0: Skeleton and de-risking
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

### Phase 1: Chat with an agent
- Onboarding: paste the API key (→ keychain); endpoints come pre-filled from build defaults + remote config;
  connection test; model list; `defaults.json` and remote-config loading.
- Agent CRUD with the form-based manifest editor (name, avatar, instructions, model).
- Streaming chat in the channel view, Markdown rendering (sanitized), message persistence, stop/cancel.
- Sidebar with agents, status, last message preview.

**Exit:** a user can create an agent and chat with it against the company AI server.

### Phase 2: Tools and data sources
- Tool registry and the agent loop with tool calling.
- Filesystem tools with the sandbox and deny-list. Native folder picker (`create_file_dialog(FOLDER_DIALOG)`).
  Read vs read-write per folder.
- `read_document` via the document-processing server, with the extraction cache.
- Tool sources: `McpSource` (Streamable HTTP) and `OpenApiSource`, catalog plus auto-detection, per-agent server and
  tool selection, read-only policy.
- Tool-call rows in the timeline; per-agent audit log view.
- Connection test includes the tool-calling probe.

**Exit:** a chat agent can answer "what POs were delivered this week?" from Costpoint and "summarize the PDFs in my
Downloads".

### Phase 3: Autonomous agents
- Scheduler (intervals, active hours, weekdays, jitter, missed-run catch-up, concurrency cap) plus "Run now".
- `task` field, `post_report`, report cards, Activity inbox, unread badges, sidebar status/snippets.
- Memory (`remember`/`recall`) and recent-run context.
- Desktop notifications: macOS via `osascript` (`display notification`) for v1; Windows toast via a small optional
  dependency or PowerShell fallback. Clicking a notification opens the agent.
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
- **Code execution** (expected to become a must-have; design deferred until we can do it securely). Approaches to
  evaluate:
  - **Server-side sandbox:** short-lived containers on the AI server (e.g. gVisor/Firecracker) exposed as another
    MCP/OpenAPI tool source. Strongest isolation and nothing to install on laptops, but it uses server resources and
    local files must be uploaded.
  - **WebAssembly Python (Pyodide)** running in an isolated, bridge-less webview or worker: no filesystem or network
    by default, and inputs passed in explicitly. Light and local, but only covers pure-Python/numeric workloads.
  - **OS-level sandboxing** (macOS `sandbox-exec`, Windows AppContainer/Job Objects) around a bundled interpreter:
    most capable locally, but the most platform-specific work and the hardest to get right.

  Whichever we pick: no network by default, access limited to explicitly granted folders, CPU/memory/time limits, and
  per-run approval at first.
- Responses API backend; multiple model fallbacks.
- MCP resources and prompts; local stdio MCP servers.
- Agent-to-agent handoff (e.g. PO Manager asks Report Writer to produce the weekly summary).
- Event triggers (folder watch: "when a new file lands in Downloads/Invoices…").

---

## 9. Testing strategy

- **Unit tests:**
  - manifest validation and migrations
  - filesystem sandbox: traversal, symlinks, deny-list, Windows path quirks (drive letters, UNC paths,
    case-insensitivity)
  - scheduler math: active hours, jitter, sleep/wake catch-up
  - OpenAPI → tool conversion, using real specs from our servers as fixtures (`$ref` inlining, name sanitizing,
    read-only detection)
  - extraction cache invalidation
  - tool-result truncation
- **Fake servers in tests** (in-process `httpx.MockTransport`), so the agent loop is tested deterministically without
  a real model:
  - an OpenAI-compatible fake (scripted streaming + tool calls)
  - a fake MCP Streamable HTTP server
  - a fake OpenAPI server
  - a fake document server
- **Integration (opt-in):** env-var-gated tests against the real AI server and a real MCP server.
- **CI:** ruff + pytest on Windows and macOS for every PR; packaging smoke build on tags.
- **Manual QA checklist** per release: fresh install, onboarding, create agent from template, scheduled run,
  notification, start at login, upgrade over previous version.

---

## 10. Decisions and open items

### Decided

| Topic | Decision |
|---|---|
| Name | **Space Agents** |
| Python | 3.12, managed with uv |
| UI | pywebview + Preact/htm (vendored, no npm). pywebview's localhost static server is acceptable |
| Server discovery | `GET {mcp_base_url}/mcp` catalog with the user's key (server side to be added; format proposed in 2.6) |
| Server types | Streamable HTTP MCP at `/mcp/{id}`; OpenAPI with the spec at `/mcp/{id}/openapi.json` |
| Document processing | MCP-style server with `convert_document` (VLM optional); always attached to every agent |
| Identity | One API key per user; M365 servers do Azure OAuth + OBO server-side; no sign-in flow in the app |
| Tool execution | Client-side tool loop: the app runs all tool calls; the gateway/vLLM only generates (see 3.2) |
| Models | Large open models (e.g. Inkling Large, GLM 5.3) with up to 1M context; no default model for now |
| Endpoints | `llm_base_url` and `mcp_base_url` configured separately: build defaults → remote config → user override |
| Code signing | Handled with IT/Security once a working build exists |
| Git | The maintainer makes commits; tooling/assistants don't commit |

### Still open

1. **Catalog and remote config formats:** confirm or adjust the proposed `/mcp` catalog response (2.6) and the remote
   config document and its path (2.5) before implementing the server side.
2. **Document server spec:** argument names for `convert_document`, how file content is passed (base64, multipart,
   URL), size limits, and page selection. Needed for Phase 2.
3. **Gateway tool-calling passthrough:** confirm `/api/chat/completions` forwards `tools`, `tool_choice`, and
   `parallel_tool_calls`, and streams `tool_calls` deltas, and that each served model has a `--tool-call-parser` set.
   The connection-test probe (2.5) will verify this per model.
4. **Context length in `/api/models`:** does the gateway expose each model's `max_model_len`? If not, a small
   per-model override map in the remote config works.
5. **License:** Apache-2.0 (includes a patent grant, common for org-backed projects) or MIT.

---

## 11. Definition of done for the MVP (end of Phase 3)

A non-technical user on Windows or macOS can:
1. Download and install the app and open it from an icon.
2. Paste the API key they were given, and see models and MCP servers appear.
3. Create a "Purchase Order Manager" agent by filling in a form: purpose, Costpoint + Outlook, a PO folder (read-only)
   and a reports folder (writable), hourly on weekdays.
4. Leave it running (with start-at-login on) and get notified about deliveries and supplier updates, each with
   suggested next actions and links to sources.
5. Click into the agent and ask follow-up questions in chat.
6. See exactly which tools the agent called, and know it cannot send email, change records, or touch files outside the
   folders they granted.
