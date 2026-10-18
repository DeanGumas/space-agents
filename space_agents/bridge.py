"""The JS ↔ Python bridge: every public method here is callable from the UI as `pywebview.api.<name>(...)`.

Conventions
- pywebview runs each call on its own worker thread. Long-running work is handed to the core loop and reports
  progress through EventBus events instead of blocking the call.
- Every method returns an envelope: {"ok": true, "data": ...} or {"ok": false, "error": "...", "fields": {...}}.
  `fields` maps dotted manifest paths to messages so forms can highlight the right input. ui/js/api.js unwraps it.
- Only public methods are exposed. Keep all state in underscore-prefixed attributes: pywebview also walks public
  attributes and would expose their methods too.
- Don't trust the page with permissions. It renders untrusted model output and hosts the sandbox's Web Worker, so
  assume it can be taken over. Anything that widens what an agent can reach or where the API key goes needs the user
  to act in native UI the page can't script: a new folder must come from the native folder picker, and write access
  or a new server address needs OK in a native confirmation dialog (`_confirm`).
"""

from __future__ import annotations

import functools
import logging
import os
import re
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
from space_agents.platform.notify import Notifier
from space_agents.runtime.dispatch import Dispatcher
from space_agents.runtime.loop import AgentRunner
from space_agents.runtime.scheduler import Scheduler
from space_agents.sandbox.pyodide import PyodideSandbox, warm_packages
from space_agents.settings import UserSettings, clean_overrides
from space_agents.skills import load_skills
from space_agents.store import NotFound, Store
from space_agents.tools.access import write_protected

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 20_000
SANDBOX_WARMUP_DELAY = 3.0  # seconds after the UI is ready


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


def approval_summary(changes: list[dict[str, Any]]) -> str:
    """One sentence for the desktop notification, e.g. 'Update PO Tracker.xlsx: mark 3 Acme POs late (+1 more file).'"""
    first = changes[0]
    verb = "Update" if first["kind"] == "modify" else "Create"
    description = str((first.get("summary") or {}).get("description") or "").strip().rstrip(".")
    sentence = description if first["name"].lower() in description.lower() else f"{verb} {first['name']}"
    if description and sentence is not description:
        sentence += f": {description[0].lower()}{description[1:]}"
    if len(changes) > 1:
        sentence += f" (+{len(changes) - 1} more file{'s' if len(changes) > 2 else ''})"
    return sentence + "."


def clone_name(name: str, existing: set[str], limit: int = 60) -> str:
    """'PO Agent' → 'PO Agent (Clone)', then '(Clone 2)'…; cloning a clone doesn't stack suffixes."""
    base = re.sub(r" \(Clone(?: \d+)?\)$", "", name).strip() or "Agent"
    n = 1
    while True:
        suffix = " (Clone)" if n == 1 else f" (Clone {n})"
        candidate = base[: limit - len(suffix)].rstrip() + suffix
        if candidate not in existing:
            return candidate
        n += 1


def to_display_path(path: str) -> str:
    """Store folders under the home directory as ~/... so manifests stay portable between users."""
    p = Path(path).expanduser().resolve()
    try:
        return "~/" + p.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(p)


def folder_key(path: str) -> Path:
    """A folder as the access policy sees it (expanded and resolved), for comparing grants."""
    return Path(os.path.expanduser(path)).resolve()


def new_permissions(old: Manifest | None, new: Manifest) -> list[str]:
    """Write access `new` gives that `old` didn't, in plain words. Each needs the user's OK in a native dialog."""
    before = {folder_key(g.path): g.access for g in old.filesystem} if old else {}
    added = [
        f"save files in {g.path}"
        for g in new.filesystem
        if g.access == "read_write" and before.get(folder_key(g.path)) != "read_write"
    ]

    def unasked_writes(m: Manifest | None) -> bool:
        writable = m is not None and any(g.access == "read_write" for g in m.filesystem)
        return writable and m.file_changes.existing_files == "allow_with_backup"

    if unasked_writes(new) and not unasked_writes(old):
        added.append("change existing files without asking you first (a backup is kept)")
    return added


