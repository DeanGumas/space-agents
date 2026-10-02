# Space Agents docs

How the pieces fit together. [PLAN.md](../PLAN.md) has the *why* and the roadmap; these docs describe what's built.

| Doc | What it covers |
|---|---|
| [development.md](development.md) | Setup, running the app, tests, smoke test, troubleshooting |
| [architecture.md](architecture.md) | Processes and threads, the JS ↔ Python bridge, events, code map |
| [configuration.md](configuration.md) | Config layers, `.env`, environment variables, baking in URLs for releases |
| [agents.md](agents.md) | Agent manifests, storage on disk, avatars, templates, the server catalog |
| [ui.md](ui.md) | Front-end structure (Preact + htm, no build), state, theming, icons, CSP, vendoring |

## Status

**Phase 0 (skeleton)** is done:
- native window
- Slack-style UI in light and dark
- create, edit, and delete "dummy" agents (emoji or picture avatars, full manifest editor)
- placeholder chat replies streamed through the real event pipeline
- layered config with `.env`

Nothing talks to a model or MCP server yet, and there's no auth.

Keep these docs current: when a change alters how pieces connect, update the matching doc in the same change.
