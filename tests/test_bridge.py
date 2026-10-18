"""Bridge API tests: the same calls the UI makes, without a window."""

import base64
import json
import time

import pytest

from space_agents.bridge import Api, to_display_path
from space_agents.changes import ChangeManager
from space_agents.config import load_config
from space_agents.core import Core, EventBus
from space_agents.documents.library import DocumentLibrary
from space_agents.runtime.loop import AgentRunner
from space_agents.sandbox.pyodide import PyodideSandbox
from space_agents.settings import UserSettings
from space_agents.store import PNG_MAGIC, Store


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, title, body):
        self.sent.append((title, body))
        return True


class FakeWindow:
    """Stands in for the native window: the folder picker and confirmation dialogs the page can't script."""

    def __init__(self):
        self.next_folder: str | None = None
        self.answer = True
        self.asked: list[tuple[str, str]] = []

    def create_file_dialog(self, dialog_type):
        return [self.next_folder] if self.next_folder else None

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return self.answer


PNG_URL = "data:image/png;base64," + base64.b64encode(PNG_MAGIC + b"\x00" * 32).decode()


@pytest.fixture
def env(tmp_path):
    def reload_config():
        return load_config(environ={}, dotenv_path=False, cli={"data_dir": tmp_path})

    config = reload_config()
    store = Store(tmp_path)
    core = Core()
    core.start()
    events = EventBus(core, flush_interval=0.01)
    delivered: list[dict] = []
    events.attach(lambda payload: delivered.extend(json.loads(payload)))
    events.start()
    sandbox = PyodideSandbox(tmp_path / "workspaces")
    docs = DocumentLibrary(store)
    changes = ChangeManager(store, tmp_path, events)
    notifier = FakeNotifier()
    api = Api(
        config=config,
        reload_config=reload_config,
        store=store,
        settings=UserSettings(tmp_path),
        core=core,
        events=events,
        sandbox=sandbox,
        runner=AgentRunner(store=store, events=events, docs=docs, sandbox=sandbox, changes=changes, config=config),
        changes=changes,
        notifier=notifier,
    )
    api._fake_notifier = notifier
    api._fake_window = FakeWindow()
    api._attach_window(api._fake_window)
    yield api, delivered
    core.stop()
    store.close()


def ok(res):
    assert res["ok"], res
    return res["data"]


def test_bootstrap_is_json_serializable(env):
    api, _ = env
    data = ok(api.bootstrap())
    json.dumps(data)
    assert data["blank_manifest"]["name"] == ""
    assert {t["key"] for t in data["templates"]} >= {"purchase-order-manager"}


def test_create_edit_delete_agent(env):
    api, _ = env
    created = ok(api.save_agent({"name": "Helper", "avatar": {"emoji": "📦", "color": "#3b82f6"}}))
    assert created["id"] == "helper"
    assert created["status"] == "idle" and created["last_message"] is None

    created["description"] = "Now with a description"
    updated = ok(api.save_agent({k: v for k, v in created.items() if k not in ("status", "last_message")}))
    assert updated["description"] == "Now with a description"
    assert [a["id"] for a in ok(api.list_agents())] == ["helper"]

    ok(api.delete_agent("helper"))
    assert ok(api.list_agents()) == []


def test_validation_errors_come_back_as_fields(env):
    api, _ = env
    res = api.save_agent({"name": "", "model": {"temperature": 9}})
    assert res["ok"] is False
    assert set(res["fields"]) == {"name", "model.temperature"}


def test_avatar_upload_keep_and_remove(env):
    api, _ = env
    a = ok(api.save_agent({"name": "Pic", "avatar": {"emoji": None, "color": "#3b82f6", "image_data": PNG_URL}}))
    assert a["avatar_url"] == PNG_URL

    kept = ok(api.save_agent({**a, "avatar": {"emoji": None, "color": "#000000", "keep_image": True}}))
    assert kept["avatar_url"] == PNG_URL and kept["avatar"]["color"] == "#000000"

    removed = ok(api.save_agent({**a, "avatar": {"emoji": "🤖", "color": "#000000"}}))
    assert removed["avatar_url"] is None


def test_ui_cannot_set_avatar_path_directly(env, tmp_path):
    api, _ = env
    (tmp_path / "x.png").write_bytes(PNG_MAGIC)
    a = ok(api.save_agent({"name": "Evil", "avatar": {"image": "../x.png", "color": "#000000"}}))
    assert a["avatar"]["image"] is None


