import pytest

from space_agents.changes import ChangeError, ChangeManager
from space_agents.manifest import FileChanges
from space_agents.store import Store


class Events:
    def __init__(self):
        self.items = []

    def emit(self, type_, **data):
        self.items.append({"type": type_, **data})


@pytest.fixture
def mgr(tmp_path):
    store = Store(tmp_path / "data")
    events = Events()
    yield ChangeManager(store, tmp_path / "data", events), events, tmp_path
    store.close()


def staged(tmp_path, name, content):
    src = tmp_path / "sandbox-out" / name
    src.parent.mkdir(exist_ok=True)
    src.write_text(content)
    return src


def propose(mgr, tmp_path, target, content, policy=None):
    return mgr.propose(
        agent_id="a",
        message_id=1,
        tool_call_id=None,
        target=target,
        source=staged(tmp_path, target.name, content),
        summary={"description": "test"},
        policy=policy or FileChanges(),
    )


def test_new_file_saved_automatically_by_default(mgr):
    m, events, tmp = mgr
    target = tmp / "work" / "new.txt"
    target.parent.mkdir()
    change = propose(m, tmp, target, "hello")
    assert change["status"] == "applied" and change["kind"] == "create" and not change["can_undo"]
    assert target.read_text() == "hello"
    assert events.items[-1]["type"] == "change.updated"


def test_existing_file_waits_then_apply_and_undo(mgr):
    m, _, tmp = mgr
    target = tmp / "tracker.csv"
    target.write_text("v1")
    change = propose(m, tmp, target, "v2")
    assert change["status"] == "pending" and target.read_text() == "v1"
    applied = m.apply(change["id"])
    assert applied["status"] == "applied" and applied["can_undo"] and target.read_text() == "v2"
    undone = m.undo(change["id"])
    assert undone["status"] == "undone" and target.read_text() == "v1"
    with pytest.raises(ChangeError):
        m.apply(change["id"])


def test_conflict_when_file_changed_after_proposal(mgr):
    m, _, tmp = mgr
    target = tmp / "t.txt"
    target.write_text("original")
    change = propose(m, tmp, target, "agent version")
    target.write_text("user edited meanwhile")
    assert m.apply(change["id"])["status"] == "conflicted"
    assert target.read_text() == "user edited meanwhile"
    assert m.apply(change["id"], force=True)["status"] == "applied"
    assert target.read_text() == "agent version"
    assert m.undo(change["id"])["status"] == "undone"
    assert target.read_text() == "user edited meanwhile"


def test_discard_and_policies(mgr):
    m, _, tmp = mgr
    target = tmp / "t.txt"
    target.write_text("keep")
    change = propose(m, tmp, target, "nope")
    assert m.discard(change["id"])["status"] == "discarded" and target.read_text() == "keep"
    auto = propose(m, tmp, target, "auto", FileChanges(new_files="ask", existing_files="allow_with_backup"))
    assert auto["status"] == "applied" and auto["can_undo"]
    ask_new = propose(m, tmp, tmp / "brand-new.txt", "x", FileChanges(new_files="ask"))
    assert ask_new["status"] == "pending" and not (tmp / "brand-new.txt").exists()


def test_for_messages_and_prune(mgr):
    m, _, tmp = mgr
    target = tmp / "t.txt"
    target.write_text("1")
    c = propose(m, tmp, target, "2")
    m.apply(c["id"])
    assert [x["id"] for x in m.for_messages([1])[1]] == [c["id"]]
    assert m.prune_history(days=0) >= 0  # nothing older than "now" yet, and it must not raise
