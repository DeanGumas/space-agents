# Configuration

## Layers

Each setting is resolved from these layers. Higher layers win.

| # | Layer | Where | Typical use |
|---|---|---|---|
| 1 | `default` | `space_agents/defaults.json` (shipped inside the app) | Org URLs baked into release builds |
| 2 | `user` | `<data dir>/settings.json` → `overrides` | A user changing endpoint URLs in Settings → Connection |
| 3 | `.env` | `.env` in the working dir, else the repo root | Dev/test scaffolding |
| 4 | `env` | Real environment variables | CI, one-off runs |
| 5 | `cli` | `--debug`, `--data-dir` | Quick overrides |

Planned (Phase 1): a **remote config** layer between `default` and `user`, fetched from the service on launch, so
URLs can move without reinstalling (PLAN.md §2.5).

Only the endpoint URLs can be changed from the app's UI. Security settings such as `extra_file_types` and
`mark_agent_files` come only from `defaults.json`, `.env`, the environment, or the CLI, so a compromised page can't
loosen them.

Saving a new server address in Settings asks for OK in a native dialog first, because the API key will be sent
there. A compromised page can't change where the key goes on its own (architecture.md).

Settings → Data shows every value and the layer it came from. Endpoint fields are editable in Settings only when the
current value comes from `default` or `user`; a value from `.env`/env/CLI would override the edit anyway.

## Settings

| Key | Env var | Kind | Notes |
|---|---|---|---|
| `llm_base_url` | `SPACE_AGENTS_LLM_BASE_URL` | URL | The app will call `{base}/chat/completions` and `{base}/models` |
| `mcp_base_url` | `SPACE_AGENTS_MCP_BASE_URL` | URL | Catalog at `{base}/mcp`, servers at `{base}/mcp/{id}` |
| `data_dir` | `SPACE_AGENTS_DATA_DIR` | path | Default: `~/Library/Application Support/SpaceAgents` or `%LOCALAPPDATA%\SpaceAgents` |
| `debug` | `SPACE_AGENTS_DEBUG` | bool | `1/true/yes/on`: devtools + debug logging |
| `catalog_file` | `SPACE_AGENTS_CATALOG_FILE` | path | **Dev only.** Offline catalog JSON used instead of `GET /mcp` |
| `models` | `SPACE_AGENTS_MODELS` | list | Models offered in the editor; the first is the default for agents without one |
| `api_key` | `SPACE_AGENTS_API_KEY` | secret | **Dev/test only.** Never shown in the UI (Settings shows "set"); moves to the OS keychain |
| `reasoning_effort` | `SPACE_AGENTS_REASONING_EFFORT` | str | Default `low`; bounds reasoning tokens; dropped if a model rejects it |
| `extra_file_types` | `SPACE_AGENTS_EXTRA_FILE_TYPES` | list | File types agents may save on top of the safe defaults, e.g. `.py,.cpp`. Program launchers are never allowed (see documents-and-sandbox.md) |
| `mark_agent_files` | `SPACE_AGENTS_MARK_AGENT_FILES` | bool | Default on: tag files agents save as downloaded from the internet (Protected View in Office) |
| `schedule_jitter_minutes` | `SPACE_AGENTS_SCHEDULE_JITTER_MINUTES` | int | Default 5 (0–30). Scheduled runs start at a random time up to this many minutes before or after their scheduled time; each agent keeps its offset. **Lowering it may lead to slow responses, because many users' agents would then run at precisely the same time (e.g. on the hour)** |
| `max_concurrent_runs` | `SPACE_AGENTS_MAX_CONCURRENT_RUNS` | int | Default 2 (1–10): how many runs (scheduled or Run now) may work at once across all agents; chat isn't capped |
| `sandbox_warmup` | `SPACE_AGENTS_SANDBOX_WARMUP` | str | `full` (default) / `light` / `off`: preload the Python sandbox at launch (see documents-and-sandbox.md) |

Behavior details:
- URLs have trailing `/` stripped. `llm_base_url` also accepts a full endpoint URL (`…/v1/chat/completions` →
  `…/v1`).
- Relative paths in `.env` resolve against the `.env` file's folder. Relative paths in env vars and the CLI resolve
  against the working directory.
- Empty values are treated as unset.

## `.env` format

`KEY=VALUE` per line. `export KEY=VALUE`, quoted values, and `# comments` are supported (parser in `config.py`;
there's no python-dotenv dependency). Copy `.env.example` to start.

## Testing different setups

- **Throwaway data:** `--data-dir /tmp/sa-test`, or point `SPACE_AGENTS_DATA_DIR` at a scratch folder.
- **Different servers:** set `SPACE_AGENTS_LLM_BASE_URL=... python -m space_agents` for one run, without editing
  `.env`.
- **Fake data sources:** edit `dev/catalog.json` or point `SPACE_AGENTS_CATALOG_FILE` at another file.

## Baking in URLs for a release

Set `llm_base_url` / `mcp_base_url` in `space_agents/defaults.json` before building. Other organizations using the
open-source project ship their own `defaults.json`. Never put secrets here: the API key goes in the OS keychain
(Phase 1).
