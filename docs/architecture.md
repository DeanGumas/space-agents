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
                         ├── scheduler: a 30 s tick that queues scheduled runs (runtime/scheduler.py)
                         └── per-agent work queues → agent runs (runtime/loop.py), LLM client, sandbox orchestration
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
| `message.started` | `agent_id, message` | A queued reply or run began: its status (and run) update |
| `message.done` | `agent_id, message` | Replace with the final message (with `run` for reports); bump unread if not viewing |
| `agent.status` | `agent_id, status` (`idle`/`responding`/`running`) | Status dot, "Responding"/"Running its task" chip, Stop button |
| `agent.schedule` | `agent_id, next_run_at` (local time, or null) | "Next run 10:07" chip and the sidebar clock |
| `tool.started` / `tool.done` | `agent_id, message_id, call` | Activity step (spinner → done/failed); also used for `compact_context` |
| `change.updated` | `agent_id, message_id, change` | Proposed-change card (pending/applied/…) |
| `attention.updated` | `attention` (full list) | Activity entries and the sidebar's Activity badge |

## Request flow: sending a chat message

1. The Composer calls `sendMessage()`, which calls `call("send_message", id, text)`.
2. `Api.send_message` validates the text and hands it to the agent's work queue (`Dispatcher.chat`,
   `runtime/dispatch.py`). The queue stores the user message and an empty agent reply with status `queued`, and
   returns both right away. If the agent is busy, the reply waits its turn ("Waiting for … to finish…"). When it
   starts, it becomes `streaming` and `message.started` is emitted.
3. With a model configured (`llm_base_url` + `api_key`), `AgentRunner.chat` (`runtime/loop.py`):
   1. loads the agent's skills, if enabled (re-read every run), and builds the system prompt and history
   2. compacts the conversation first if it's over the agent's threshold (see "Long runs" below)
   3. streams the model's answer (`message.delta`)
   4. runs any tool calls (`tool.started`/`tool.done`, plus `change.updated` for files) and loops
   5. saves the final text and token usage (`message.done`)

   Without a model, `runtime/dummy.py` streams a placeholder.
4. "Stop" cancels what the agent is doing and everything queued for it. Queued replies end as "Stopped before it
   started."

**The work queue.** Each agent has one FIFO queue (an `asyncio.Lock` per agent on the core loop) shared by chat
replies, runs, and later room turns. Different agents work in parallel. A job reads the agent's manifest when it
starts, not when it was queued. Job state (started, cancelled, finished) changes only on the core loop, so Stop can't
race a job that's starting. After any job that ran, the bridge's `_after_work` checks for pending changes to notify
about.

## Request flow: a run (Run now or scheduled)

1. **Starting a run:**
   - **Run now:** the channel header calls `run_agent(id)`. It needs a task (Edit → Schedule) and a configured
     model.
   - **Scheduled:** the scheduler's tick finds the agent's slot due (see "Scheduling" below) and calls
     `Dispatcher.run(agent, "schedule")`.
2. `Dispatcher.run` creates a `runs` row (`trigger: manual` or `schedule`) and its report message (`queued`, with
   `run_id`), then queues the job.
   - **Shared cap:** runs share a global cap (`max_concurrent_runs`, default 2).
   - **Catch-ups:** catch-up runs (`catch_up=True`) also take turns one at a time across agents, before taking a
     slot.
   - **Waiting:** a run waiting for its turn stays `queued` and keeps its place in its agent's line.
3. `AgentRunner.run` (`runtime/loop.py`) uses a fresh context: the system prompt with "This is a run, not a chat",
   then the task. `post_report` is among its tools.
4. The run ends when the model calls `post_report` (`runtime/reports.py`):
   - **If it answers in text instead,** it gets one nudge with only `post_report` on offer. If that fails too, the
     text becomes the report.
   - **Saving:** the report is saved on the run, and its Markdown becomes the message content.
   - **UI:** `message.done` carries the run, so the UI renders a report card.
5. File changes follow the agent's `file_changes` policy, as in chat.

**Scheduling (`runtime/scheduler.py`).** `main.py` starts `Scheduler.run_forever()` on the core loop. It isn't
started in `--smoke-test`/`--sandbox-selftest`. The first tick comes 15 s after launch, then one every 30 s.

