# Architecture

## Processes and threads

One process. pywebview owns the main thread (both WebView2 and Cocoa require it); everything async runs on a single
asyncio loop in a background thread.

```
SpaceAgents (python)
├── main thread ─────── pywebview GUI loop ── native window ── WebView2 / WKWebView
│                                                   │  loads ui/ from our WSGI app (server.py) on
│                                                   │  127.0.0.1: static files + sandbox file routes
│                                                   └─ Web Worker: Pyodide sandbox (ui/sandbox/worker.js)
├── pywebview workers ─ one short-lived thread per JS → Python call (bridge.Api methods)
└── "space-core" ────── asyncio loop (core.Core)
                         ├── EventBus flusher: batches events to the UI every ~50 ms
                         └── agent runs (runtime/loop.py), LLM client, sandbox orchestration
document extraction ──── one dedicated thread (pypdfium2 isn't thread-safe)
```

## JS ↔ Python

**Calls (JS → Python).** `ui/js/api.js` → `call("method", ...args)` → `window.pywebview.api.method(...)` →
`space_agents/bridge.py::Api.method`. Every method is wrapped by `@endpoint` and returns an envelope:

```json
{ "ok": true, "data": ... }
{ "ok": false, "error": "Please fix the highlighted fields.", "fields": { "schedule.every_minutes": "Must be between 5 and 1440." } }
```

`call()` unwraps it: it resolves with `data`, or throws an `ApiError` carrying `fields`, which forms use to
highlight inputs. Only public methods are exposed; all `Api` state lives in `_underscore` attributes.

**Events (Python → JS).** Anything on any thread calls `events.emit("type", **data)`. The EventBus queues it; every
~50 ms the flusher merges consecutive `message.delta` events for the same message and makes **one**
`window.run_js("window.__spaceEvents([...])")` call. `state.js::handleEvent` applies them to signals. Delivery starts
only after the UI calls `ui_ready`, and events emitted earlier wait in the queue.

| Event | Payload | Effect in UI |
|---|---|---|
| `message.delta` | `agent_id, message_id, delta` | Append text to a streaming message |
| `message.done` | `agent_id, message` | Replace with the final message; bump unread if not viewing |
| `agent.status` | `agent_id, status` (`idle`/`responding`) | Status dot, "Responding" chip, Stop button |
| `tool.started` / `tool.done` | `agent_id, message_id, call` | Activity step (spinner → done/failed) |
| `change.updated` | `agent_id, message_id, change` | Proposed-change card (pending/applied/…) |

## Request flow: sending a chat message

1. The Composer calls `sendMessage()`, which calls `call("send_message", id, text)`.
2. `Api.send_message` validates the text, stores the user message, and stores an empty agent message with status
   `streaming`. It marks the agent busy, submits the reply to the core loop, and returns both messages immediately.
3. With a model configured (`llm_base_url` + `api_key`), `AgentRunner.chat` (`runtime/loop.py`):
   1. builds the system prompt and history
   2. streams the model's answer (`message.delta`)
   3. runs any tool calls (`tool.started`/`tool.done`, plus `change.updated` for files) and loops
   4. saves the final text and token usage (`message.done`)

   Without a model, `runtime/dummy.py` streams a placeholder.
4. "Stop" cancels the run's future; the message is saved as stopped.

## Request flow: reviewing a proposed change

Files an agent produces become *proposed changes* (`changes.py`; details in
[documents-and-sandbox.md](documents-and-sandbox.md)). The change card calls:

| Card action | Bridge call | Result |
|---|---|---|
| Apply / Apply anyway | `apply_change(id, force)` | Hash check → snapshot to `history/` → atomic write → `change.updated` |
| Discard… (no feedback) | `discard_change(id)` | Status `discarded`; staged file deleted |
| Discard & tell agent | `discard_change(id, feedback)` | As above, then the feedback is posted as the user's next message and a reply starts (same path as `send_message`) |
| Undo | `undo_change(id)` | Current file snapshotted, previous version restored |
| Show in folder | `reveal_change(id)` | Opens Finder/Explorer at the file |

