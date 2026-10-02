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
| `models` | `SPACE_AGENTS_MODELS` | list | **Dev only.** Comma-separated models used instead of `GET /models` |

Behavior details:
- URLs have trailing `/` stripped.
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
