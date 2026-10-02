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

The front-end libraries are already vendored in `ui/vendor/` (no npm). To change versions, edit `PACKAGES` in
`scripts/vendor.py` and run `python scripts/vendor.py --update` (needs network).

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

- **Smoke test:** launches the actual pywebview window, waits for the UI to report ready, checks the DOM rendered and
  no JS errors occurred, then exits. Use it in CI on Windows and macOS runners.
- **`tests/test_ui.py`** catches the common front-end breakages without a browser: CSP hash drift, missing vendored
  files, broken relative imports, unknown icon names, and templates referencing unknown servers.

## Common tasks

| Task | How |
|---|---|
| Edited the import map in `ui/index.html` | `python scripts/update_csp.py` (the CSP pins its hash) |
| Need a new icon | Use any name from `ui/vendor/lucide/` with `<${Icon} name="..." />` |
| Add an agent template | Drop a manifest JSON in `templates/agents/` (no `id`); tests validate it |
| Add a fake data source | Edit `dev/catalog.json` (same format as the future `/mcp` catalog) |
| Add a bridge method | Public method on `Api` in `space_agents/bridge.py` with `@endpoint`; call with `call("name", ...)` |

## Troubleshooting

- **Blank window:** run with `--debug`, then right-click → Inspect → Console. Module import errors and CSP
  violations show up there.
- **"Refused to execute inline script" in the console:** the import map changed; run `scripts/update_csp.py`.
- **macOS log noise like `NSWindow warning: adding an unknown subview: <WKInspectorWKWebView>`:** harmless. It
  appears when the web inspector opens.
