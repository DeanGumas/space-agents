# Space Agents docs

How the pieces fit together. [PLAN.md](../PLAN.md) has the *why* and the roadmap; these docs describe what's built.

| Doc | What it covers |
|---|---|
| [development.md](development.md) | Setup, running the app, tests, smoke test, troubleshooting |
| [architecture.md](architecture.md) | Processes and threads, the JS ↔ Python bridge, events, what the page is trusted with, long-run compaction, code map |
| [configuration.md](configuration.md) | Config layers, `.env`, environment variables (incl. file-safety settings), baking in URLs for releases |
| [agents.md](agents.md) | Agent manifests, storage on disk, avatars, templates, skills, the server catalog |
| [ui.md](ui.md) | Front-end structure (Preact + htm, no build), state, theming, icons, CSP, vendoring |
| [documents-and-sandbox.md](documents-and-sandbox.md) | Agent tools, local document reading, the Pyodide sandbox, where agents may write and what, proposed changes |

## Status

- **Phase 0 (skeleton):** done.
- **Working vertical slice of Phases 1, 2a, and 2b:**
  - real chat with any OpenAI-compatible model (API key via `.env`; tested with gpt-5-nano)
  - the tool loop
  - local document reading/search
  - the Pyodide sandbox
  - proposed changes with Apply / Undo, and Discard with feedback that the agent acts on
  - the Activity tab and desktop notifications for anything awaiting approval
  - a resizable sidebar and a right-click menu (Edit, Duplicate)
  - skills: a folder of SKILL.md skills per agent ([agents.md](agents.md))
  - compaction of long runs ([architecture.md](architecture.md))
  - **Run now** with report cards, and a work queue per agent ([architecture.md](architecture.md))
  - scheduled runs: slots with jitter, catch-up after sleep, and a global run cap
  - memory: agent notes between runs and chats, and new / changed / still-open findings
  - report notifications and run reports in Activity

  See [documents-and-sandbox.md](documents-and-sandbox.md).
- **Security hardening:**
  - the bridge doesn't trust the page with permissions ([architecture.md](architecture.md))
  - agents can't save into system, autostart, or hidden folders, or save program files, and their files are
    marked as downloaded ([documents-and-sandbox.md](documents-and-sandbox.md))
- **Not yet built:**
  - MCP/OpenAPI servers
  - attachments (drag-and-drop)
  - onboarding and keychain
  - background running: close-to-background, single instance, start at login (Phase 3 step e)
  - rooms (group chats; designed in PLAN.md §3.5)
  - the "Allow writing to this folder?" permission card

Keep these docs current: when a change alters how pieces connect, update the matching doc in the same change.
