"""Run reports: post_report validation, the fallback when it isn't called, and the Markdown form."""

import pytest

from space_agents.runtime.reports import fallback_report, fresh_items, parse_report, report_markdown, top_level


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


def test_findings_are_marked_new_changed_or_unchanged():
    report = parse_report(
        {
            "summary": "1 new delay",
            "items": [
                {"level": "urgent", "change": "new", "title": "PO 46100 line down"},
                {"level": "action", "change": "unchanged", "title": "PO 45821 still late"},
                {"level": "info", "title": "No change given: treated as new"},
            ],
        }
    )
    assert [i["change"] for i in report["items"]] == ["new", "unchanged", "new"]
    assert [i["title"] for i in fresh_items(report)] == ["PO 46100 line down", "No change given: treated as new"]
    assert top_level(report, fresh_only=True) == "urgent"
    still_open_only = {"summary": "x", "items": [report["items"][1]]}
    assert top_level(still_open_only) == "action" and top_level(still_open_only, fresh_only=True) is None
    assert "(still open, reported before)" in report_markdown(report)
    with pytest.raises(ValueError, match="change"):
        parse_report({"summary": "x", "items": [{"level": "info", "change": "maybe", "title": "t"}]})


def test_possible_repeats_match_reference_numbers_or_most_words():
    from space_agents.runtime.reports import looks_like, possible_repeats

    assert looks_like("PO 45821 still late", "PO 45821 delayed; new date Oct 14, 2026")  # same PO number
    assert not looks_like("Invoice 2026 totals", "Budget 2026 review")  # years aren't reference numbers
    assert looks_like("Acme heat treat outage delays", "Acme heat treat outage delays shipment")
    assert not looks_like("Beta Corp on time", "Acme heat treat outage")
    earlier = [
        {
            "finished_at": "2026-10-19T10:03:00+00:00",
            "created_at": "x",
            "report": {"items": [{"title": "PO 45821 delayed to Oct 14"}]},
        }
    ]
    report = parse_report(
        {
            "summary": "s",
            "items": [
                {"level": "action", "change": "new", "title": "PO 45821 delayed"},
                {"level": "action", "change": "unchanged", "title": "PO 45821 still late"},  # already labelled
                {"level": "urgent", "change": "new", "title": "PO 46100 line down"},
            ],
        }
    )
    assert possible_repeats(report, earlier) == [("PO 45821 delayed", "PO 45821 delayed to Oct 14", "2026-10-19 10:03")]
