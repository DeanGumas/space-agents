"""The JS ↔ Python bridge: every public method here is callable from the UI as `pywebview.api.<name>(...)`.

Conventions
- pywebview runs each call on its own worker thread. Long-running work is handed to the core loop and reports
  progress through EventBus events instead of blocking the call.
- Every method returns an envelope: {"ok": true, "data": ...} or {"ok": false, "error": "...", "fields": {...}}.
  `fields` maps dotted manifest paths to messages so forms can highlight the right input. ui/js/api.js unwraps it.
- Only public methods are exposed. Keep all state in underscore-prefixed attributes: pywebview also walks public
  attributes and would expose their methods too.
"""

from __future__ import annotations

import concurrent.futures
import functools
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import webview

from space_agents import __version__, paths
from space_agents.catalog import load_catalog_file, load_templates
from space_agents.changes import ChangeError, ChangeManager
from space_agents.config import Config
from space_agents.core import Core, EventBus
from space_agents.manifest import Manifest, ManifestError
from space_agents.platform.appearance import open_in_file_manager
from space_agents.runtime.dummy import dummy_reply
from space_agents.runtime.loop import AgentRunner
from space_agents.sandbox.pyodide import PyodideSandbox
from space_agents.settings import UserSettings
from space_agents.store import NotFound, Store

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 20_000


class UserError(Exception):
    """An error whose message is safe and useful to show to the user as-is."""


