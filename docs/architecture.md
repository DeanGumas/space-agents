# Architecture

## Processes and threads

One process. pywebview owns the main thread (both WebView2 and Cocoa require it); everything async runs on a single
asyncio loop in a background thread.

```
SpaceAgents (python)
├── main thread ─────── pywebview GUI loop ── native window ── WebView2 / WKWebView
│                                                   │  loads ui/ from pywebview's static server
│                                                   │  (127.0.0.1, random port, static files only)
├── pywebview workers ─ one short-lived thread per JS → Python call (bridge.Api methods)
└── "space-core" ────── asyncio loop (core.Core)
                         ├── EventBus flusher: batches events to the UI every ~50 ms
                         └── agent work (Phase 0: runtime/dummy.py; later: agent loop, scheduler, HTTP clients)
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
| `agent.status` | `agent_id, status` (`idle`/`responding`) | Status dot, "Responding" chip, composer lock |

## Request flow: sending a chat message (Phase 0)

1. The Composer calls `sendMessage()`, which calls `call("send_message", id, text)`.
2. `Api.send_message` validates the text, stores the user message, and stores an empty agent message with status
   `streaming`. It marks the agent busy, submits `dummy_reply(...)` to the core loop, and returns both messages
   immediately.
3. `dummy_reply` emits `agent.status: responding`, then a `message.delta` per chunk, then saves the final text and
   emits `message.done` and `agent.status: idle`.
4. In Phase 1, `runtime/loop.py` replaces step 3 with the real tool-calling loop (PLAN.md §3.2). The events stay the
   same.

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
  runtime/dummy.py           placeholder streaming replies
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
