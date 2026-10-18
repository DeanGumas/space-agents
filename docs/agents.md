# Agents

## Manifest

An agent is defined by its **manifest**: a portable JSON document with no secrets, so it can be exported and shared.
The schema is in `space_agents/manifest.py` (dataclasses) and PLAN.md §3.1.

| Field | Meaning | Edited in |
|---|---|---|
| `id` | Slug, assigned on create from the name (`purchase-order-manager`, `-2` on collision) | — |
| `name`, `description` | Shown in the sidebar and header | General |
| `avatar.emoji` / `avatar.color` | Emoji on a colored tile; `emoji: null` falls back to initials | General → avatar |
| `avatar.image` | `avatars/<id>.png`, managed by the store, **never set by the UI** | General → avatar → Picture |
| `instructions` | Standing orders (system prompt), plain English | Instructions |
| `skills` | `enabled`, `folder`: a folder of skills the agent can use (see below) | Instructions → Skills |
| `model.name`, `model.temperature` | Model id (empty → the first entry of the `models` setting); temperature is shown as "Creativity" and dropped automatically for models that reject it (e.g. GPT-5) | Model |
| `mcp_servers[]` | `{id, tools: "read_only" \| [names]}`. The document server is implicit and not listed | Data sources |
| `filesystem[]` | `{path, access: "read" \| "read_write"}`. Paths under home are stored as `~/…` | Files & folders |
| `file_changes` | `new_files: allow\|ask`, `existing_files: ask\|allow_with_backup` (see documents-and-sandbox.md) | Files & folders |
| `schedule`, `task` | When to run and what to do each run (task required if the schedule is on). With the schedule on, it runs at fixed times on the chosen days within the active hours (e.g. 07:03, 08:03, …: every N minutes from the start time, plus the agent's own random offset of up to ±5 minutes, see `schedule_jitter_minutes`) while the app is open; a missed run catches up once if "Catch up after a missed run" is on. **Run now** in the channel header runs the task immediately. Either way the result is a report card (architecture.md) | Schedule |
| `notifications` | `desktop`: pop up when the agent needs approval (and, later, when a scheduled run finds something); `min_level` for runs (`info` / `action` / `urgent`) | Schedule |
| `compaction` | `enabled`, `trigger_tokens` (8k–2M, default 100k), `summary_tokens` (200–20k, default 2k, at most a quarter of the trigger): summarize earlier work when a run gets long (architecture.md) | Model → Long tasks |
| `memory`, `limits` | Notes between runs; per-run tool-call and time caps | Advanced |
| `permissions.external_actions` | Reserved; always `never` in v1 | Advanced (disabled) |

**Validation** (`Manifest.from_dict`) collects *all* problems keyed by dotted path (`schedule.active_hours.end`,
`filesystem.1.path`). The bridge returns them as `fields`; the editor shows each under its input and puts a red dot
on the tab that owns it.

**Permissions** (see "The page is not trusted with permissions" in architecture.md): a folder an agent didn't
already have must be added with the native folder picker (or come from a built-in template), and new write access
asks for OK in a native dialog when the agent is saved. *Read & write* is refused on system, app-settings, and hidden
folders (documents-and-sandbox.md).

## Storage

```
<data dir>/
  agents/<id>.json     manifest (written atomically)
  avatars/<id>.png     optional picture, 256×256 PNG
  space.db             messages, tool calls, proposed changes, file versions, document cache (architecture.md)
  staging/, history/   proposed-change files and previous versions (documents-and-sandbox.md)
  workspaces/          per-run sandbox folders (temporary)
  settings.json        theme, endpoint overrides, UI prefs (sidebar width)
  logs/                when the data dir is overridden
```

Invalid agent files are skipped with a warning, so one bad file can't break the app.

## Avatars

Priority: **picture → emoji → initials**, drawn on `avatar.color`.

- **Emoji:** a curated, searchable grid (`ui/js/lib/emoji.js`). Users can also paste any emoji and press Enter. OS
  emoji pickers: Win + . on Windows, Ctrl + Cmd + Space on macOS.
- **Picture:** the file is read in the webview, then cropped by dragging and zooming to a 256×256 PNG on a canvas
  (`AvatarPicker.js`). It's sent as a data URL in `avatar.image_data`. Python checks the PNG signature and size
  (≤ 512 KB) and saves `avatars/<id>.png`. No image library is needed in Python.
- **On edit,** the UI sends `avatar.keep_image: true` to keep the existing picture. Omitting both fields removes it.
- **On read,** the bridge returns `avatar_url` as a data URL. Paths are confined to `avatars/`.

## Duplicating

Right-click an agent → **Duplicate** (`bridge.duplicate_agent`) copies the whole manifest (instructions, model,
data sources, folders, file-change policy, schedule, limits) and its avatar picture into a new agent. Chat history is
not copied. Names go "PO Agent (Clone)", then "(Clone 2)", …; cloning a clone doesn't stack suffixes.

## Templates

`templates/agents/*.json` are manifests without an `id`, shown in the New Agent dialog. Current templates:
- Purchase Order Manager
- Jira Triage
- Meeting Prep
- Document Summarizer

"Start from scratch" uses `blank_manifest` from `bootstrap`, so its defaults come from the Python dataclasses rather
than a separate copy in JS.

## Server catalog

The Data sources tab lists servers from the catalog. For now that's `dev/catalog.json`; in Phase 2a it's
`GET {mcp_base_url}/mcp`, using the same format (PLAN.md §2.6):

```jsonc
{ "servers": [ { "id": "costpoint", "name": "Costpoint", "description": "…", "type": "openapi",
                 "tools": [ { "name": "search_receipts", "description": "…", "read_only": true } ] },
               { "id": "docs", "name": "Document Processing", "type": "mcp", "role": "document_processing", … } ] }
```

- **Read-only tools** can be granted all at once (`"read_only"`) or picked individually.
- **Write tools** are shown locked.
- **The `document_processing` server** (GPU scans/VLM) shows as "Always available". Everyday documents are read locally (PLAN.md §2.8).
- **Servers an agent references that aren't in the catalog** are shown so the user can remove them.

## Skills

A skill is a folder with a `SKILL.md`: a YAML header with a `name` and a `description` (when to use it), then
instructions. It can hold extra files the instructions point to (references, templates, scripts). This is the
standard Agent Skills layout, so existing skill folders work as they are. Code is in `space_agents/skills.py`.

- **Choosing the folder:** Instructions → Skills → Enable skills, then pick the folder with the native picker. The
  folder can be the skills folder itself, a folder that contains `skills/`, or a single skill. The editor previews
  what it found (`list_skills`) and flags problems, such as a skill with no description or two skills with the same
  name. Like data folders, a new skills folder must come from the native picker (architecture.md).
- **Loading:** skills are re-read at the start of every run, so edits apply without re-saving the agent. The model
  sees each skill's name and description in its system prompt. When a task matches one, it calls `use_skill(name)`,
  which returns the instructions and the list of the skill's files. It then reads those files with
  `read_document`, or passes Python scripts to `run_python`.
- **Limits:** a skill's folder is readable but never writable, even inside a read & write grant: skills are trusted
  instructions, so an agent mustn't be able to rewrite them. Shell and Node scripts can't run (there's no shell).
  Python scripts run in the sandbox with its packages. The sandbox puts input files side by side, so scripts that
  import their own modules from subfolders may need the model's help.
- **Scale:** up to 100 skills, descriptions up to 1,024 characters, and `SKILL.md` up to 512 KB.
