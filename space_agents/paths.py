"""Filesystem locations: bundled resources (UI, templates) and per-user data."""

from __future__ import annotations

import sys
from pathlib import Path

from platformdirs import user_data_dir, user_log_dir

APP_NAME = "SpaceAgents"


def resource_root() -> Path:
    """Directory holding ui/, templates/, etc. The repo root in dev; the bundle dir when frozen by PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def ui_dir() -> Path:
    return resource_root() / "ui"


def templates_dir() -> Path:
    return resource_root() / "templates" / "agents"


def default_data_dir() -> Path:
    # Local (not roaming) app data on Windows: SQLite doesn't belong in a roaming profile.
    return Path(user_data_dir(APP_NAME, appauthor=False, roaming=False))


def default_log_dir() -> Path:
    return Path(user_log_dir(APP_NAME, appauthor=False))
