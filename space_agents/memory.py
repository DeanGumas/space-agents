"""Agent memory (PLAN.md §3.4): short notes an agent keeps between runs and chats.

    remember("po-45821-delay", "Reported the slip to Oct 14 on Oct 19; waiting on Acme's partial-shipment answer")
    recall("45821")        → matching notes          forget("po-45821-delay")   → the situation is resolved

Notes are keyed: remembering the same key again replaces the note, so an agent updates what it knows instead of
piling up duplicates. The most recent notes go into the agent's system prompt for every chat and run; `recall`
searches the rest.

Notes are written by the model, so a poisoned document could try to plant standing orders ("always …"). The prompt
therefore presents them as the agent's own earlier notes (facts, not instructions), and the user can see and delete
every note in the agent's Advanced tab. Only available when the agent's memory is on (`memory.enabled`).
"""

from __future__ import annotations

import re
from typing import Any

from space_agents.store import Store, now_iso

MAX_NOTES = 200
MAX_KEY = 80
MAX_NOTE = 500
PROMPT_NOTES = 40  # most recent notes shown in the system prompt…
PROMPT_CHARS = 4_000  # …within this budget; recall finds the rest
RECALL_RESULTS = 10

MEMORY_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save a short note for your future runs and chats, e.g. what you already reported or a preference "
                "the user stated. Saving the same key again replaces that note."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "A short id for the note, e.g. 'po-45821-delay'."},
                    "note": {"type": "string", "description": f"The note (up to {MAX_NOTE} characters)."},
                },
                "required": ["key", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Search all your notes (only the most recent are in your instructions).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Words to look for, e.g. a PO number."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "Delete a note that's no longer true or needed, e.g. once an issue is resolved.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },
]


def normalize_key(key: str) -> str:
    key = re.sub(r"[^a-z0-9._-]+", "-", str(key).strip().lower()).strip("-")
    return key[:MAX_KEY]


class AgentMemory:
    def __init__(self, store: Store, agent_id: str):
        self._store = store
        self.agent_id = agent_id

    def notes(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Newest first."""
        sql = "SELECT * FROM agent_memory WHERE agent_id = ? ORDER BY updated_at DESC, id DESC"
        params: tuple = (self.agent_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return self._store.query(sql, params)

    def count(self) -> int:
        row = self._store.query_one("SELECT COUNT(*) AS n FROM agent_memory WHERE agent_id = ?", (self.agent_id,))
        return row["n"] if row else 0

    def remember(self, key: str, note: str, message_id: int | None = None) -> str:
        """Save or replace a note. Returns "saved" or "updated". Raises ValueError with a message for the model."""
        key, note = normalize_key(key), " ".join(str(note).split())
        if not key:
            raise ValueError("remember needs a key, e.g. 'po-45821-delay'.")
        if not note:
            raise ValueError("remember needs a note. To delete a note, use forget.")
        if len(note) > MAX_NOTE:
            raise ValueError(f"Notes are limited to {MAX_NOTE} characters; this one is {len(note)}. Shorten it.")
        exists = self._store.query_one(
            "SELECT id FROM agent_memory WHERE agent_id = ? AND key = ?", (self.agent_id, key)
        )
        if not exists and self.count() >= MAX_NOTES:
            raise ValueError(f"Memory is full ({MAX_NOTES} notes). Forget notes you no longer need first.")
        now = now_iso()
        self._store.execute(
            "INSERT INTO agent_memory (agent_id, key, note, message_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (agent_id, key) DO UPDATE SET note = excluded.note,"
            " message_id = excluded.message_id, updated_at = excluded.updated_at",
            (self.agent_id, key, note, message_id, now, now),
        )
        return "updated" if exists else "saved"

    def forget(self, key: str) -> bool:
        cur = self._store.execute(
            "DELETE FROM agent_memory WHERE agent_id = ? AND key = ?", (self.agent_id, normalize_key(key))
        )
        return cur.rowcount > 0

    def delete(self, note_id: int) -> bool:
        cur = self._store.execute("DELETE FROM agent_memory WHERE agent_id = ? AND id = ?", (self.agent_id, note_id))
        return cur.rowcount > 0

    def clear(self) -> int:
        return self._store.execute("DELETE FROM agent_memory WHERE agent_id = ?", (self.agent_id,)).rowcount

    def recall(self, query: str) -> list[dict[str, Any]]:
        """Notes matching the most query words (in key or note), best first; the newest notes if the query is empty."""
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
        if not words:
            return self.notes(RECALL_RESULTS)
        scored = []
        for n in self.notes():
            text = f"{n['key']} {n['note']}".lower()
            score = sum(w in text for w in words)
            if score:
                scored.append((score, n))
        scored.sort(key=lambda pair: pair[0], reverse=True)  # stable: newest first among equals
        return [n for _, n in scored[:RECALL_RESULTS]]


def format_notes(notes: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {n['key']} ({n['updated_at'][:10]}): {n['note']}" for n in notes)


def prompt_notes(memory: AgentMemory) -> tuple[list[dict[str, Any]], int]:
    """The newest notes that fit the prompt budget, and how many notes there are in all."""
    shown, used = [], 0
    for n in memory.notes(PROMPT_NOTES):
        used += len(n["key"]) + len(n["note"]) + 20
        if used > PROMPT_CHARS:
            break
        shown.append(n)
    return shown, memory.count()
