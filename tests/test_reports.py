"""Run reports: post_report validation, the fallback when it isn't called, and the Markdown form."""

import pytest

from space_agents.runtime.reports import fallback_report, parse_report, report_markdown, top_level


def test_parse_report_normalizes_and_validates():
    report = parse_report(
        {
            "summary": "  1 supplier delay  ",
            "items": [
                {"level": "ACTION", "title": "PO 45821 slipped", "detail": "Oct 7 → Oct 14", "sources": "acme.pdf p.2"},
                {"level": "info", "title": "2 POs delivered", "suggested_actions": ["File receipts", 3, ""]},
            ],
        }
    )
    assert report["summary"] == "1 supplier delay"
    assert report["items"][0]["level"] == "action" and report["items"][0]["sources"] == ["acme.pdf p.2"]
    assert report["items"][1]["suggested_actions"] == ["File receipts", "3"]
    assert top_level(report) == "action" and top_level({"summary": "x", "items": []}) is None
    for bad, why in (({}, "summary"), ({"summary": "x", "items": [{"title": "t", "level": "meh"}]}, "level")):
        with pytest.raises(ValueError, match=why):
            parse_report(bad)


def test_fallback_and_markdown():
    report = fallback_report("## All quiet\nNo new files in the folder.")
    assert report["summary"] == "All quiet" and report["items"][0]["level"] == "info"
    md = report_markdown(parse_report({"summary": "1 delay", "items": [{"level": "urgent", "title": "Line down"}]}))
    assert md.startswith("**1 delay**") and "**Urgent: Line down**" in md
    assert "Nothing new to report." in report_markdown({"summary": "Quiet", "items": []})
