"""Persistence: agent manifests (JSON files), avatar images, and chat history (SQLite).

Layout inside the data directory:

    agents/<id>.json     one manifest per agent (portable, no secrets)
    avatars/<id>.png     optional avatar picture (already cropped/resized by the UI)
    space.db             SQLite: messages, tool_calls, changes, file_versions, doc_cache/doc_pages/doc_fts

Thread-safety: bridge calls arrive on pywebview worker threads and the core loop runs on its own thread, so all
SQLite access goes through one connection guarded by a lock. Manifest writes are atomic (temp file + rename).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from space_agents.manifest import Manifest, ManifestError, slugify
from space_agents.settings import write_json_atomic

log = logging.getLogger(__name__)

MAX_AVATAR_BYTES = 512 * 1024  # the UI sends a 256x256 PNG, typically well under 100 KB
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

SCHEMA = [
    # v1
    """
    CREATE TABLE messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id    TEXT NOT NULL,
        role        TEXT NOT NULL CHECK (role IN ('user', 'agent', 'system')),
        content     TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('streaming', 'done', 'error')),
        created_at  TEXT NOT NULL
    );
    CREATE INDEX messages_agent ON messages (agent_id, id);
    """,
    # v2: agent loop activity, proposed changes, file history, document cache
    """
    ALTER TABLE messages ADD COLUMN usage TEXT;
    CREATE TABLE tool_calls (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id  INTEGER NOT NULL,
        agent_id    TEXT NOT NULL,
        name        TEXT NOT NULL,
        args        TEXT NOT NULL,
        summary     TEXT NOT NULL DEFAULT '',
        result      TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'error')),
        started_at  TEXT NOT NULL,
        duration_ms INTEGER
    );
    CREATE INDEX tool_calls_message ON tool_calls (message_id, id);
    CREATE TABLE changes (
        id           TEXT PRIMARY KEY,
        agent_id     TEXT NOT NULL,
        message_id   INTEGER,
        tool_call_id INTEGER,
        target       TEXT NOT NULL,
        kind         TEXT NOT NULL CHECK (kind IN ('create', 'modify')),
        staged       TEXT NOT NULL,
        base_sha256  TEXT,
        summary      TEXT NOT NULL DEFAULT '{}',
        status       TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'discarded', 'conflicted', 'undone')),
        version_id   INTEGER,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX changes_message ON changes (message_id);
    CREATE TABLE file_versions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        path        TEXT NOT NULL,
        blob        TEXT NOT NULL,
        sha256      TEXT NOT NULL,
        size        INTEGER NOT NULL,
        change_id   TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE doc_cache (
        sha256      TEXT PRIMARY KEY,
        path        TEXT NOT NULL,
        size        INTEGER NOT NULL,
        mtime       REAL NOT NULL,
        kind        TEXT NOT NULL,
        page_count  INTEGER NOT NULL,
        info        TEXT NOT NULL DEFAULT '{}',
        used_at     TEXT NOT NULL
    );
    CREATE INDEX doc_cache_path ON doc_cache (path, size, mtime);
    CREATE TABLE doc_pages (
        sha256  TEXT NOT NULL,
        page    INTEGER NOT NULL,
        status  TEXT NOT NULL,
        text    TEXT NOT NULL,
        PRIMARY KEY (sha256, page)
    );
    CREATE VIRTUAL TABLE doc_fts USING fts5(sha256 UNINDEXED, page UNINDEXED, text);
    """,
]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class NotFound(KeyError):
    pass


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.agents_dir = data_dir / "agents"
        self.avatars_dir = data_dir / "avatars"
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.avatars_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(data_dir / "space.db", check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- generic query helpers (used by documents/, changes.py, runtime/) ---------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(sql, rows)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def _migrate(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(SCHEMA[version:], start=version + 1):
                log.info("Migrating database to schema v%d", i)
                self._db.executescript(f"BEGIN; {script}; PRAGMA user_version = {i}; COMMIT;")

    # --- agents -------------------------------------------------------------------------------------------------

    def _agent_path(self, agent_id: str) -> Path:
        return self.agents_dir / f"{agent_id}.json"

    def list_agents(self) -> list[Manifest]:
        agents = []
        for path in self.agents_dir.glob("*.json"):
            try:
                agents.append(Manifest.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, ManifestError) as e:
                log.warning("Skipping invalid agent file %s: %s", path.name, e)
        return sorted(agents, key=lambda m: (m.name.casefold(), m.id))

    def get_agent(self, agent_id: str) -> Manifest:
        path = self._agent_path(agent_id)
        if not path.exists():
            raise NotFound(agent_id)
        return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def new_agent_id(self, name: str) -> str:
        base = slugify(name)
        candidate, n = base, 2
        while self._agent_path(candidate).exists():
            candidate, n = f"{base}-{n}", n + 1
        return candidate

    def save_agent(self, manifest: Manifest, avatar_png: bytes | None = None) -> Manifest:
        """Create (when manifest.id is empty) or update an agent. Optionally store a new avatar picture."""
        with self._lock:
            if not manifest.id:
                manifest.id = self.new_agent_id(manifest.name)
            if avatar_png is not None:
                manifest.avatar.image = self._save_avatar(manifest.id, avatar_png)
            elif manifest.avatar.image is None:
                (self.avatars_dir / f"{manifest.id}.png").unlink(missing_ok=True)
            write_json_atomic(self._agent_path(manifest.id), manifest.to_dict())
            return manifest

    def delete_agent(self, agent_id: str) -> None:
        with self._lock:
            path = self._agent_path(agent_id)
            if not path.exists():
                raise NotFound(agent_id)
            path.unlink()
            (self.avatars_dir / f"{agent_id}.png").unlink(missing_ok=True)
            self._db.execute("DELETE FROM messages WHERE agent_id = ?", (agent_id,))
            self._db.execute("DELETE FROM tool_calls WHERE agent_id = ?", (agent_id,))
            self._db.execute("DELETE FROM changes WHERE agent_id = ? AND status = 'pending'", (agent_id,))

    # --- avatars ------------------------------------------------------------------------------------------------

    def _save_avatar(self, agent_id: str, png: bytes) -> str:
        if not png.startswith(PNG_MAGIC):
            raise ValueError("Avatar must be a PNG image.")
        if len(png) > MAX_AVATAR_BYTES:
            raise ValueError("Avatar image is too large.")
        path = self.avatars_dir / f"{agent_id}.png"
        path.write_bytes(png)
        return f"avatars/{agent_id}.png"

    def avatar_bytes(self, manifest: Manifest) -> bytes | None:
        if not manifest.avatar.image:
            return None
        path = (self.data_dir / manifest.avatar.image).resolve()
        if path.parent != self.avatars_dir.resolve() or not path.is_file():
            return None
        return path.read_bytes()

    def avatar_data_url(self, manifest: Manifest) -> str | None:
        data = self.avatar_bytes(manifest)
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii") if data else None

    @staticmethod
    def decode_png_data_url(data_url: str) -> bytes:
        prefix = "data:image/png;base64,"
        if not isinstance(data_url, str) or not data_url.startswith(prefix):
            raise ValueError("Avatar must be a PNG data URL.")
        try:
            return base64.b64decode(data_url[len(prefix) :], validate=True)
        except binascii.Error as e:
            raise ValueError("Avatar image data is corrupt.") from e

    # --- messages -----------------------------------------------------------------------------------------------

    def add_message(self, agent_id: str, role: str, content: str, status: str = "done") -> dict[str, Any]:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (agent_id, role, content, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (agent_id, role, content, status, now_iso()),
            )
            return self.get_message(cur.lastrowid)

    def get_message(self, message_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise NotFound(message_id)
        return dict(row)

    def update_message(
        self,
        message_id: int,
        *,
        content: str | None = None,
        status: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        with self._lock:
            if usage is not None:
                self._db.execute("UPDATE messages SET usage = ? WHERE id = ?", (json.dumps(usage), message_id))
            if content is not None:
                self._db.execute("UPDATE messages SET content = ? WHERE id = ?", (content, message_id))
            if status is not None:
                self._db.execute("UPDATE messages SET status = ? WHERE id = ?", (status, message_id))

    def list_messages(self, agent_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE agent_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (agent_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_messages(self) -> dict[str, dict[str, Any]]:
        """Most recent message per agent, for sidebar previews."""
        with self._lock:
            rows = self._db.execute(
                "SELECT m.* FROM messages m JOIN (SELECT agent_id, MAX(id) AS id FROM messages GROUP BY agent_id) l"
                " ON m.id = l.id"
            ).fetchall()
        return {r["agent_id"]: dict(r) for r in rows}

    def recover_interrupted(self) -> None:
        """Messages left 'streaming' by a crash or quit are marked as errors on startup."""
        with self._lock:
            self._db.execute("UPDATE messages SET status = 'error' WHERE status = 'streaming'")
