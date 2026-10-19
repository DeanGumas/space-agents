"""Agent memory: keyed notes, limits, search, and what goes into the prompt."""

import pytest

from space_agents import memory as mem
from space_agents.memory import AgentMemory, normalize_key, prompt_notes
from space_agents.store import Store


@pytest.fixture
def notes(tmp_path):
    store = Store(tmp_path)
    yield AgentMemory(store, "po-bot"), store
    store.close()


def test_remember_replaces_by_key_and_forget(notes):
    m, _ = notes
    assert m.remember("PO 45821 Delay", "Slipped to Oct 14.") == "saved"
    assert m.remember("po-45821-delay", "Slipped to Oct 14; Acme confirmed.") == "updated"  # same key
    assert [(n["key"], n["note"]) for n in m.notes()] == [("po-45821-delay", "Slipped to Oct 14; Acme confirmed.")]
    assert m.forget("PO 45821 delay") is True and m.forget("nope") is False and m.count() == 0
    assert normalize_key("  Weird / Key!! ") == "weird-key"


def test_limits_and_validation(notes, monkeypatch):
    m, _ = notes
    for bad_key, bad_note, why in (("", "x", "needs a key"), ("k", "  ", "needs a note"), ("k", "x" * 501, "limited")):
        with pytest.raises(ValueError, match=why):
            m.remember(bad_key, bad_note)
    monkeypatch.setattr(mem, "MAX_NOTES", 2)
    m.remember("a", "1")
    m.remember("b", "2")
    with pytest.raises(ValueError, match="full"):
        m.remember("c", "3")
    assert m.remember("a", "1 again") == "updated"  # replacing still works when full


def test_recall_ranks_by_matching_words(notes):
    m, _ = notes
    m.remember("acme-45821", "Acme PO 45821 slipped to Oct 14.")
    m.remember("acme-contact", "Acme contact is J. Rivera.")
    m.remember("beta-supplier", "Beta Corp is on time.")
    assert [n["key"] for n in m.recall("Acme 45821")] == ["acme-45821", "acme-contact"]
    assert m.recall("nothing like it") == []
    assert len(m.recall("")) == 3  # empty query: the newest notes


def test_prompt_shows_newest_notes_within_budget(notes, monkeypatch):
    m, _ = notes
    for i in range(5):
        m.remember(f"note-{i}", "x" * 100)
    monkeypatch.setattr(mem, "PROMPT_CHARS", 300)
    shown, total = prompt_notes(m)
    assert total == 5 and 1 <= len(shown) < 5
    assert shown[0]["key"] == "note-4"  # newest first


def test_notes_are_per_agent_and_go_with_the_agent(notes):
    m, store = notes
    from space_agents.manifest import Manifest

    agent = store.save_agent(Manifest(name="PO Bot"))
    m.remember("k", "mine")
    assert AgentMemory(store, "someone-else").count() == 0
    store.delete_agent(agent.id)
    assert m.count() == 0
