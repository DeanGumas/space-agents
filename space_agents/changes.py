"""Proposed changes and file history (PLAN.md §2.10).

Agents never write user files directly. Every file an agent produces is staged as a *proposed change*; depending on
the agent's `file_changes` policy it's applied right away or waits for the user to click Apply. Applying:
  1. re-checks the target's hash (if the file changed since the proposal → "conflicted", the user decides)
  2. snapshots the current file into history/ (restorable with Undo)
  3. writes via temp file + atomic rename
Nothing is ever deleted: a created file can't be "undone" (that would be a delete); a modified file can be restored.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from space_agents.core import EventBus
from space_agents.documents.library import file_sha256
from space_agents.manifest import FileChanges
from space_agents.platform.marking import mark_as_downloaded
from space_agents.store import NotFound, Store, now_iso
from space_agents.tools.access import display_path

log = logging.getLogger(__name__)

HISTORY_DAYS = 30


class ChangeError(ValueError):
    pass


class ChangeManager:
    def __init__(self, store: Store, data_dir: Path, events: EventBus, mark_files: bool = True):
        self._store = store
        self._events = events
        self._mark_files = mark_files  # tag applied files as downloaded (setting `mark_agent_files`)
        self.staging_dir = data_dir / "staging"
        self.history_dir = data_dir / "history"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self._revising: set[str] = set()  # agents revising after the user discarded a change with feedback
        # Other things waiting on the user, merged into attention(): agent_id -> run reports (runtime/notices.py).
        self.extra_attention: Callable[[], dict[str, list[dict[str, Any]]]] | None = None

    # --- queries ------------------------------------------------------------------------------------------------

    def _row(self, change_id: str) -> dict[str, Any]:
        row = self._store.query_one("SELECT * FROM changes WHERE id = ?", (change_id,))
        if row is None:
            raise NotFound(change_id)
        return row

    def view(self, row: dict[str, Any]) -> dict[str, Any]:
        target = Path(row["target"])
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "message_id": row["message_id"],
            "name": target.name,
            "target": display_path(target),
            "kind": row["kind"],
            "status": row["status"],
            "summary": json.loads(row["summary"] or "{}"),
            "can_undo": row["status"] == "applied" and row["version_id"] is not None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get(self, change_id: str) -> dict[str, Any]:
        return self.view(self._row(change_id))

    def for_messages(self, message_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not message_ids:
            return {}
        rows = self._store.query(
            f"SELECT * FROM changes WHERE message_id IN ({','.join('?' * len(message_ids))}) ORDER BY created_at",
            tuple(message_ids),
        )
        out: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["message_id"], []).append(self.view(r))
        return out

    def pending_for(self, message_id: int | None, target: Path) -> dict[str, Any] | None:
        """An unapplied proposal for `target` made earlier in the same message (the agent is iterating on it)."""
        if message_id is None:
            return None
        return self._store.query_one(
            "SELECT * FROM changes WHERE message_id = ? AND target = ? AND status IN ('pending', 'conflicted')"
            " ORDER BY created_at DESC LIMIT 1",
            (message_id, str(target)),
        )

    def target_path(self, change_id: str) -> Path:
        return Path(self._row(change_id)["target"])

    # --- lifecycle ----------------------------------------------------------------------------------------------

    def propose(
        self,
        *,
        agent_id: str,
        message_id: int | None,
        tool_call_id: int | None,
        target: Path,
        source: Path,
        summary: dict[str, Any],
        policy: FileChanges,
    ) -> dict[str, Any]:
        previous = self.pending_for(message_id, target)
        if previous is not None:
            # The agent revised its own pending proposal: replace it so there's one card per file per message.
            staged = Path(previous["staged"])
            staged.unlink(missing_ok=True)
            shutil.move(str(source), staged)
            self._set(previous["id"], summary=json.dumps(summary), status="pending", tool_call_id=tool_call_id)
            change = self.get(previous["id"])
            self._emit(change)
            return change

        change_id = uuid.uuid4().hex
        staged = self.staging_dir / change_id / target.name
        staged.parent.mkdir(parents=True)
        shutil.move(str(source), staged)
        exists = target.exists()
        now = now_iso()
        self._store.execute(
            "INSERT INTO changes (id, agent_id, message_id, tool_call_id, target, kind, staged, base_sha256, summary,"
            " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                change_id,
                agent_id,
                message_id,
                tool_call_id,
                str(target),
                "modify" if exists else "create",
                str(staged),
                file_sha256(target) if exists else None,
                json.dumps(summary),
                now,
                now,
            ),
        )
        auto = policy.existing_files == "allow_with_backup" if exists else policy.new_files == "allow"
        if auto:
            return self.apply(change_id)
        change = self.get(change_id)
        self._emit(change)
        return change

    def apply(self, change_id: str, force: bool = False) -> dict[str, Any]:
        row = self._row(change_id)
        if row["status"] not in ("pending", "conflicted"):
            raise ChangeError(f"This change is already {row['status']}.")
        target, staged = Path(row["target"]), Path(row["staged"])
        if not staged.exists():
            raise ChangeError("The proposed file is missing; ask the agent to make the change again.")
        current = file_sha256(target) if target.exists() else None
        if current != row["base_sha256"] and not force:
            self._set(change_id, status="conflicted")
            return self._emit(self.get(change_id))
        version_id = self._snapshot(target, change_id) if target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_copy(staged, target)
        if self._mark_files:
            mark_as_downloaded(target)
        shutil.rmtree(staged.parent, ignore_errors=True)
        self._set(change_id, status="applied", version_id=version_id)
        log.info("Applied change %s → %s", change_id, target)
        return self._emit(self.get(change_id))

    def discard(self, change_id: str) -> dict[str, Any]:
        row = self._row(change_id)
        if row["status"] not in ("pending", "conflicted"):
            raise ChangeError(f"This change is already {row['status']}.")
        shutil.rmtree(Path(row["staged"]).parent, ignore_errors=True)
        self._set(change_id, status="discarded")
        return self._emit(self.get(change_id))

    def undo(self, change_id: str) -> dict[str, Any]:
        """Restore the version a change replaced. The current content is snapshotted first, so nothing is lost."""
        row = self._row(change_id)
        if row["status"] != "applied" or row["version_id"] is None:
            raise ChangeError("Only applied changes to existing files can be undone.")
        version = self._store.query_one("SELECT * FROM file_versions WHERE id = ?", (row["version_id"],))
        blob = self.history_dir / version["blob"] if version else None
        if blob is None or not blob.exists():
            raise ChangeError("The previous version is no longer in history.")
        target = Path(row["target"])
        if target.exists():
            self._snapshot(target, change_id)
        self._atomic_copy(blob, target)
        self._set(change_id, status="undone")
        return self._emit(self.get(change_id))

    def prune_history(self, days: int = HISTORY_DAYS) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        old = self._store.query("SELECT id, blob FROM file_versions WHERE created_at < ?", (cutoff,))
        self._store.execute("DELETE FROM file_versions WHERE created_at < ?", (cutoff,))
        live = {r["blob"] for r in self._store.query("SELECT blob FROM file_versions")}
        for r in old:
            if r["blob"] not in live:
                (self.history_dir / r["blob"]).unlink(missing_ok=True)
        return len(old)

    # --- helpers ------------------------------------------------------------------------------------------------

    def _snapshot(self, path: Path, change_id: str) -> int:
        sha = file_sha256(path)
        blob = self.history_dir / sha
        if not blob.exists():
            shutil.copy2(path, blob)
        cur = self._store.execute(
            "INSERT INTO file_versions (path, blob, sha256, size, change_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(path), sha, sha, path.stat().st_size, change_id, now_iso()),
        )
        return cur.lastrowid

    @staticmethod
    def _atomic_copy(src: Path, dest: Path) -> None:
        tmp = dest.with_name(f".{dest.name}.space-agents.tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)

    def _set(self, change_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._store.execute(
            f"UPDATE changes SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), now_iso(), change_id)
        )

    def _emit(self, change: dict[str, Any]) -> dict[str, Any]:
        self._events.emit("change.updated", agent_id=change["agent_id"], message_id=change["message_id"], change=change)
        self.emit_attention()
        return change

    # --- "needs your attention" (Activity tab, sidebar badge) -----------------------------------------------------

    def attention(self) -> list[dict[str, Any]]:
        """One entry per agent waiting on the user: it has unapplied proposals, it's revising after feedback, or
        (through `extra_attention`) it has notable run reports the user hasn't seen.

        An entry disappears once nothing is pending, the agent has finished working, and its reports were seen.
        """
        rows = self._store.query(
            "SELECT * FROM changes WHERE status IN ('pending', 'conflicted') ORDER BY created_at, rowid"
        )
        entries: dict[str, dict[str, Any]] = {}
        for r in rows:
            entry = entries.setdefault(r["agent_id"], {"agent_id": r["agent_id"], "changes": [], "updated_at": ""})
            entry["changes"].append(self.view(r))
            entry["updated_at"] = max(entry["updated_at"], r["updated_at"])
        for agent_id in self._revising:
            entries.setdefault(agent_id, {"agent_id": agent_id, "changes": [], "updated_at": now_iso()})
        for agent_id, reports in (self.extra_attention() if self.extra_attention else {}).items():
            entry = entries.setdefault(agent_id, {"agent_id": agent_id, "changes": [], "updated_at": ""})
            entry["reports"] = reports
            entry["updated_at"] = max(entry["updated_at"], *(r["finished_at"] or "" for r in reports))
        for entry in entries.values():
            entry["revising"] = entry["agent_id"] in self._revising
            entry.setdefault("reports", [])
        return sorted(entries.values(), key=lambda e: e["updated_at"], reverse=True)

    def set_revising(self, agent_id: str, revising: bool) -> None:
        (self._revising.add if revising else self._revising.discard)(agent_id)
        self.emit_attention()

    def emit_attention(self) -> None:
        self._events.emit("attention.updated", attention=self.attention())