def test_updating_missing_agent_fails(env):
    api, _ = env
    assert api.save_agent({"name": "Ghost", "id": "ghost"})["ok"] is False


def test_send_message_streams_dummy_reply(env):
    api, delivered = env
    ok(api.save_agent({"name": "Chatty"}))
    sent = ok(api.send_message("chatty", "hello there"))
    assert sent["agent_message"]["status"] == "streaming"
    assert api.send_message("chatty", "again")["ok"] is False  # still responding

    deadline = time.time() + 15
    while time.time() < deadline and not any(e["type"] == "message.done" for e in delivered):
        time.sleep(0.05)
    done = next(e for e in delivered if e["type"] == "message.done")
    streamed = "".join(e["delta"] for e in delivered if e["type"] == "message.delta")
    assert done["message"]["status"] == "done"
    assert done["message"]["content"] == streamed
    assert "hello there" in streamed
    assert [e["status"] for e in delivered if e["type"] == "agent.status"] == ["responding", "idle"]


def test_send_message_validation(env):
    api, _ = env
    ok(api.save_agent({"name": "Quiet"}))
    assert api.send_message("quiet", "   ")["ok"] is False
    assert api.send_message("nobody", "hi")["ok"] is False


def test_theme_and_overrides(env):
    api, _ = env
    assert ok(api.set_theme("dark")) == "dark"
    assert api.set_theme("neon")["ok"] is False
    described = {c["key"]: c for c in ok(api.save_endpoint_overrides({"llm_base_url": "https://x/api/"}))}
    assert described["llm_base_url"]["value"] == "https://x/api"
    assert described["llm_base_url"]["source"] == "user"


def test_new_server_address_needs_native_confirmation(env):
    api, _ = env
    window = api._fake_window
    window.answer = False
    res = api.save_endpoint_overrides({"llm_base_url": "https://evil.example/api"})
    assert res["ok"] is False and "wasn't changed" in res["error"]
    assert api._config.llm_base_url != "https://evil.example/api"
    assert "https://evil.example/api" in window.asked[-1][1]

    window.answer = True
    ok(api.save_endpoint_overrides({"llm_base_url": "https://new.example/api"}))
    assert api._config.llm_base_url == "https://new.example/api"

    window.asked.clear()
    ok(api.save_endpoint_overrides({"llm_base_url": "https://new.example/api/"}))  # unchanged: no dialog
    ok(api.save_endpoint_overrides({}))  # back to the build default: no dialog
    assert window.asked == []
    assert api.save_endpoint_overrides("nope")["ok"] is False


def test_new_folders_must_come_from_the_native_picker(env, tmp_path):
    api, _ = env
    folder = tmp_path / "Reports"
    folder.mkdir()
    grant = [{"path": str(folder), "access": "read"}]
    res = api.save_agent({"name": "Sneaky", "filesystem": grant})
    assert res["ok"] is False and set(res["fields"]) == {"filesystem.0.path"}

    api._fake_window.next_folder = str(folder)
    assert ok(api.pick_folder())["path"]
    agent = ok(api.save_agent({"name": "Picked", "filesystem": grant}))
    # Folders the agent already has, and folders from built-in templates, need no picking.
    api._picked_folders.clear()
    ok(api.save_agent({**agent, "description": "edited"}))
    ok(api.save_agent({"name": "From template", "filesystem": [{"path": "~/Downloads", "access": "read"}]}))
    assert api._fake_window.asked == []  # read-only access never asks


def test_write_access_needs_native_confirmation(env, tmp_path):
    api, _ = env
    window = api._fake_window
    folder = tmp_path / "Reports"
    folder.mkdir()
    window.next_folder = str(folder)
    ok(api.pick_folder())
    writable = [{"path": str(folder), "access": "read_write"}]

    window.answer = False
    res = api.save_agent({"name": "Writer", "filesystem": writable})
    assert res["ok"] is False and "wasn't allowed" in res["error"]
    assert ok(api.list_agents()) == []
    assert window.asked == [("Allow more access?", f'Allow "Writer" to:\n• save files in {folder}')]

    window.answer = True
    agent = ok(api.save_agent({"name": "Reader", "filesystem": [{"path": str(folder), "access": "read"}]}))
    window.asked.clear()
    agent = ok(api.save_agent({**agent, "filesystem": writable}))  # upgrading read → read & write asks
    assert len(window.asked) == 1
    ok(api.save_agent({**agent, "description": "edited"}))  # nothing new: no dialog
    assert len(window.asked) == 1

    window.answer = False
    res = api.save_agent({**agent, "file_changes": {"new_files": "allow", "existing_files": "allow_with_backup"}})
    assert res["ok"] is False and "without asking" in window.asked[-1][1]


