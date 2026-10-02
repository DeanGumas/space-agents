"""Per-user preferences persisted to settings.json in the data directory."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

THEMES = ("system", "light", "dark")

# Endpoint settings a user may override from Settings → Advanced.
USER_OVERRIDABLE = ("llm_base_url", "mcp_base_url")


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via a temp file + rename so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class UserSettings:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "settings.json"
        self.theme = "system"
        self.overrides: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Ignoring unreadable %s: %s", self.path, e)
            return
        if raw.get("theme") in THEMES:
            self.theme = raw["theme"]
        overrides = raw.get("overrides") or {}
        self.overrides = {k: str(v) for k, v in overrides.items() if k in USER_OVERRIDABLE and v}

    def save(self) -> None:
        write_json_atomic(self.path, {"theme": self.theme, "overrides": self.overrides})

    def set_theme(self, theme: str) -> None:
        if theme not in THEMES:
            raise ValueError(f"Unknown theme {theme!r}")
        self.theme = theme
        self.save()

    def set_overrides(self, overrides: dict[str, str]) -> None:
        self.overrides = {
            k: v.strip().rstrip("/") for k, v in overrides.items() if k in USER_OVERRIDABLE and v and v.strip()
        }
        self.save()