ENDPOINT_LABELS = {"llm_base_url": "AI model server", "mcp_base_url": "Data sources server"}


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
        notifier: Notifier | None = None,
        on_ui_ready: Callable[[dict[str, Any]], None] | None = None,
        confirm: Callable[[str, str], bool] | None = None,
        dispatcher: Dispatcher | None = None,
        scheduler: Scheduler | None = None,
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
        self._notifier = notifier or Notifier()
        self._on_ui_ready = on_ui_ready
        self._confirm_dialog = confirm  # tests inject one; otherwise the window's native dialog
        self._picked_folders: set[Path] = set()  # chosen in the native folder picker this session
        self._template_folders = {
            folder_key(g["path"]) for t in load_templates(paths.templates_dir()) for g in t["manifest"]["filesystem"]
        }
        self._ui_focused = True  # reported by the UI (window focus/visibility)
        self._ui_viewing: str | None = None  # agent whose channel is open
        self._window: webview.Window | None = None
        # One work queue per agent (chat replies and runs); the bridge only starts and stops work.
        self._dispatch = dispatcher or Dispatcher(store=store, core=core, events=events, runner=runner, changes=changes)
        self._dispatch.on_finished = self._after_work
        self._scheduler = scheduler  # only for showing each agent's next run; it runs on the core loop

    def _attach_window(self, window: webview.Window) -> None:
        self._window = window

    def _confirm(self, title: str, message: str) -> bool:
        """Ask in a native dialog, which the page can't script or click. No window (headless) means no."""
        if self._confirm_dialog is not None:
            return bool(self._confirm_dialog(title, message))
        window = self._window
        return bool(window and window.create_confirmation_dialog(title, message))

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
            packages = warm_packages(self._config.get("sandbox_warmup"))
            if packages is not None:
                # A few seconds after launch, so the window is responsive first.
                self._core.loop.call_soon_threadsafe(
                    lambda: self._core.loop.call_later(SANDBOX_WARMUP_DELAY, self._sandbox.warm, packages)
                )
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
            "ui": self._settings.ui,
            "config": self._config.describe(),
            "data_dir": str(self._config.data_dir),
            "agents": self._agent_views(),
            "attention": self._changes.attention(),
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
        view["status"] = self._dispatch.status(m.id)
        view["next_run_at"] = self._scheduler.next_run(m) if self._scheduler else None
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
        existing = self._store.get_agent(manifest.id) if manifest.id else None
        if existing and keep_image:
            manifest.avatar.image = existing.avatar.image
        self._check_folders(existing, manifest)
        added = new_permissions(existing, manifest)
        if added:
            message = f'Allow "{manifest.name}" to:\n' + "\n".join(f"• {a}" for a in added)
            if not self._confirm("Allow more access?", message):
                raise UserError("Not saved: the new access wasn't allowed.")
        png = Store.decode_png_data_url(image_data) if image_data else None
        saved = self._store.save_agent(manifest, png)
        return self._agent_view(saved, self._store.last_messages().get(saved.id))

    def _check_folders(self, old: Manifest | None, new: Manifest) -> None:
        """A folder the agent didn't have before must come from the native folder picker (or a built-in template).
        Otherwise a compromised page could grant an agent any folder just by sending its path.

        Read & write is refused on system, app-settings, and hidden folders: files there can run code by themselves.
        (FolderAccess blocks writes there anyway; this tells the user up front.)

        The skills folder follows the same picker rule: skills are instructions the agent trusts.
        """
        known = (
            self._picked_folders
            | self._template_folders
            | {folder_key(g.path) for g in (old.filesystem if old else [])}
        )
        errors = {}
        for i, g in enumerate(new.filesystem):
            if folder_key(g.path) not in known:
                errors[f"filesystem.{i}.path"] = "Add this folder with the Add folder… button."
            elif g.access == "read_write" and write_protected(folder_key(g.path)):
                errors[f"filesystem.{i}.path"] = (
                    "This is a system, app-settings, or hidden folder, so agents can't save files here. "
                    "Choose Read only, or pick a regular folder."
                )
        known_skills = self._picked_folders | ({folder_key(old.skills.folder)} if old and old.skills.folder else set())
        if new.skills.folder and folder_key(new.skills.folder) not in known_skills:
            errors["skills.folder"] = "Choose this folder with the Choose folder… button."
        if errors:
            raise ManifestError(errors)

    @endpoint
    def list_skills(self, folder: str) -> dict[str, Any]:
        """Preview the skills in a folder for the editor: names, descriptions, and anything wrong with them.

        Only for folders the user picked or an agent already uses, so the page can't probe the disk.
        """
        if not isinstance(folder, str) or not folder.strip():
            return {"skills": [], "problems": []}
        known = self._picked_folders | {
            folder_key(m.skills.folder) for m in self._store.list_agents() if m.skills.folder
        }
        if folder_key(folder) not in known:
            raise UserError("Choose the skills folder with the Choose folder… button.")
        found = load_skills(folder)
        return {
            "skills": [{"name": s.name, "description": s.description} for s in found.skills],
            "problems": found.problems,
        }

    @endpoint
    def duplicate_agent(self, agent_id: str) -> dict[str, Any]:
        """Copy an agent's whole setup (not its chat history) as "<name> (Clone)", "(Clone 2)", …"""
        source = self._store.get_agent(agent_id)
        clone = Manifest.from_dict(source.to_dict())
        clone.id = ""
        clone.name = clone_name(source.name, {m.name for m in self._store.list_agents()})
        clone.avatar.image = None
        saved = self._store.save_agent(clone, self._store.avatar_bytes(source))
        return self._agent_view(saved, None)

    @endpoint
    def delete_agent(self, agent_id: str) -> None:
        if self._dispatch.busy(agent_id):
            raise UserError("This agent is busy. Try again in a moment.")
        self._store.delete_agent(agent_id)
        self._changes.emit_attention()  # its pending proposals are gone

    # --- chat ---------------------------------------------------------------------------------------------------

    @endpoint
    def list_messages(self, agent_id: str) -> list[dict[str, Any]]:
        messages = self._store.list_messages(agent_id)
        ids = [m["id"] for m in messages if m["role"] == "agent"]
        calls, changes = self._runner.tool_calls_for(ids), self._changes.for_messages(ids)
        for m in messages:
            m["tool_calls"] = calls.get(m["id"], [])
            m["changes"] = changes.get(m["id"], [])
        return self._store.attach_runs(messages)

    @endpoint
    def send_message(self, agent_id: str, text: str) -> dict[str, Any]:
        """Post the user's message. If the agent is busy, its reply waits in the agent's queue."""
        text = (text or "").strip()
        if not text:
            raise UserError("Message is empty.")
        if len(text) > MAX_MESSAGE_CHARS:
            raise UserError(f"Message is too long (max {MAX_MESSAGE_CHARS:,} characters).")
        return self._dispatch.chat(self._store.get_agent(agent_id), text)

    @endpoint
    def run_agent(self, agent_id: str) -> dict[str, Any]:
        """Run now: the agent does its task on its own and posts a report in its channel (queued if it's busy)."""
        agent = self._store.get_agent(agent_id)
        if not agent.task.strip():
            raise UserError("This agent has no task yet. Add one under Edit → Schedule.")
        if not self._runner.config.llm_ready:
            raise UserError("Runs need a model. Set the model server and API key first (see Settings).")
        return {"message": self._dispatch.run(agent, "manual")}

    def _after_work(self, agent_id: str, message_id: int) -> None:
        try:
            agent = self._store.get_agent(agent_id)
        except NotFound:
            return
        self._notify_if_awaiting_approval(agent, message_id)

    def _notify_if_awaiting_approval(self, agent: Manifest, message_id: int) -> None:
        """After a reply: if it left changes for the user to approve, pop up a desktop notification.

        Skipped when the user is already looking at that agent's conversation (the card is right in front of them)
        or when the agent's desktop notifications are turned off.
        """
        pending = [
            c
            for c in self._changes.for_messages([message_id]).get(message_id, [])
            if c["status"] in ("pending", "conflicted")
        ]
        if not pending or not agent.notifications.desktop:
            return
        if self._ui_focused and self._ui_viewing == agent.id:
            return
        title, body = f"{agent.name} requests approval", approval_summary(pending)
        sent = self._notifier.notify(title, body)
        log.info("Desktop notification %s: %s | %s", "sent" if sent else "unavailable", title, body)

    @endpoint
    def set_ui_state(self, focused: bool, viewing: str | None = None) -> None:
        """The UI reports whether the window is in front and which agent's conversation is open."""
        self._ui_focused = bool(focused)
        self._ui_viewing = viewing if isinstance(viewing, str) else None

    @endpoint
    def attention(self) -> list[dict[str, Any]]:
        return self._changes.attention()

    @endpoint
    def stop_agent(self, agent_id: str) -> bool:
        """Stop what the agent is doing, and anything it's waiting to do."""
        return self._dispatch.stop(agent_id)

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
        change = self._changes.discard(change_id)
        if not feedback:
            return {"change": change}
        text = f"I discarded your proposed change to {change['name']}. Here's what should be different:\n\n{feedback}"
        return {"change": change, **self._dispatch.chat(self._store.get_agent(agent_id), text, revising=True)}

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
    def sandbox_warmed(self, info: dict[str, Any] | None = None) -> None:
        """Called by ui/js/sandbox.js when the background warm-up finishes."""
        info = info if isinstance(info, dict) else {}
        if info.get("error"):
            log.warning("Sandbox warm-up failed: %s", info["error"])
        else:
            log.info("Sandbox warm in %.1f s", (info.get("ms") or 0) / 1000)

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
        self._picked_folders.add(folder_key(result[0]))
        return {"path": to_display_path(result[0])}

    # --- settings -----------------------------------------------------------------------------------------------

    @endpoint
    def set_ui_pref(self, key: str, value: Any) -> Any:
        """Persist a small UI preference (e.g. sidebar width). Unknown keys and bad values are rejected/clamped."""
        return self._settings.set_ui(str(key), value)

    @endpoint
    def set_theme(self, theme: str) -> str:
        self._settings.set_theme(theme)
        return theme

    @endpoint
    def save_endpoint_overrides(self, overrides: dict[str, str]) -> list[dict[str, Any]]:
        """Change the server URLs. A new address needs OK in a native dialog, because the API key will go there."""
        if not isinstance(overrides, dict):
            raise UserError("Invalid settings.")
        cleaned = clean_overrides(overrides)
        moved = {k: v for k, v in cleaned.items() if v != self._config.get(k)}
        if moved:
            lines = "\n".join(f"• {ENDPOINT_LABELS[k]}: {v}" for k, v in moved.items())
            message = (
                f"Send your API key and agent data to a new address?\n{lines}\n\n"
                "Only continue if your IT team gave you this address."
            )
            if not self._confirm("Change server address?", message):
                raise UserError("Not saved: the server address wasn't changed.")
        self._settings.set_overrides(cleaned)
        self._config = self._reload_config()
        self._runner.config = self._config
        return self._config.describe()

    @endpoint
    def open_data_folder(self) -> None:
        open_in_file_manager(str(self._config.data_dir))
