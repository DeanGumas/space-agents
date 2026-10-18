import json
from pathlib import Path

import pytest

from space_agents.manifest import Manifest, ManifestError, slugify

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "agents"


def errors_for(data):
    with pytest.raises(ManifestError) as exc:
        Manifest.from_dict(data)
    return exc.value.errors


def test_minimal_manifest_gets_defaults():
    m = Manifest.from_dict({"name": "  Helper  "})
    assert m.name == "Helper"
    assert m.avatar.emoji == "🤖"
    assert m.schedule.enabled is False
    assert m.schedule.weekdays == ["mon", "tue", "wed", "thu", "fri"]
    assert m.permissions.external_actions == "never"


def test_round_trip():
    m = Manifest.from_dict(json.loads((TEMPLATES / "purchase-order-manager.json").read_text()))
    assert Manifest.from_dict(m.to_dict()) == m


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.json")), ids=lambda p: p.stem)
def test_templates_are_valid(path):
    Manifest.from_dict(json.loads(path.read_text()))


def test_errors_are_collected_with_dotted_paths():
    errs = errors_for(
        {
            "name": "",
            "avatar": {"color": "blue"},
            "model": {"temperature": 5},
            "filesystem": [{"path": "~/a", "access": "everything"}, {"path": "~/a"}],
            "schedule": {"enabled": True, "every_minutes": 1, "active_hours": {"start": "18:00", "end": "07:00"}},
        }
    )
    assert errs["name"] == "This field is required."
    assert "avatar.color" in errs
    assert "model.temperature" in errs
    assert "filesystem.0.access" in errs
    assert errs["filesystem.1.path"] == "This folder is listed twice."
    assert "schedule.every_minutes" in errs
    assert errs["schedule.active_hours.end"] == "End must be after start."
    assert "task" in errs  # scheduled agents need a task


def test_schedule_weekdays_normalized_and_validated():
    m = Manifest.from_dict({"name": "x", "schedule": {"weekdays": ["fri", "mon", "mon"]}})
    assert m.schedule.weekdays == ["mon", "fri"]
    assert "schedule.weekdays" in errors_for({"name": "x", "schedule": {"weekdays": ["funday"]}})
    assert "schedule.weekdays" in errors_for({"name": "x", "task": "t", "schedule": {"enabled": True, "weekdays": []}})


def test_server_grants():
    m = Manifest.from_dict({"name": "x", "mcp_servers": [{"id": "jira"}, {"id": "outlook", "tools": ["a", "b"]}]})
    assert m.mcp_servers[0].tools == "read_only"
    assert m.mcp_servers[1].tools == ["a", "b"]
    assert "mcp_servers.1.id" in errors_for({"name": "x", "mcp_servers": [{"id": "jira"}, {"id": "jira"}]})
    assert "mcp_servers.0.tools" in errors_for({"name": "x", "mcp_servers": [{"id": "jira", "tools": 3}]})


def test_wrong_types_do_not_crash():
    errs = errors_for({"name": 5, "filesystem": "nope", "schedule": "daily", "limits": {"max_minutes": True}})
    assert set(errs) >= {"name", "filesystem", "schedule", "limits.max_minutes"}
    assert errors_for([1, 2]) == {"name": "This field is required.", "": "Manifest must be a JSON object."}


def test_rejects_unknown_schema_version_and_bad_id():
    assert "schema_version" in errors_for({"name": "x", "schema_version": 99})
    assert "id" in errors_for({"name": "x", "id": "Not A Slug"})


def test_slugify():
    assert slugify("Purchase Order Manager!") == "purchase-order-manager"
    assert slugify("📦") == "agent"
    assert len(slugify("x" * 100)) <= 40


def test_compaction_defaults_and_validation():
    c = Manifest.from_dict({"name": "Helper"}).compaction
    assert (c.enabled, c.trigger_tokens, c.summary_tokens) == (True, 100_000, 2_000)
    assert set(errors_for({"name": "x", "compaction": {"trigger_tokens": 100}})) == {"compaction.trigger_tokens"}
    errors = errors_for({"name": "x", "compaction": {"trigger_tokens": 20_000, "summary_tokens": 10_000}})
    assert "quarter" in errors["compaction.summary_tokens"]
