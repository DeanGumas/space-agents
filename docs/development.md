# Development

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv venv --python 3.12                         # once
uv pip install -r requirements.txt            # add --offline to install from uv's cache without network
cp .env.example .env                          # local config (git-ignored); see configuration.md
```

`requirements.txt` is the install list for now. It includes a few "maybe" packages pre-fetched for offline work.
`pyproject.toml` holds project metadata, the core dependency list, and ruff/pytest config. Once the dependency set
settles, we'll switch to `uv sync` with a `uv.lock`.

Third-party front-end assets live in `ui/vendor/` (no npm), managed by `scripts/vendor.py`:

- **JS libraries and Lucide icons** are small and committed.
- **The Pyodide sandbox** (runtime plus allow-listed wheels, 34 MB) is **git-ignored**. After cloning, run
  `python scripts/vendor.py` once to fetch it. Every file is verified against upstream hashes: Pyodide's lock file,
  or PyPI for pure-Python wheels.

To change versions, edit `PACKAGES` / `PYODIDE` in `scripts/vendor.py` and run `--update`. `--check` verifies
offline; add `--strict` to also require the Pyodide files (useful in CI before packaging).

The sandbox may load only the packages listed in the trimmed `ui/vendor/pyodide/pyodide-lock.json`. To allow a new
one, add it to `PYODIDE["packages"]` if Pyodide ships it, or to `PYODIDE["extra_wheels"]` if it's a pure-Python wheel
from PyPI.

## Run

```sh
.venv/bin/python -m space_agents                 # uses .env if present
.venv/bin/python -m space_agents --debug         # devtools: right-click → Inspect Element
.venv/bin/python -m space_agents --data-dir /tmp/sa-scratch   # throwaway data
```

With the sample `.env`, data lives in `./.devdata/` (git-ignored). Delete that folder to start fresh. Logs go to
`<data dir>/logs/space-agents.log` when the data dir is overridden; otherwise they go to the OS log folder
(`~/Library/Logs/SpaceAgents`, `%LOCALAPPDATA%\SpaceAgents\Logs`).

## Test

```sh
.venv/bin/python -m pytest              # unit + bridge + static UI checks (no window)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m space_agents --smoke-test --data-dir /tmp/sa-smoke   # opens the real window, exit 0 = OK
```

- **Sandbox self-test:** `python -m space_agents --sandbox-selftest --data-dir /tmp/sa-sbx` runs the Pyodide
  checks in the real webview (openpyxl/pandas round-trips, no network, no host disk, timeouts, recovery) and prints
  JSON. Exit 0 = all pass.
- **Sample documents:** `python dev/make_sample_docs.py` writes realistic test files to `dev/sample-docs/`.
- **Live model:** with `SPACE_AGENTS_LLM_BASE_URL` + `SPACE_AGENTS_API_KEY` in `.env`, chat uses the real model.
  Token usage per run is logged ("Usage for …").
- **Smoke test:** launches the actual pywebview window, waits for the UI to report ready, checks the DOM rendered and
  no JS errors occurred, then exits. Use it in CI on Windows and macOS runners.
- **Agent-loop tests** (`tests/test_loop.py`) use a scripted model (`ScriptedClient`) and a fake sandbox, so tool
  flows are tested without network or a webview. `tests/conftest.py` provides the `sample_docs` fixture.
- **`tests/test_ui.py`** catches the common front-end breakages without a browser: CSP hash drift, missing vendored
  files, broken relative imports, unknown icon names, and templates referencing unknown servers.
- **Bridge tests** (`tests/test_bridge.py`) attach a `FakeWindow` for the native folder picker and confirmation
  dialogs. Set `next_folder` to "pick" a folder and `answer` to approve or decline, and check `asked` to see what
  the user would have been asked.
- **`tests/test_compaction.py`** and **`tests/test_skills.py`** cover the compaction helpers and skill discovery and
  parsing. The loop tests cover both end to end.

## Common tasks

| Task | How |
|---|---|
| Edited the import map in `ui/index.html` | `python scripts/update_csp.py` (the CSP pins its hash) |
| Need a new icon | Use any name from `ui/vendor/lucide/` with `<${Icon} name="..." />` |
| Add an agent template | Drop a manifest JSON in `templates/agents/` (no `id`); tests validate it |
| Add a fake data source | Edit `dev/catalog.json` (same format as the future `/mcp` catalog) |
| Add a bridge method | Public method on `Api` in `space_agents/bridge.py` with `@endpoint`; call with `call("name", ...)` |
| Add a tool for agents | Add its spec to `TOOL_SPECS` and a branch in `ToolBox.call` (`tools/registry.py`); return a `ToolOutcome` with a plain-language `summary`; resolve paths with `self.access` |
| Change agent behavior | System prompt in `runtime/prompts.py`; for small models, instructions inside tool results (registry) work better |
| Allow a new sandbox package | See "Third-party front-end assets" above (`PYODIDE` in `scripts/vendor.py`) |
| Add a skill to test with | Make a folder with `<skill>/SKILL.md` (`---` header with `name` and `description`), then Instructions → Skills → Enable and pick it (agents.md) |
| Let agents save another file type | `SPACE_AGENTS_EXTRA_FILE_TYPES=.py,.cpp` in `.env`; launchers like `.exe` stay blocked (`NEVER_FILE_TYPES` in `tools/access.py`) |
| Test compaction without a huge document | Set the agent's "Summarize at" low (min 8,000) and have it read a few long files. The step appears as "Summarized earlier work…" |
| Test a live model cheaply | `gpt-5-nano` with `SPACE_AGENTS_REASONING_EFFORT=low`; a document task is ~10–40k tokens. Check "Usage for" in the log |

## Troubleshooting

- **Blank window:** run with `--debug`, then right-click → Inspect → Console. Module import errors and CSP
  violations show up there.
- **"Refused to execute inline script" in the console:** the import map changed; run `scripts/update_csp.py`.
- **Sandbox "failed to start":** run `--sandbox-selftest`. If the worker never fetches `/vendor/pyodide/pyodide.mjs`,
  check the worker CSP in `server.py` (no `worker-src 'none'`) and that `scripts/vendor.py` has fetched Pyodide.
- **No change card, and the agent says a folder is read-only:** the agent's folder is granted *Read only*. Switch it
  to *Read & write* under Edit → Files & folders.
- **A custom script that calls `main.run()` hangs on exit:** call `os._exit()` afterwards, like `__main__.py` does
  (see Shutdown in architecture.md).
- **No desktop notifications on macOS:** until the app is packaged, they're sent via `osascript` and attributed to
  "Script Editor". Allow notifications for Script Editor in System Settings → Notifications. The log shows
  "Desktop notification sent: …" for each one.
- **"Not saved. Agents can't save .xyz files":** that file type isn't allowed. Add it with
  `SPACE_AGENTS_EXTRA_FILE_TYPES` if it's safe (documents-and-sandbox.md).
- **"…is a system, app-settings, or hidden location":** agents can't save into `~/Library`, `AppData`, system
  folders, or hidden folders, even with Read & write. Point the agent at a regular folder.
- **Office shows "Enable Editing" on files an agent changed:** that's the Mark of the Web
  (`SPACE_AGENTS_MARK_AGENT_FILES`, on by default). Click Enable Editing, or turn the setting off.
- **"Add this folder with the Add folder… button" when saving an agent:** folders, including the skills folder,
  must come from the native picker. Typing or pasting a path (or an edited manifest JSON) doesn't count.
- **A skill doesn't show up:** the editor's Skills section lists problems. Usually a `SKILL.md` has no
  `description`, or two skills share a name. On macOS, hidden folders like `~/.claude/skills` need ⌘⇧. in the
  picker.
- **macOS log noise like `NSWindow warning: adding an unknown subview: <WKInspectorWKWebView>`:** harmless. It
  appears when the web inspector opens.