- **Slots:** each scheduled agent has fixed scheduled times on its active days inside its active hours: start, then
  every N minutes. It runs at those times plus its own jitter.
- **Jitter:** a random offset of up to ±`schedule_jitter_minutes` (default 5), drawn once per agent and saved in
  `schedule_state.jitter_seconds`. It's random, not derived from the agent id, because agents made from the same
  template share an id across machines.
- **Saved progress:** the next slot is stored in `schedule_state` with a key of the schedule's settings. Editing
  the schedule recomputes it.
- **Each tick** compares the wall clock with `next_run_at`:
  - **Due within 3 minutes:** run.
  - **Older** (sleep, app closed): run once, as a *catch-up*, if catch-up is on and it's within active hours now.
    Otherwise wait for the next normal time. Only one upcoming time is stored per agent, so days away still means
    one run.
  - **Skipped:** while the agent's previous scheduled run is still queued or running, or when no model is
    configured.
  - **Always:** move to the next slot after now and emit `agent.schedule`.
- **For the UI:** the bridge adds `next_run_at` to each agent view via `Scheduler.next_run`, which works even
  before the first tick.

**Long runs (compaction, `runtime/compaction.py`).** Every model call resends the run so far, so a run that reads
many documents can outgrow the model's context window. Before each call, the loop measures the run's size: the
model's own `prompt_tokens` from the last call plus a chars/4 estimate of what's been added since.

Past the agent's `compaction.trigger_tokens`, it compacts the run:
1. The agent's own model summarizes everything except the system prompt and the latest round of tool calls and
   results, in about `summary_tokens`.
2. The run continues from `[system prompt, summary + the user's request verbatim, latest round]`.
3. The step shows up in the timeline as `compact_context`, and its result is the summary text.

If the summary call fails, older tool results are replaced by a short note instead, and compaction isn't retried in
that run. The summary call's tokens count toward the run's usage.

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

## Approvals: Activity and desktop notifications

- **The attention list** (`ChangeManager.attention()`) has one entry per agent that's waiting on the user: it has
  `pending`/`conflicted` changes, or it's *revising* after "Discard & tell agent". It's sent in `bootstrap` and pushed
  as `attention.updated` whenever a change's status or an agent's revising state changes.
- **The Activity tab** renders one entry per agent with its change cards (Apply/Discard work right there). The
  sidebar badge shows the number of entries and is hidden at 0.
- **Desktop notification:** when a reply finishes (`Api._start_reply` → `finished`), if it left pending changes the
  bridge calls `Notifier.notify("<agent> requests approval", approval_summary(changes))`. It's skipped when the window
  is focused on that agent's conversation (reported by the UI via `set_ui_state`), or when the agent's
  `notifications.desktop` is off.
- **Delivery** (`platform/notify.py`): `osascript` on macOS, a PowerShell toast on Windows, `notify-send` on Linux. Text
  is passed as arguments/environment variables (no injection). Until the app is packaged, macOS lists these under
  "Script Editor" and Windows under "Windows PowerShell".

## Bridge endpoints

| Area | Methods |
|---|---|
| Lifecycle | `ping`, `ui_ready`, `bootstrap` |
| Agents | `list_agents`, `save_agent`, `duplicate_agent`, `delete_agent` |
| Chat | `list_messages` (with tool calls, changes, and runs), `send_message` (queued if busy), `stop_agent` (current + queued) |
| Runs | `run_agent` (Run now); agent views include `next_run_at` |
| Skills | `list_skills(folder)` (editor preview; only for picked folders or folders agents already use) |
| Activity | `attention` (also in `bootstrap`), `set_ui_state(focused, viewing)` (window focus + open conversation, for notifications) |
| Changes | `apply_change`, `discard_change`, `undo_change`, `reveal_change` |
| Sandbox | `sandbox_done` (called by `ui/js/sandbox.js` when the worker finishes a run) |
| Files & settings | `pick_folder`, `set_theme`, `set_ui_pref` (allow-listed keys, e.g. `sidebar_width`), `save_endpoint_overrides`, `open_data_folder` |

