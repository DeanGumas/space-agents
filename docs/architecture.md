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
   `streaming`. It marks the agent busy, submits `dummy_reply(...)` to the core loop, and returns both messages
   immediately.
3. With a model configured (`llm_base_url` + `api_key`), `AgentRunner.chat` (`runtime/loop.py`):
   1. builds the system prompt and history
   2. streams the model's answer (`message.delta`)
   3. runs any tool calls (`tool.started`/`tool.done`, plus `change.updated` for files) and loops
   4. saves the final text and token usage (`message.done`)

   Without a model, `runtime/dummy.py` streams a placeholder.
4. "Stop" cancels the run's future; the message is saved as stopped.

## Code map

```
space_agents/
  main.py        entry point: CLI args, logging, wiring, window creation, --smoke-test
  bridge.py      Api: every method the UI can call
  core.py        Core (asyncio thread) + EventBus (batched UI events)
  config.py      layered config resolution (see configuration.md)
  settings.py    settings.json (theme, endpoint overrides); atomic JSON writes
  manifest.py    agent manifest dataclasses + validation (see agents.md)
  store.py       agents/*.json, avatars/*.png, space.db (messages)
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
scripts/vendor.py            download + verify front-end libs
scripts/update_csp.py        keep the CSP hash in sync with the import map
```

## Persistence and crash safety

- **Manifests and settings** are written atomically (temp file + `os.replace`).
- **SQLite** runs in WAL mode behind a single lock-guarded connection. Schema migrations are a list in `store.py`,
  applied in order using `PRAGMA user_version`.
- **On startup**, messages left `streaming` (crash or quit mid-reply) are marked `error`.
