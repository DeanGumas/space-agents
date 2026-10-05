# Space Agents docs

How the pieces fit together. [PLAN.md](../PLAN.md) has the *why* and the roadmap; these docs describe what's built.

| Doc | What it covers |
|---|---|
| [development.md](development.md) | Setup, running the app, tests, smoke test, troubleshooting |
| [architecture.md](architecture.md) | Processes and threads, the JS ↔ Python bridge, events, code map |
| [configuration.md](configuration.md) | Config layers, `.env`, environment variables, baking in URLs for releases |
| [agents.md](agents.md) | Agent manifests, storage on disk, avatars, templates, the server catalog |
| [ui.md](ui.md) | Front-end structure (Preact + htm, no build), state, theming, icons, CSP, vendoring |
| [documents-and-sandbox.md](documents-and-sandbox.md) | Agent tools, local document reading, the Pyodide sandbox, proposed changes |

## Status

- **Phase 0 (skeleton):** done.
- **Working vertical slice of Phases 1, 2a, and 2b:**
  - real chat with any OpenAI-compatible model (API key via `.env`; tested with gpt-5-nano)
  - the tool loop
  - local document reading/search
  - the Pyodide sandbox
  - proposed changes with Apply / Discard / Undo

  See [documents-and-sandbox.md](documents-and-sandbox.md).
- **Not yet built:**
  - MCP/OpenAPI servers
  - attachments (drag-and-drop)
  - onboarding and keychain
  - scheduled runs

Keep these docs current: when a change alters how pieces connect, update the matching doc in the same change.