## The page is not trusted with permissions

The webview renders untrusted model output and hosts the sandbox's Web Worker in the same renderer process as the
page. So the bridge assumes a compromised page is possible (say, via a browser-engine bug triggered by injected code)
and makes sure such a page can't widen an agent's reach or redirect the API key on its own. Those actions need the
user to act in native UI, which the page can't script:

| Action | Requires |
|---|---|
| Granting an agent a folder it didn't have | The folder was chosen in the native picker this session (`pick_folder`), or comes from a built-in template. Otherwise `save_agent` rejects it as a field error |
| Read & write on a folder (new, or upgraded from read only) | OK in a native confirmation dialog on save |
| "Existing files: apply without asking" (`allow_with_backup`) on an agent with a writable folder | Same dialog |
| A new skills folder | Chosen in the native picker this session (skills are instructions the agent trusts) |
| A new server address (`save_endpoint_overrides`) | OK in a native dialog: the API key and agent data will go there. Clearing an override (back to the build default) doesn't ask |

The dialogs go through `Api._confirm`. With no window (headless), the answer is always no, and tests inject a fake.
What a compromised page *can* still do: chat with agents, apply or discard pending changes (every overwrite keeps a
restorable version), and edit agents' instructions.

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
  skills.py      agent skills: find SKILL.md folders, parse their headers, what use_skill returns
  paths.py       resource dir (dev vs PyInstaller bundle), per-OS data/log dirs
  defaults.json  build-time defaults (org URLs get baked in here)
  server.py      WSGI app: UI files, sandbox worker CSP header, token-scoped workspace routes
  llm/client.py  OpenAI-compatible chat (streaming, tools, param-compat, retries)
  runtime/       loop.py (agent loop: chat + runs), dispatch.py (per-agent work queue + run cap),
                 scheduler.py (slots, 30 s tick, catch-up), reports.py (post_report),
                 compaction.py, prompts.py, dummy.py (no-model placeholder)
  tools/         registry.py (tool specs + execution), access.py (folder policy)
  documents/     extract.py (PDF/Office/text → pages), library.py (cache + FTS5 search)
  sandbox/       pyodide.py (host side), selftest.py (--sandbox-selftest)
  changes.py     proposed changes, apply/discard/undo, file history
  platform/appearance.py     OS dark-mode detection, open folder in Finder/Explorer
  platform/notify.py         desktop notifications (osascript / PowerShell toast / notify-send)
  platform/marking.py        mark files agents write as downloaded (Mark of the Web / macOS quarantine)
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
- **On startup**, messages left `streaming` or `queued` (crash or quit mid-reply) are marked `error`, and so are
  runs left `queued`/`running`. Leftover sandbox `workspaces/` are deleted, and file history older than 30 days is
  pruned.
- **Schema v3** rebuilds `messages` (SQLite can't change a CHECK constraint in place) with ids kept, and adds `runs`.
- **Proposed changes** are applied with a hash check, a snapshot of the previous version, and a temp file +
  `os.replace`, so a crash can't leave a half-written user file.

| Table | Contents |
|---|---|
| `messages` | Chat history (+ token usage per agent reply); status `queued`/`streaming`/`done`/`error`; `run_id` for run reports |
| `runs` | Runs of an agent's task: trigger (`manual`/`schedule`), status, task, report JSON, timestamps |
| `schedule_state` | Per scheduled agent: `next_run_at`, `last_run_at`, its `jitter_seconds`, and the settings they were computed for |
| `tool_calls` | Every tool call: name, arguments, summary, truncated result, status, duration (the audit trail) |
| `changes` | Proposed changes and their status |
| `file_versions` | Previous versions of overwritten files (blobs in `history/`) |
| `doc_cache`, `doc_pages`, `doc_fts` | Extracted document text by content hash, plus the FTS5 search index |

## Shutdown

`run()` stops the core loop, closes the document executor and the database, and returns. `__main__.py` then flushes
logs and calls `os._exit`. pywebview runs bridge calls on non-daemon threads that wait for the window to acknowledge
their return value, so a call that was in flight when the window closed would otherwise block interpreter exit
forever.
