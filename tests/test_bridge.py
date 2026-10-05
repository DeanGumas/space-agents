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
    )
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


def test_to_display_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    assert to_display_path(str(tmp_path / "Documents")) == "~/Documents"
