"""Which run reports deserve the user's attention, and how to say so (PLAN.md §3.3, Phase 3 step d).

A finished run is **notable** when:
- its report has a *new or changed* finding at or above the agent's `notifications.min_level` (info / action /
  urgent). Findings marked unchanged ("still open") never count, so an hourly agent doesn't re-alert about the
  same delay every hour; or
- it failed: an unattended agent failing silently is worse than a notification.

Notable runs the user hasn't seen get a desktop notification (if the agent's notifications are on and the user isn't
already looking at that agent), and appear in Activity until the user opens the conversation or dismisses them.
"""

from __future__ import annotations

from typing import Any

from space_agents.manifest import Manifest
from space_agents.runtime.reports import LEVEL_LABELS, LEVELS, fresh_items
from space_agents.store import Store

MAX_LISTED = 3


def notable_items(agent: Manifest, run: dict[str, Any]) -> list[dict[str, Any]]:
    """New or changed findings at or above the agent's minimum level, most serious first."""
    floor = LEVELS.index(agent.notifications.min_level)
    items = [i for i in fresh_items(run.get("report")) if LEVELS.index(i["level"]) >= floor]
    return sorted(items, key=lambda i: LEVELS.index(i["level"]), reverse=True)


def is_notable(agent: Manifest, run: dict[str, Any]) -> bool:
    return run["status"] == "error" or bool(notable_items(agent, run))


def failure_reason(message_content: str) -> str:
    """The run's error line ("⚠️ The model server rejected the API key."), without the decoration."""
    lines = [line.strip() for line in message_content.splitlines() if line.strip()]
    reason = next((line for line in reversed(lines) if line.startswith("⚠️")), lines[-1] if lines else "")
    return reason.removeprefix("⚠️").strip().strip("_") or "Something went wrong."


def notification(agent: Manifest, run: dict[str, Any], message_content: str, pending_changes: int) -> tuple[str, str]:
    """(title, body) for the desktop notification, e.g. ("PO Manager: action needed", "PO 45821 slipped (+1 more)")."""
    if run["status"] == "error":
        return f"{agent.name}: the run failed", failure_reason(message_content)
    items = notable_items(agent, run)
    level = items[0]["level"]
    title = f"{agent.name}: {LEVEL_LABELS[level].lower() if level != 'info' else 'new findings'}"
    body = items[0]["title"] + (f" (+{len(items) - 1} more)" if len(items) > 1 else "")
    if pending_changes:
        body += f" · {pending_changes} file change{'s' if pending_changes > 1 else ''} to approve"
    return title, body


def reports_needing_attention(store: Store) -> dict[str, list[dict[str, Any]]]:
    """Per agent: its notable runs the user hasn't seen, for the Activity inbox (oldest first)."""
    agents = {a.id: a for a in store.list_agents()}
    out: dict[str, list[dict[str, Any]]] = {}
    for run in store.unseen_runs():
        agent = agents.get(run["agent_id"])
        if agent is None or not is_notable(agent, run):
            continue
        entry = {
            "run_id": run["id"],
            "message_id": run["message_id"],
            "trigger": run["trigger"],
            "status": run["status"],
            "finished_at": run["finished_at"],
        }
        if run["status"] == "error":
            entry["error"] = failure_reason(store.get_message(run["message_id"])["content"])
        else:
            items = notable_items(agent, run)
            entry.update(
                summary=run["report"]["summary"], items=items[:MAX_LISTED], more=max(0, len(items) - MAX_LISTED)
            )
        out.setdefault(agent.id, []).append(entry)
    return out