The agent learns outcomes when its history is built: earlier replies are annotated with what happened to their
changes.

## Bridge endpoints

| Area | Methods |
|---|---|
| Lifecycle | `ping`, `ui_ready`, `bootstrap` |
| Agents | `list_agents`, `save_agent`, `duplicate_agent`, `delete_agent` |
| Chat | `list_messages` (with tool calls and changes), `send_message`, `stop_agent` |
| Changes | `apply_change`, `discard_change`, `undo_change`, `reveal_change` |
| Sandbox | `sandbox_done` (called by `ui/js/sandbox.js` when the worker finishes a run) |
| Files & settings | `pick_folder`, `set_theme`, `set_ui_pref` (allow-listed keys, e.g. `sidebar_width`), `save_endpoint_overrides`, `open_data_folder` |

## Code map

```
space_agents/
  __main__.py    `python -m space_agents`: run(), then flush logs and hard-exit (see Shutdown below)
  main.py        CLI args, logging, wiring, window creation, --smoke-test, --sandbox-selftest
  bridge.py      Api: every method the UI can call
  core.py        Core (asyncio thread) + EventBus (batched UI events)
  config.py      layered config resolution (see configuration.md)
  settings.py    settings.json (theme, endpoint overrides, UI prefs); atomic JSON writes
  manifest.py    agent manifest dataclasses + validation (see agents.md)
  store.py       agents/*.json, avatars/*.png, space.db schema/migrations + query helpers
  catalog.py     tool-server catalog (dev file for now) + agent templates
  paths.py       resource dir (dev vs PyInstaller bundle), per-OS data/log dirs
  defaults.json  build-time defaults (org URLs get baked in here)
  server.py      WSGI app: UI files, sandbox worker CSP header, token-scoped workspace routes
  llm/client.py  OpenAI-compatible chat (streaming, tools, param-compat, retries)
  runtime/       loop.py (agent loop), prompts.py, dummy.py (no-model placeholder)
  tools/         registry.py (tool specs + execution), access.py (folder policy)
  documents/     extract.py (PDF/Office/text → pages), library.py (cache + FTS5 search)
  sandbox/       pyodide.py (host side), selftest.py (--sandbox-selftest)
  changes.py     proposed changes, apply/discard/undo, file history
  platform/appearance.py     OS dark-mode detection, open folder in Finder/Explorer
ui/              front end (see ui.md)
templates/agents/            starter agent manifests
dev/catalog.json             offline stand-in for GET /mcp
dev/make_sample_docs.py      realistic test documents (also pytest fixtures)
tests/                       pytest suite (see development.md)
scripts/vendor.py            download + verify front-end libs
scripts/update_csp.py        keep the CSP hash in sync with the import map
```

## Persistence and crash safety

- **Manifests and settings** are written atomically (temp file + `os.replace`).
- **SQLite** runs in WAL mode behind a single lock-guarded connection. Schema migrations are a list in `store.py`,
  applied in order using `PRAGMA user_version`.
- **On startup**, messages left `streaming` (crash or quit mid-reply) are marked `error`. Leftover sandbox
  `workspaces/` are deleted, and file history older than 30 days is pruned.
- **Proposed changes** are applied with a hash check, a snapshot of the previous version, and a temp file +
  `os.replace`, so a crash can't leave a half-written user file.

| Table | Contents |
|---|---|
| `messages` | Chat history (+ token usage per agent reply) |
| `tool_calls` | Every tool call: name, arguments, summary, truncated result, status, duration (the audit trail) |
| `changes` | Proposed changes and their status |
| `file_versions` | Previous versions of overwritten files (blobs in `history/`) |
| `doc_cache`, `doc_pages`, `doc_fts` | Extracted document text by content hash, plus the FTS5 search index |

## Shutdown

`run()` stops the core loop, closes the document executor and the database, and returns. `__main__.py` then flushes
logs and calls `os._exit`. pywebview runs bridge calls on non-daemon threads that wait for the window to acknowledge
their return value, so a call that was in flight when the window closed would otherwise block interpreter exit
forever.
