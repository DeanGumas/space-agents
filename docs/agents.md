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
| `model.name`, `model.temperature` | Model id (empty → the first entry of the `models` setting); temperature is shown as "Creativity" and dropped automatically for models that reject it (e.g. GPT-5) | Model |
| `mcp_servers[]` | `{id, tools: "read_only" \| [names]}`. The document server is implicit and not listed | Data sources |
| `filesystem[]` | `{path, access: "read" \| "read_write"}`. Paths under home are stored as `~/…` | Files & folders |
| `file_changes` | `new_files: allow\|ask`, `existing_files: ask\|allow_with_backup` (see documents-and-sandbox.md) | Files & folders |
| `schedule`, `task` | When to run and what to do each run (task required if the schedule is on) | Schedule |
| `notifications` | Desktop popups and minimum level (`info` / `action` / `urgent`) | Schedule |
| `memory`, `limits` | Notes between runs; per-run tool-call and time caps | Advanced |
| `permissions.external_actions` | Reserved; always `never` in v1 | Advanced (disabled) |

**Validation** (`Manifest.from_dict`) collects *all* problems keyed by dotted path (`schedule.active_hours.end`,
`filesystem.1.path`). The bridge returns them as `fields`; the editor shows each under its input and puts a red dot
on the tab that owns it.

## Storage

```
<data dir>/
  agents/<id>.json     manifest (written atomically)
  avatars/<id>.png     optional picture, 256×256 PNG
  space.db             messages, tool calls, proposed changes, file versions, document cache (architecture.md)
  staging/, history/   proposed-change files and previous versions (documents-and-sandbox.md)
  workspaces/          per-run sandbox folders (temporary)
  settings.json        theme + endpoint overrides
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
