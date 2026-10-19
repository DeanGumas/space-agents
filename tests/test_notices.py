"""Which run reports reach the user (notification + Activity), and how they're worded."""

from space_agents.manifest import Manifest, Notifications
from space_agents.runtime.notices import (
    failure_reason,
    is_notable,
    notable_items,
    notification,
    reports_needing_attention,
)
from space_agents.store import Store, now_iso


def item(level, title, change="new"):
    return {"level": level, "change": change, "title": title, "detail": "", "suggested_actions": [], "sources": []}


def run(status="done", items=(), summary="s"):
    return {"status": status, "report": {"summary": summary, "items": list(items)} if status == "done" else None}


AGENT = Manifest(name="PO Manager", notifications=Notifications(desktop=True, min_level="action"))


def test_only_new_or_changed_findings_at_the_minimum_level_count():
    r = run(items=[item("info", "2 delivered"), item("action", "45821 late", "unchanged"), item("urgent", "Line down")])
    assert [i["title"] for i in notable_items(AGENT, r)] == ["Line down"]  # info is below; unchanged never counts
    assert not is_notable(AGENT, run(items=[item("action", "45821 late", "unchanged")]))
    assert not is_notable(AGENT, run(items=[item("info", "FYI")]))
    everything = Manifest(name="x", notifications=Notifications(min_level="info"))
    assert is_notable(everything, run(items=[item("info", "FYI")]))
    assert is_notable(AGENT, run(status="error"))  # failures always count


def test_notification_wording():
    r = run(items=[item("action", "PO 46010 partial shipment"), item("urgent", "PO 45821 slips to Oct 14", "changed")])
    assert notification(AGENT, r, "", 0) == ("PO Manager: urgent", "PO 45821 slips to Oct 14 (+1 more)")
    one = run(items=[item("action", "PO 46010 partial shipment")])
    assert notification(AGENT, one, "", 2) == (
        "PO Manager: action needed",
        "PO 46010 partial shipment · 2 file changes to approve",
    )
    failed = notification(AGENT, run(status="error"), "Reading…\n\n⚠️ The model server rejected the API key.", 0)
    assert failed == ("PO Manager: the run failed", "The model server rejected the API key.")
    assert failure_reason("partial text\n\n_Stopped._") == "Stopped."


def test_unseen_notable_reports_wait_in_activity(tmp_path):
    store = Store(tmp_path)
    agent = store.save_agent(Manifest(name="PO Manager"))
    quiet = store.add_run(agent.id, "schedule", "t")
    store.update_run(
        quiet["id"], status="done", report=run(items=[item("info", "FYI")])["report"], finished_at=now_iso()
    )
    loud = store.add_run(agent.id, "schedule", "t")
    report = run(items=[item("action", "a"), item("action", "b"), item("urgent", "c"), item("action", "d")])["report"]
    store.update_run(loud["id"], status="done", report=report, finished_at=now_iso())
    [entry] = reports_needing_attention(store)[agent.id]  # the info-only run isn't listed
    assert entry["run_id"] == loud["id"] and entry["message_id"] == loud["message_id"]
    assert [i["title"] for i in entry["items"]] == ["c", "a", "b"] and entry["more"] == 1
    store.mark_runs_seen(agent.id)
    assert reports_needing_attention(store) == {}
    store.close()
