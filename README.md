# Space Agents

An open-source, local-first desktop app for creating and chatting with AI agents. It works with self-hosted,
OpenAI-compatible models and your organization's MCP/OpenAPI tool servers. Agents live in a Slack-style sidebar;
each has a plain-English manifest, chosen data sources, folder access, and an optional schedule.

**Status:** early but working.
- Agents chat with a real OpenAI-compatible model.
- Agents read local PDFs and Office files.
- Agents edit spreadsheets and documents in a sandboxed Python (Pyodide). Edits arrive as reviewable changes: Apply,
  Undo, or Discard with feedback that the agent acts on.
- Not yet: MCP servers, scheduled runs, or onboarding.

See [PLAN.md](PLAN.md) for the roadmap.

## Quick start

```sh
uv venv --python 3.12
uv pip install -r requirements.txt
.venv/bin/python scripts/vendor.py   # fetch the Pyodide sandbox (git-ignored, ~34 MB)
cp .env.example .env              # set SPACE_AGENTS_LLM_BASE_URL + SPACE_AGENTS_API_KEY for a real model
.venv/bin/python -m space_agents
```

## Docs

- [docs/](docs/README.md): how the pieces fit together (architecture, configuration, agents, UI, development,
  documents and the sandbox)
- [PLAN.md](PLAN.md): goals, design decisions, and roadmap