def test_without_a_window_nothing_is_confirmed(env):
    api, _ = env
    api._window = None
    assert api._confirm("Allow?", "Allow it?") is False


def test_to_display_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    assert to_display_path(str(tmp_path / "Documents")) == "~/Documents"


def _pending_change(api, tmp_path, agent_id):
    from space_agents.manifest import FileChanges

    target = tmp_path / "tracker.csv"
    target.write_text("v1")
    staged = tmp_path / "staged.csv"
    staged.write_text("v2")
    return api._changes.propose(
        agent_id=agent_id,
        message_id=None,
        tool_call_id=None,
        target=target,
        source=staged,
        summary={"description": "update"},
        policy=FileChanges(),
    )


def test_discard_without_feedback_just_discards(env, tmp_path):
    api, _ = env
    ok(api.save_agent({"name": "Editor"}))
    change = _pending_change(api, tmp_path, "editor")
    res = ok(api.discard_change(change["id"]))
    assert res["change"]["status"] == "discarded" and "user_message" not in res
    assert (tmp_path / "tracker.csv").read_text() == "v1"


def test_discard_with_feedback_tells_the_agent(env, tmp_path):
    api, delivered = env
    ok(api.save_agent({"name": "Editor"}))
    change = _pending_change(api, tmp_path, "editor")
    res = ok(api.discard_change(change["id"], "  Use 'Late' instead of 'Delayed'.  "))
    assert res["change"]["status"] == "discarded"
    assert res["user_message"]["content"].endswith("Use 'Late' instead of 'Delayed'.")
    assert "tracker.csv" in res["user_message"]["content"]
    assert res["agent_message"]["status"] == "streaming"
    # While the agent replies, more feedback is refused (and nothing is discarded by the failed call).
    another = _pending_change(api, tmp_path, "editor")
    assert api.discard_change(another["id"], "more")["ok"] is False
    assert api._changes.get(another["id"])["status"] == "pending"


def test_clone_names():
    from space_agents.bridge import clone_name

    assert clone_name("PO Agent", {"PO Agent"}) == "PO Agent (Clone)"
    assert clone_name("PO Agent", {"PO Agent", "PO Agent (Clone)"}) == "PO Agent (Clone 2)"
    assert clone_name("PO Agent (Clone)", {"PO Agent", "PO Agent (Clone)"}) == "PO Agent (Clone 2)"  # no stacking
    long = clone_name("x" * 60, set())
    assert len(long) <= 60 and long.endswith(" (Clone)")


def test_duplicate_agent_copies_setup_not_history(env):
    api, _ = env
    api._fake_window.answer = True  # granting ~/Documents read & write asks in a native dialog
    src = ok(
        api.save_agent(
            {
                "name": "PO Agent",
                "instructions": "Track POs.",
                "model": {"name": "gpt-5-nano", "temperature": 0.3},
                "avatar": {"emoji": None, "color": "#3b82f6", "image_data": PNG_URL},
                "mcp_servers": [{"id": "costpoint", "tools": ["get_po"]}],
                "filesystem": [{"path": "~/Documents", "access": "read_write"}],
                "file_changes": {"new_files": "ask", "existing_files": "allow_with_backup"},
            }
        )
    )
    api._store.add_message(src["id"], "user", "hello")
    clone = ok(api.duplicate_agent(src["id"]))
    assert clone["name"] == "PO Agent (Clone)" and clone["id"] != src["id"]
    for key in ("instructions", "model", "mcp_servers", "filesystem", "file_changes", "schedule", "limits"):
        assert clone[key] == src[key], key
    assert clone["avatar_url"] == src["avatar_url"] and clone["avatar"]["image"] == f"avatars/{clone['id']}.png"
    assert clone["last_message"] is None and ok(api.list_messages(clone["id"])) == []
    assert ok(api.duplicate_agent(src["id"]))["name"] == "PO Agent (Clone 2)"
    assert api.duplicate_agent("nope")["ok"] is False


