import base64

import pytest

from space_agents.manifest import Manifest
from space_agents.store import PNG_MAGIC, NotFound, Store

PNG = PNG_MAGIC + b"\x00" * 64


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    yield s
    s.close()


def test_create_assigns_unique_ids(store):
    a = store.save_agent(Manifest(name="PO Helper"))
    b = store.save_agent(Manifest(name="PO Helper"))
    assert (a.id, b.id) == ("po-helper", "po-helper-2")
    assert [m.id for m in store.list_agents()] == ["po-helper", "po-helper-2"]


def test_update_and_delete(store):
    m = store.save_agent(Manifest(name="Zed"))
    m.description = "changed"
    store.save_agent(m)
    assert store.get_agent("zed").description == "changed"
    store.add_message("zed", "user", "hi")
    store.delete_agent("zed")
    with pytest.raises(NotFound):
        store.get_agent("zed")
    assert store.list_messages("zed") == []


def test_invalid_agent_files_are_skipped(store):
    (store.agents_dir / "broken.json").write_text("{not json")
    (store.agents_dir / "invalid.json").write_text('{"name": ""}')
    store.save_agent(Manifest(name="Good"))
    assert [m.name for m in store.list_agents()] == ["Good"]


def test_avatar_save_keep_and_remove(store):
    m = store.save_agent(Manifest(name="Pic"), avatar_png=PNG)
    assert m.avatar.image == "avatars/pic.png"
    url = store.avatar_data_url(m)
    assert url == "data:image/png;base64," + base64.b64encode(PNG).decode()

    m.avatar.image = None  # user removed the picture
    store.save_agent(m)
    assert not (store.avatars_dir / "pic.png").exists()
    assert store.avatar_data_url(m) is None


def test_avatar_rejects_non_png_and_oversize(store):
    with pytest.raises(ValueError):
        store.save_agent(Manifest(name="x"), avatar_png=b"GIF89a")
    with pytest.raises(ValueError):
        store.save_agent(Manifest(name="y"), avatar_png=PNG_MAGIC + b"\x00" * (600 * 1024))


def test_avatar_path_cannot_escape_avatars_dir(store, tmp_path):
    (tmp_path / "secret.png").write_bytes(PNG)
    m = Manifest(name="sneaky")
    m.avatar.image = "../secret.png"
    assert store.avatar_data_url(m) is None


def test_decode_png_data_url():
    assert Store.decode_png_data_url("data:image/png;base64," + base64.b64encode(PNG).decode()) == PNG
    for bad in ("data:image/jpeg;base64,AAAA", "data:image/png;base64,***", 42):
        with pytest.raises(ValueError):
            Store.decode_png_data_url(bad)


def test_messages(store):
    store.add_message("a", "user", "one")
    reply = store.add_message("a", "agent", "", status="streaming")
    store.add_message("b", "user", "other agent")
    store.update_message(reply["id"], content="two", status="done")
    assert [m["content"] for m in store.list_messages("a")] == ["one", "two"]
    assert store.last_messages()["a"]["content"] == "two"
    assert store.list_messages("a", limit=1)[0]["content"] == "two"


def test_recover_interrupted(store):
    msg = store.add_message("a", "agent", "partial", status="streaming")
    store.recover_interrupted()
    assert store.get_message(msg["id"])["status"] == "error"


def test_migrations_are_idempotent(tmp_path):
    Store(tmp_path).close()
    s = Store(tmp_path)
    assert s._db.execute("PRAGMA user_version").fetchone()[0] == 1
    s.close()
