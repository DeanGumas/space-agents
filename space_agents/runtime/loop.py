"""The agent loop (PLAN.md §3.2): model ⇄ tools until the model answers, streaming progress to the UI.

Events emitted (see docs/architecture.md):
  agent.status   responding / idle
  message.delta  streamed answer text
  tool.started   a tool call began (shown as an activity step)
  tool.done      it finished (with a short result preview)
  change.updated emitted by ChangeManager for proposed changes
  message.done   final message (content, status, usage)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from space_agents.changes import ChangeManager
from space_agents.config import Config
from space_agents.core import EventBus
from space_agents.documents.library import DocumentLibrary
from space_agents.llm.client import ChatClient, LLMError
from space_agents.manifest import Manifest
from space_agents.runtime.prompts import system_prompt
from space_agents.sandbox.pyodide import PyodideSandbox
from space_agents.store import Store, now_iso
from space_agents.tools.registry import ToolBox

log = logging.getLogger(__name__)

HISTORY_MESSAGES = 20
CHANGE_OUTCOMES = {
    "pending": "proposed, still waiting for the user's review",
    "applied": "applied by the user",
    "discarded": "discarded by the user",
    "conflicted": "not applied: the file changed after it was proposed",
    "undone": "applied, then undone by the user",
}
TOOL_RESULT_CHARS = 12_000


class AgentRunner:
    def __init__(
        self,
        *,
        store: Store,
        events: EventBus,
        docs: DocumentLibrary,
        sandbox: PyodideSandbox,
        changes: ChangeManager,
        config: Config,
        client_factory: Callable[[Config], ChatClient] | None = None,
    ):
        self._store = store
        self._events = events
        self._docs = docs
        self._sandbox = sandbox
        self._changes = changes
        self.config = config  # replaced by the bridge when settings change
        self._client_factory = client_factory or (lambda c: ChatClient(c.llm_base_url, c.api_key))
        self._client: ChatClient | None = None
        self._client_key: tuple[str, str] | None = None

    def _client_for(self, config: Config) -> ChatClient:
        key = (config.llm_base_url, config.api_key)
        if self._client is None or self._client_key != key:
            self._client, self._client_key = self._client_factory(config), key
        return self._client

    def _history(self, agent_id: str, before_id: int) -> list[dict[str, Any]]:
        rows = self._store.query(
            "SELECT id, role, content FROM (SELECT * FROM messages WHERE agent_id = ? AND id < ? AND content != ''"
            " ORDER BY id DESC LIMIT ?) ORDER BY id",
            (agent_id, before_id, HISTORY_MESSAGES),
        )
        # Tell the model what became of the file changes it proposed earlier (applied, discarded, …): the user acts on
        # them in the UI, outside the conversation.
        changes = self._changes.for_messages([r["id"] for r in rows if r["role"] == "agent"])
        history = []
        for r in rows:
            content = r["content"]
            if r["role"] == "agent" and changes.get(r["id"]):
                notes = "\n".join(
                    f"- {'Update' if c['kind'] == 'modify' else 'Create'} {c['target']}: {CHANGE_OUTCOMES[c['status']]}"
                    for c in changes[r["id"]]
                )
                content += f"\n\n[File changes from this reply]\n{notes}"
            history.append({"role": "assistant" if r["role"] == "agent" else "user", "content": content})
        return history

    async def chat(self, agent: Manifest, message_id: int) -> None:
        """Answer the latest user message for `agent`, writing into the (already created) agent message."""
        config = self.config
        model = agent.model.name or (config.models[0] if config.models else "")
        tools = ToolBox(
            agent=agent,
            message_id=message_id,
            docs=self._docs,
            sandbox=self._sandbox,
            changes=self._changes,
            protected=[config.data_dir],
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(agent, tools.access)},
            *self._history(agent.id, message_id),
        ]
        text, usage, status = "", {}, "done"
        deadline = time.monotonic() + agent.limits.max_minutes * 60
        calls_left = agent.limits.max_tool_calls

        async def on_text(delta: str) -> None:
            nonlocal text
            text += delta
            self._events.emit("message.delta", agent_id=agent.id, message_id=message_id, delta=delta)

        self._events.emit("agent.status", agent_id=agent.id, status="responding")
        try:
            if not model:
                raise LLMError("No model is set. Pick one in the agent's Model tab.")
            client = self._client_for(config)
            while True:
                allow_tools = calls_left > 0 and time.monotonic() < deadline
                result = await client.chat(
                    model=model,
                    messages=messages,
                    tools=tools.specs() if allow_tools else None,
                    temperature=agent.model.temperature,
                    reasoning_effort=config.reasoning_effort or None,
                    on_text=on_text,
                )
                for k, v in result.usage.items():
                    usage[k] = usage.get(k, 0) + v
                if not result.tool_calls:
                    break
                if text and not text.endswith("\n"):
                    await on_text("\n\n")  # keep a preamble separate from the final answer
                messages.append(
                    {
                        "role": "assistant",
                        "content": result.content or None,
                        "tool_calls": [
                            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                            for c in result.tool_calls
                        ],
                    }
                )
                for call in result.tool_calls:
                    calls_left -= 1
                    content = await self._run_tool(agent, message_id, tools, call)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                if calls_left <= 0 or time.monotonic() >= deadline:
                    messages.append(
                        {"role": "user", "content": "(Tool limit reached. Answer now with what you have; no tools.)"}
                    )
            if not text.strip():
                text = "(The model returned an empty answer.)"
        except asyncio.CancelledError:
            status = "error"
            text = (text + "\n\n_Stopped._").strip()
            raise
        except LLMError as e:
            status = "error"
            text = (text + f"\n\n⚠️ {e}").strip()
        except Exception:
            log.exception("Agent run failed")
            status = "error"
            text = (text + "\n\n⚠️ Something went wrong. Details are in the log.").strip()
        finally:
            self._store.update_message(message_id, content=text, status=status, usage=usage)
            if usage:
                log.info("Usage for %s (%s): %s", agent.id, model, usage)
            self._events.emit("message.done", agent_id=agent.id, message=self._store.get_message(message_id))
            self._events.emit("agent.status", agent_id=agent.id, status="idle")

    async def _run_tool(self, agent: Manifest, message_id: int, tools: ToolBox, call) -> str:
        try:
            args = call.parsed_arguments()
        except ValueError as e:
            args, bad_args = {}, str(e)
        else:
            bad_args = None
        cur = self._store.execute(
            "INSERT INTO tool_calls (message_id, agent_id, name, args, summary, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, agent.id, call.name, json.dumps(args), _pending_summary(call.name, args), now_iso()),
        )
        row_id = cur.lastrowid
        self._events.emit("tool.started", agent_id=agent.id, message_id=message_id, call=self.tool_call(row_id))
        started = time.monotonic()
        if bad_args:
            outcome_result, summary, ok = f"Error: {bad_args}", _pending_summary(call.name, args), False
        else:
            outcome = await tools.call(call.name, args, tool_call_id=row_id)
            outcome_result, summary, ok = outcome.result, outcome.summary, outcome.ok
        if len(outcome_result) > TOOL_RESULT_CHARS:
            outcome_result = outcome_result[:TOOL_RESULT_CHARS] + "\n…(truncated; ask for less, e.g. a page range)"
        self._store.execute(
            "UPDATE tool_calls SET summary = ?, result = ?, status = ?, duration_ms = ? WHERE id = ?",
            (summary, outcome_result, "done" if ok else "error", int((time.monotonic() - started) * 1000), row_id),
        )
        self._events.emit("tool.done", agent_id=agent.id, message_id=message_id, call=self.tool_call(row_id))
        return outcome_result

    def tool_call(self, row_id: int) -> dict[str, Any]:
        row = self._store.query_one("SELECT * FROM tool_calls WHERE id = ?", (row_id,))
        return tool_call_view(row)

    def tool_calls_for(self, message_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not message_ids:
            return {}
        rows = self._store.query(
            f"SELECT * FROM tool_calls WHERE message_id IN ({','.join('?' * len(message_ids))}) ORDER BY id",
            tuple(message_ids),
        )
        out: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["message_id"], []).append(tool_call_view(r))
        return out


def tool_call_view(row: dict[str, Any]) -> dict[str, Any]:
    args = json.loads(row["args"] or "{}")
    return {
        "id": row["id"],
        "name": row["name"],
        "summary": row["summary"],
        "status": row["status"],
        "duration_ms": row["duration_ms"],
        "code": args.get("code") if row["name"] == "run_python" else None,
        "args": {k: v for k, v in args.items() if k != "code"},
        "result": row["result"][:4000],
    }


def _pending_summary(name: str, args: dict[str, Any]) -> str:
    path = Path(str(args.get("path") or "")).name
    return {
        "list_files": "Looking through files",
        "read_document": f"Reading {path}" if path else "Reading a document",
        "search_document": f"Searching {path}" if path else "Searching a document",
        "run_python": str(args.get("description") or "Running analysis")[:200],
    }.get(name, name)