def test_ui_prefs_are_validated_and_persisted(env, tmp_path):
    api, _ = env
    assert ok(api.set_ui_pref("sidebar_width", 330)) == 330
    assert ok(api.set_ui_pref("sidebar_width", 9999)) == 480  # clamped
    assert api.set_ui_pref("evil", 1)["ok"] is False
    assert api.set_ui_pref("sidebar_width", "wide")["ok"] is False
    assert UserSettings(tmp_path).ui == {"sidebar_width": 480}
    assert ok(api.bootstrap())["ui"] == {"sidebar_width": 480}


def test_approval_summary_sentence():
    from space_agents.bridge import approval_summary

    one = [{"name": "PO Tracker.xlsx", "kind": "modify", "summary": {"description": "Mark 3 Acme POs late."}}]
    assert approval_summary(one) == "Update PO Tracker.xlsx: mark 3 Acme POs late."
    named = [{"name": "PO Tracker.xlsx", "kind": "modify", "summary": {"description": "Update PO Tracker.xlsx dates"}}]
    assert approval_summary(named) == "Update PO Tracker.xlsx dates."
    many = one + [{"name": "b.csv", "kind": "create", "summary": {}}] * 2
    assert approval_summary(many).endswith("(+2 more files).")
    assert approval_summary([{"name": "r.csv", "kind": "create", "summary": {}}]) == "Create r.csv."


def _reply_with_pending_change(api, tmp_path, agent_id):
    """Simulate a finished agent reply that proposed a change to an existing file."""
    from space_agents.manifest import FileChanges

    reply = api._store.add_message(agent_id, "agent", "Proposed.")
    target = tmp_path / "t.csv"
    target.write_text("v1")
    staged = tmp_path / "s.csv"
    staged.write_text("v2")
    api._changes.propose(
        agent_id=agent_id,
        message_id=reply["id"],
        tool_call_id=None,
        target=target,
        source=staged,
        summary={"description": "Mark PO 45821 late"},
        policy=FileChanges(),
    )
    return reply


def test_notifies_when_a_reply_needs_approval(env, tmp_path):
    api, _ = env
    agent = api._store.get_agent(ok(api.save_agent({"name": "PO Agent"}))["id"])
    reply = _reply_with_pending_change(api, tmp_path, agent.id)

    ok(api.set_ui_state(True, agent.id))  # looking right at it: no pop-up
    api._notify_if_awaiting_approval(agent, reply["id"])
    assert api._fake_notifier.sent == []

    ok(api.set_ui_state(False, agent.id))  # window in the background
    api._notify_if_awaiting_approval(agent, reply["id"])
    assert api._fake_notifier.sent == [("PO Agent requests approval", "Update t.csv: mark PO 45821 late.")]

    agent.notifications.desktop = False  # agent's notifications turned off
    api._notify_if_awaiting_approval(agent, reply["id"])
    assert len(api._fake_notifier.sent) == 1
    assert ok(api.bootstrap())["attention"][0]["agent_id"] == agent.id


def test_no_notification_without_pending_changes(env):
    api, _ = env
    agent = api._store.get_agent(ok(api.save_agent({"name": "Quiet"}))["id"])
    reply = api._store.add_message(agent.id, "agent", "Just an answer.")
    ok(api.set_ui_state(False, None))
    api._notify_if_awaiting_approval(agent, reply["id"])
    assert api._fake_notifier.sent == []


def test_discard_feedback_keeps_agent_in_activity_until_reply_finishes(env, tmp_path):
    api, delivered = env
    ok(api.save_agent({"name": "Editor"}))
    change = _pending_change(api, tmp_path, "editor")
    ok(api.discard_change(change["id"], "Use Late instead."))
    assert [e["revising"] for e in api._changes.attention()] == [True]
    deadline = time.time() + 15
    while time.time() < deadline and "editor" in api._busy:
        time.sleep(0.05)
    time.sleep(0.1)
    assert api._changes.attention() == []
    assert any(e["type"] == "attention.updated" for e in delivered)


def test_sensitive_folders_cannot_be_read_and_write(env, tmp_path, monkeypatch):
    api, _ = env
    home = tmp_path.resolve() / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    api._fake_window.next_folder = str(home / "Library" / "LaunchAgents")
    path = ok(api.pick_folder())["path"]
    res = api.save_agent({"name": "Sneaky", "filesystem": [{"path": path, "access": "read_write"}]})
    assert res["ok"] is False and "can't save files here" in res["fields"]["filesystem.0.path"]
    assert api._fake_window.asked == []  # refused before asking
    ok(api.save_agent({"name": "Reader", "filesystem": [{"path": path, "access": "read"}]}))