def endpoint(fn: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
    @functools.wraps(fn)
    def wrapper(self: Api, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"ok": True, "data": fn(self, *args, **kwargs)}
        except ManifestError as e:
            return {"ok": False, "error": "Please fix the highlighted fields.", "fields": e.errors}
        except NotFound as e:
            return {"ok": False, "error": f"Not found: {e.args[0]}"}
        except (UserError, ChangeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        except Exception:
            log.exception("API call %s failed", fn.__name__)
            return {"ok": False, "error": "Something went wrong. Details are in the log."}

    return wrapper


def to_display_path(path: str) -> str:
    """Store folders under the home directory as ~/... so manifests stay portable between users."""
    p = Path(path).expanduser().resolve()
    try:
        return "~/" + p.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(p)


class Api:
    def __init__(
        self,
        *,
        config: Config,
        reload_config: Callable[[], Config],
        store: Store,
        settings: UserSettings,
        core: Core,
        events: EventBus,
        sandbox: PyodideSandbox,
        runner: AgentRunner,
        changes: ChangeManager,
        on_ui_ready: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._config = config
        self._reload_config = reload_config
        self._store = store
        self._settings = settings
        self._core = core
        self._events = events
        self._sandbox = sandbox
        self._runner = runner
        self._changes = changes
        self._on_ui_ready = on_ui_ready
        self._window: webview.Window | None = None
        self._busy: set[str] = set()
        self._tasks: dict[str, concurrent.futures.Future] = {}

    def _attach_window(self, window: webview.Window) -> None:
        self._window = window

    # --- lifecycle ----------------------------------------------------------------------------------------------

    @endpoint
    def ping(self) -> dict[str, Any]:
        return {"pong": True, "version": __version__}

    @endpoint
    def ui_ready(self, info: dict[str, Any] | None = None) -> None:
        """Called by the UI once it has rendered; from here on, events are delivered."""
        window = self._window
        if window is not None:
            self._events.attach(
                lambda payload: window.run_js(f"window.__spaceEvents && window.__spaceEvents({payload})")
            )
            self._sandbox.attach(window.run_js)
        log.info("UI ready: %s", info or {})
        if self._on_ui_ready:
            self._on_ui_ready(info or {})

    @endpoint
    def bootstrap(self) -> dict[str, Any]:
        """Everything the UI needs on startup, in one round trip."""
        return {
            "version": __version__,
            "platform": sys.platform,
            "theme": self._settings.theme,
            "config": self._config.describe(),
            "data_dir": str(self._config.data_dir),
            "agents": self._agent_views(),
            "blank_manifest": Manifest(name="").to_dict(),
            "templates": load_templates(paths.templates_dir()),
            "catalog": load_catalog_file(self._config.catalog_file),
            "models": self._config.models,
            "llm": {
                "ready": self._config.llm_ready,
                "default_model": self._config.models[0] if self._config.models else "",
            },
        }

    # --- agents -------------------------------------------------------------------------------------------------

    def _agent_view(self, m: Manifest, last: dict[str, Any] | None) -> dict[str, Any]:
        view = m.to_dict()
        view["avatar_url"] = self._store.avatar_data_url(m)
        view["status"] = "responding" if m.id in self._busy else "idle"
        view["last_message"] = (
            {"role": last["role"], "preview": last["content"][:160], "created_at": last["created_at"]} if last else None
        )
        return view

    def _agent_views(self) -> list[dict[str, Any]]:
        last = self._store.last_messages()
        return [self._agent_view(m, last.get(m.id)) for m in self._store.list_agents()]

    @endpoint
    def list_agents(self) -> list[dict[str, Any]]:
        return self._agent_views()

    @endpoint
    def save_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create (no id) or update (with id) an agent.

        Avatar handling: the UI never sets `avatar.image` (a file path) directly. It sends either
        `avatar.image_data` (a new PNG data URL) or `avatar.keep_image: true` to keep the current picture.
        """
        if not isinstance(payload, dict):
            raise UserError("Invalid agent data.")
        payload = dict(payload)
        avatar = dict(payload.get("avatar") or {})
        image_data = avatar.pop("image_data", None)
        keep_image = avatar.pop("keep_image", False)
        avatar["image"] = None
        payload["avatar"] = avatar

        manifest = Manifest.from_dict(payload)
        if manifest.id:
            existing = self._store.get_agent(manifest.id)
            if keep_image:
                manifest.avatar.image = existing.avatar.image
        png = Store.decode_png_data_url(image_data) if image_data else None
        saved = self._store.save_agent(manifest, png)
        return self._agent_view(saved, self._store.last_messages().get(saved.id))

    @endpoint
    def delete_agent(self, agent_id: str) -> None:
        if agent_id in self._busy:
            raise UserError("This agent is busy. Try again in a moment.")
        self._store.delete_agent(agent_id)

    # --- chat ---------------------------------------------------------------------------------------------------

    @endpoint
    def list_messages(self, agent_id: str) -> list[dict[str, Any]]:
        messages = self._store.list_messages(agent_id)
        ids = [m["id"] for m in messages if m["role"] == "agent"]
        calls, changes = self._runner.tool_calls_for(ids), self._changes.for_messages(ids)
        for m in messages:
            m["tool_calls"] = calls.get(m["id"], [])
            m["changes"] = changes.get(m["id"], [])
        return messages

    @endpoint
    def send_message(self, agent_id: str, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise UserError("Message is empty.")
        if len(text) > MAX_MESSAGE_CHARS:
            raise UserError(f"Message is too long (max {MAX_MESSAGE_CHARS:,} characters).")
        if agent_id in self._busy:
            raise UserError("This agent is still responding.")
        return self._start_reply(self._store.get_agent(agent_id), text)

    def _start_reply(self, agent: Manifest, text: str) -> dict[str, Any]:
        """Store the user's message and start the agent's reply in the background."""
        agent_id = agent.id
        user_msg = self._store.add_message(agent_id, "user", text)
        reply = self._store.add_message(agent_id, "agent", "", status="streaming")
        reply["tool_calls"], reply["changes"] = [], []
        self._busy.add(agent_id)
        if self._config.llm_ready:
            future = self._core.submit(self._runner.chat(agent, reply["id"]))
        else:  # no model configured: placeholder replies so the UI can still be exercised
            future = self._core.submit(dummy_reply(self._store, self._events, agent, text, reply["id"]))
        self._tasks[agent_id] = future

        def finished(_f: concurrent.futures.Future) -> None:
            self._busy.discard(agent_id)
            self._tasks.pop(agent_id, None)

        future.add_done_callback(finished)
        return {"user_message": user_msg, "agent_message": reply}

    @endpoint
    def stop_agent(self, agent_id: str) -> bool:
        future = self._tasks.get(agent_id)
        return bool(future and future.cancel())

    # --- proposed changes -------------------------------------------------------------------------------------

    @endpoint
    def apply_change(self, change_id: str, force: bool = False) -> dict[str, Any]:
        return self._changes.apply(change_id, force=bool(force))

    @endpoint
    def discard_change(self, change_id: str, feedback: str = "") -> dict[str, Any]:
        """Discard a proposed change. With feedback, also tell the agent what to do differently (it replies)."""
        feedback = (feedback or "").strip()
        if len(feedback) > MAX_MESSAGE_CHARS:
            raise UserError(f"Feedback is too long (max {MAX_MESSAGE_CHARS:,} characters).")
        agent_id = self._changes.get(change_id)["agent_id"]
        if feedback and agent_id in self._busy:
            raise UserError("The agent is still responding. Try again when it's done.")
        change = self._changes.discard(change_id)
        if not feedback:
            return {"change": change}
        text = f"I discarded your proposed change to {change['name']}. Here's what should be different:\n\n{feedback}"
        return {"change": change, **self._start_reply(self._store.get_agent(agent_id), text)}

    @endpoint
    def undo_change(self, change_id: str) -> dict[str, Any]:
        return self._changes.undo(change_id)

    @endpoint
    def reveal_change(self, change_id: str) -> None:
        path = self._changes.target_path(change_id)
        if not path.exists():
            raise UserError("That file no longer exists.")
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            open_in_file_manager(str(path.parent))

    # --- sandbox ------------------------------------------------------------------------------------------------

    @endpoint
    def sandbox_done(self, run_id: str, result: dict[str, Any]) -> None:
        """Called by ui/js/sandbox.js when the Pyodide worker finishes a run."""
        self._sandbox.complete(str(run_id), result)

    # --- files --------------------------------------------------------------------------------------------------

    @endpoint
    def pick_folder(self) -> dict[str, str] | None:
        if self._window is None:
            return None
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not result:
            return None
        return {"path": to_display_path(result[0])}

    # --- settings -----------------------------------------------------------------------------------------------

    @endpoint
    def set_theme(self, theme: str) -> str:
        self._settings.set_theme(theme)
        return theme

    @endpoint
    def save_endpoint_overrides(self, overrides: dict[str, str]) -> list[dict[str, Any]]:
        self._settings.set_overrides(overrides or {})
        self._config = self._reload_config()
        self._runner.config = self._config
        return self._config.describe()

    @endpoint
    def open_data_folder(self) -> None:
        open_in_file_manager(str(self._config.data_dir))
