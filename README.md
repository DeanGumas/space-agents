# Space Agents

An open-source, local-first desktop app for creating and chatting with AI agents. It works with self-hosted,
OpenAI-compatible models and your organization's MCP/OpenAPI tool servers. Agents live in a Slack-style sidebar;
each has a plain-English manifest, chosen data sources, folder access, and an optional schedule.

**Status:** early but working. See [PLAN.md](PLAN.md) for the roadmap.

## What works today

- **Chat** with agents backed by any OpenAI-compatible model (tested with gpt-5-nano), with streaming, Stop, and a
  plain-language step for everything the agent does.
- **Documents:** agents read local PDFs and Word, Excel, and PowerPoint files, with no OCR and no upload. Extracted
  text is cached and searchable, so a 1,000-page PDF takes about 2 seconds.
- **Edits as reviewable changes:** agents edit spreadsheets and documents in a sandboxed Python (Pyodide, in the
  app's webview, with no disk or network access). Each edit comes back as a change card with a cell-level diff:
  Apply, Undo, or Discard with feedback that the agent acts on. Nothing is ever deleted, and every overwrite can be
  restored.
- **Runs:** give an agent a task and click **Run now**. It works on its own and posts a report card with findings
  (urgent / action needed / info), suggested next steps, and sources. Each agent works through one queue, so a
  message sent while it's busy waits its turn.
- **Schedules:** agents run on their own, e.g. every hour on weekdays from 7 to 6, while the app is open. Missed
  runs (computer asleep, app closed) catch up once, however long you were away, one agent at a time. The header
  shows "Next run 10:07".
- **Approvals:** an Activity inbox and desktop notifications for anything waiting on you.
- **Skills:** give an agent a folder of standard `SKILL.md` skills (e.g. `~/.claude/skills`). It loads the right one
  when a task calls for it.
- **Long tasks:** when a run gets long, the agent summarizes its earlier work so it doesn't overflow the model's
  context window. The threshold and summary length are set per agent.

**Not yet:** MCP/OpenAPI servers, drag-and-drop attachments, memory between runs and report notifications (next),
running in the background with the window closed, rooms (group chats, designed in PLAN.md §3.5), onboarding (the API key is in `.env`
for now), and installers.

## Security in brief

The threat we design against is untrusted content (emails, documents) steering the model, plus ordinary model
mistakes. In short:
- **Folders:** agents only see the folders you grant, and can only save into folders you mark Read & write. Even
  there, they can never save into system, app-settings, startup, or hidden folders, never create program files
  (`.exe`, `.bat`, `.lnk`, …), and never delete anything.
- **Code:** it runs only in the Pyodide sandbox, with no network access. Files agents save are marked as downloaded,
  so Office opens them in Protected View.
- **Permissions:** granting folders or write access, choosing a skills folder, or changing the server address goes
  through the native folder picker or a native confirmation, never just the web page.

Details: [PLAN.md §4](PLAN.md#4-security-model) and [docs/architecture.md](docs/architecture.md).

## Quick start

```sh
uv venv --python 3.12
uv pip install -r requirements.txt
.venv/bin/python scripts/vendor.py   # fetch the Pyodide sandbox (git-ignored, ~34 MB)
cp .env.example .env              # set SPACE_AGENTS_LLM_BASE_URL + SPACE_AGENTS_API_KEY for a real model
.venv/bin/python -m space_agents
```

Tests: `.venv/bin/python -m pytest` (no window or network needed). See [docs/development.md](docs/development.md)
for the smoke test, the sandbox self-test, and troubleshooting.

## Docs

- [docs/](docs/README.md): how the pieces fit together (architecture, configuration, agents and skills, UI,
  development, documents and the sandbox)
- [PLAN.md](PLAN.md): goals, design decisions, and roadmap
