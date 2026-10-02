"""Phase 0 stand-in for the agent loop: streams a canned reply so the chat UI and the event path can be exercised
without a model. Replaced by runtime/loop.py in Phase 1."""

from __future__ import annotations

import asyncio
import logging
import random
import re

from space_agents.core import EventBus
from space_agents.manifest import Manifest
from space_agents.store import Store

log = logging.getLogger(__name__)


def _reply_text(agent: Manifest, user_text: str) -> str:
    sources = [s.id for s in agent.mcp_servers]
    folders = [f"`{f.path}` ({'read/write' if f.access == 'read_write' else 'read-only'})" for f in agent.filesystem]
    lines = [
        f"Hi! I'm **{agent.name}**, but I'm not connected to a model yet, so this is a placeholder reply.",
        "",
        "Here's what I *would* work with:",
        f"- **Model:** {agent.model.name or '_not chosen_'}",
        f"- **Data sources:** {', '.join(sources) if sources else '_none yet_'}",
        f"- **Folders:** {', '.join(folders) if folders else '_none yet_'}",
    ]
    if agent.schedule.enabled:
        lines.append(
            f"- **Schedule:** every {agent.schedule.every_minutes} min, {agent.schedule.active_hours.start}"
            f"–{agent.schedule.active_hours.end}"
        )
    quoted = "\n".join(f"> {line}" for line in user_text.strip().splitlines()[:6])
    lines += ["", "You said:", quoted]
    return "\n".join(lines)


async def dummy_reply(store: Store, events: EventBus, agent: Manifest, user_text: str, message_id: int) -> None:
    text = _reply_text(agent, user_text)
    events.emit("agent.status", agent_id=agent.id, status="responding")
    sent = ""
    try:
        await asyncio.sleep(0.4)  # "thinking"
        for chunk in re.findall(r"\S+\s*|\s+", text):
            sent += chunk
            events.emit("message.delta", agent_id=agent.id, message_id=message_id, delta=chunk)
            await asyncio.sleep(random.uniform(0.01, 0.04))
        store.update_message(message_id, content=sent, status="done")
        events.emit("message.done", agent_id=agent.id, message=store.get_message(message_id))
    except asyncio.CancelledError:
        store.update_message(message_id, content=sent, status="error")
        raise
    except Exception:
        log.exception("Dummy reply failed")
        store.update_message(message_id, content=sent, status="error")
        events.emit("message.done", agent_id=agent.id, message=store.get_message(message_id))
    finally:
        events.emit("agent.status", agent_id=agent.id, status="idle")
