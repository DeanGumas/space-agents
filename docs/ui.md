# UI

Preact + htm with no build step and no npm. The browser runs the source files directly.

## Files

```
ui/
  index.html              CSP, import map, entry script
  css/tokens.css          design tokens (colors, radii, shadows) for light + dark
  css/app.css             all component styles (uses tokens only)
  js/main.js              render <App/>, init(), then call("ui_ready")
  js/api.js               call() wrapper, ApiError, event fan-out, JS error capture
  js/state.js             signals + actions (agents, messages, dialogs, toasts, theme)
  js/app.js               layout + dialogs + keyboard shortcuts
  js/sandbox.js           main-thread side of the Pyodide sandbox: owns the Web Worker, validates its messages
  js/views/               Sidebar, Channel (header/timeline/composer), Activity (things waiting on you),
                          AgentEditor, SettingsDialog, Welcome
  js/components/          common.js (Icon, Avatar, Modal, Field, Switch…), AvatarPicker.js,
                          Activity.js (activity steps, change cards, the Discard feedback box),
                          ContextMenu.js (right-click menus)
  js/lib/                 theme, markdown (marked + DOMPurify), emoji list, util (paths, time)
  vendor/                 pinned third-party files + lucide/ icons + licenses (see scripts/vendor.py)
  sandbox/worker.js       the Pyodide Web Worker (see documents-and-sandbox.md)
  img/logo.svg
```

## Components and state

htm uses template literals instead of JSX:

```js
html`<${Avatar} agent=${agent} size=${32} />`
html`<${Field} label="Name" error=${err("name")}>…<//>`
```

**Global state** lives in signals in `state.js`. Components read `signal.value` and re-render when it changes. Data
flows one way:

1. A UI action calls a function in `state.js`.
2. That function calls the bridge with `call()`.
3. It updates signals from the response.
4. Pushed events (`handleEvent`) also update signals.

Local UI state (form drafts, open/closed toggles) uses `useState`.

**Agent replies** render, in order: activity steps (`tool_calls`), the Markdown answer, and change cards
(`changes`). Both lists come with `list_messages` and are kept live by the `tool.started`/`tool.done` and
`change.updated` events. Besides tool calls, the steps include `compact_context` ("Summarized earlier work…") when
a long run was compacted.

**Agent status and the queue.** `agent.status` is `idle`, `responding` (a chat reply), or `running` (a run). While
the agent is busy, the composer shows **Stop** next to Send. Stop cancels the current work and anything queued.
Sending still works: the reply shows "Waiting for … to finish its current work…" (status `queued`) until the
agent's queue gets to it, and `message.started` switches it to streaming.

**Schedules.** A scheduled agent shows a "Next run 10:07" / "Next run tomorrow 07:02" chip in the channel header
(`nextRunLabel` in `lib/util.js`), and a clock in its sidebar row with the same tooltip. Both come from
`next_run_at` in the agent view and update on `agent.schedule` events. The schedule itself is edited in the
editor's Schedule tab. Scheduled runs show a "Scheduled run" chip on their report.

**Runs.** **Run now** in the channel header (`runAgent()` → `run_agent`) is disabled, with a tooltip, until the
agent has a task and a model is configured.
- **While it works:** the run's message shows a "Run" chip and its activity steps.
- **When done:** it renders `RunReport` (`components/Report.js`), with a summary, then new and changed findings
  by level (urgent / action needed / info) with suggested actions and source chips. "Changed" findings get a tag.
  Findings marked `unchanged` (already reported) follow as a dimmed "Still open (reported before)" list.
- **Sanitizing:** item details are model output and go through `renderMarkdown`.

The **agent editor** edits a draft copy and saves through `saveAgent()`. A failed save puts `ApiError.fields` into
the editor's `errors` and jumps to the first tab with an error. Closing with unsaved changes asks for confirmation.

Editor tabs and what they own (`TABS` in `AgentEditor.js`, which also routes errors to the right tab):
- **General:** name, description, avatar.
- **Instructions:** instructions, plus **Skills** (switch, folder picker, and a preview of the skills found via
  `list_skills`).
- **Model:** model, creativity, plus **Long tasks** (compaction switch, trigger, and summary length).
- **Data sources:** MCP servers.
- **Files & folders:** folders and the file-change policy.
- **Schedule:** schedule, task, and notifications.
- **Advanced:** limits, the memory switch with the agent's notes (`MemoryNotes`: delete one, or all, with a
  two-step confirm; notes are agent-written, so plain text), JSON, and delete.

## Sidebar and menus

- **Resizable sidebar:**
  - Drag its right edge (200–480 px); double-click the edge to reset.
  - Drag below ~150 px and it snaps to a 72 px **icon rail**: avatars only, unread badges, names as tooltips.
  - **Ctrl/⌘ B** toggles between the rail and the last width.
  - The width lives in the `sidebarWidth` signal, which sets `--sidebar-w` on `.app`. It's persisted with
    `call("set_ui_pref", "sidebar_width", px)` into `settings.json` → `ui`, and restored from `bootstrap().ui`.
- **Context menus:** call `openContextMenu(event, {title, items})` from state.js. `<ContextMenu>` in `app.js` renders
  one menu at a time, keeps it on screen, supports arrow keys and Escape, and closes on outside click, scroll, or
  resize. Right-clicking an agent offers **Edit agent** and **Duplicate**.
- **Activity:** the `attention` signal (from `bootstrap` and `attention.updated`) drives the Activity tab and the red
  count badge on the Activity nav item (hidden at 0).
  - **Entries:** one per agent, with its report notices (`ReportNotice`) and change cards.
  - **Opening:** "Open conversation" calls `openChange()` or `openReport()`, which open the agent and scroll to and
    flash the card or report (`focusTarget` holds the element id, `change-…` or `msg-…`).
  - **Seen:** `selectAgent` tells Python the user saw the agent's reports (`mark_reports_seen`), and Dismiss calls
    `dismiss_report`. `init()`'s automatic first selection doesn't mark anything seen.
- **Focus reporting:** `state.js` calls `set_ui_state(focused, viewing)` on window focus/blur/visibility changes and
  when the open conversation changes, so Python can skip desktop notifications you'd see anyway.
- **Keyboard shortcuts:** Ctrl/⌘ N new agent · Ctrl/⌘ , settings · Ctrl/⌘ B collapse sidebar.

## Theming

- **Tokens:** every color is a CSS variable in `tokens.css`. `:root` holds light, `:root[data-theme="dark"]` holds
  dark.
- **Applying the theme:** `lib/theme.js` resolves `system`/`light`/`dark` and sets `<html data-theme>`, then follows
  OS changes while set to `system`.
- **Sidebar:** dark navy in both themes, Slack-style.
- **Launch flash:** Python sets the native window background to the theme's `--bg` before the page paints
  (`WINDOW_BG` in `main.py`, kept in sync by a test).

## Icons

Lucide SVGs (`ui/vendor/lucide/`) are rendered as CSS masks so they take `currentColor`:
`<${Icon} name="folder" size="sm" />`. Any of the ~2,100 names works. We'll trim the set to what's used before
packaging.

## Security (CSP and sanitizing)

- **Page CSP** (in `index.html`): no remote origins. No inline scripts except the import map, which is pinned by
  SHA-256 (`scripts/update_csp.py`). `'unsafe-eval'` is needed because pywebview's injected bridge builds API stubs
  with `new Function`. `worker-src 'self'` allows the sandbox worker.
- **Worker CSP:** the sandbox worker gets its own policy as an HTTP header from `server.py`: `connect-src 'self'` (no
  network) and `'wasm-unsafe-eval'`. Don't add `worker-src 'none'` there; WebKit then blocks the worker's own
  imports.
- **Sanitizing:** all model/agent output is rendered through `renderMarkdown()` (marked → DOMPurify). Never inject
  agent text any other way. Links get `target=_blank`, which pywebview opens in the system browser.
- **Local server:** our WSGI app (`server.py`) serves `ui/` on `127.0.0.1`, because ES modules can't load from
  `file://`. Besides static files it only has the sandbox's token-scoped `/workspace/…` routes. All app actions go
  through the bridge.

## Conventions

- **Styles:** use tokens, never raw colors.
- **Avatars and icons:** use the `Avatar` and `Icon` components.
- **Errors:** show them with `toast`/`toastError`. Don't use `alert`/`confirm`/`prompt` (unreliable in webviews);
  use inline UI like the delete button's two-step confirm or the change card's Discard box.
- **Permissions are confirmed natively, not in the page.** Anything that widens an agent's reach (new folders,
  Read & write, a skills folder, server addresses) is checked by Python, which raises a native dialog or requires
  the native folder picker. A page-side confirm would defeat the purpose, because the page is the thing we don't
  trust (architecture.md). Folder and skills-folder inputs only take values from `call("pick_folder")`.
- **Wording:** user-facing copy is written for non-technical users, e.g. "Creativity" rather than "temperature",
  and "Data sources" rather than "MCP servers".
