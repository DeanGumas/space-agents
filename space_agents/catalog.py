"""Tool-server catalog and agent templates.

Phase 0 reads the catalog from a local JSON file (SPACE_AGENTS_CATALOG_FILE) so the UI can be built and tested
offline. Phase 2 adds fetching GET {mcp_base_url}/mcp with the user's key; the response format is the same
(PLAN.md §2.6).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from space_agents.manifest import Manifest, ManifestError

log = logging.getLogger(__name__)

SERVER_TYPES = ("mcp", "openapi")


def _normalize_server(raw: dict[str, Any]) -> dict[str, Any] | None:
    sid, name = raw.get("id"), raw.get("name")
    if not isinstance(sid, str) or not sid or not isinstance(name, str):
        return None
    tools = []
    for t in raw.get("tools") or []:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            tools.append(
                {
                    "name": t["name"],
                    "description": str(t.get("description") or ""),
                    "read_only": t.get("read_only") is True,
                }
            )
    return {
        "id": sid,
        "name": name,
        "description": str(raw.get("description") or ""),
        "icon": raw.get("icon") if isinstance(raw.get("icon"), str) else None,
        "type": raw.get("type") if raw.get("type") in SERVER_TYPES else "mcp",
        "role": raw.get("role") if isinstance(raw.get("role"), str) else None,
        "tools": tools,
    }


def load_catalog_file(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read catalog file %s: %s", path, e)
        return []
    servers = raw.get("servers", []) if isinstance(raw, dict) else []
    return [s for s in (_normalize_server(x) for x in servers if isinstance(x, dict)) if s]


def load_templates(directory: Path) -> list[dict[str, Any]]:
    """Agent templates shown in the New Agent dialog. Each is a manifest without an id."""
    templates = []
    for path in sorted(directory.glob("*.json")):
        try:
            m = Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ManifestError) as e:
            log.warning("Skipping invalid template %s: %s", path.name, e)
            continue
        m.id = ""
        templates.append({"key": path.stem, "manifest": m.to_dict()})
    return templates
