"""Compaction helpers: what gets summarized, what's kept, and the fallback."""

from space_agents.runtime import compaction


def tool_round(i, result="x" * 400):
    call = {"id": f"c{i}", "type": "function", "function": {"name": "read_document", "arguments": '{"path": "a.pdf"}'}}
    return [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": f"c{i}", "content": result},
    ]


SYSTEM = {"role": "system", "content": "You are an agent."}
REQUEST = {"role": "user", "content": "Summarize the supplier PDFs."}


def test_keeps_the_latest_round_and_the_request():
    messages = [SYSTEM, {"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}, REQUEST]
    messages += tool_round(1) + tool_round(2)
    plan = compaction.plan(messages, REQUEST["content"])
    assert messages[plan.keep_from]["tool_calls"][0]["id"] == "c2"
    new = compaction.compacted(messages, plan, "Read a.pdf: 3 POs late.", 2_000)
    assert new[0] == SYSTEM and len(new) == 4
    assert "3 POs late" in new[1]["content"] and new[1]["content"].endswith(REQUEST["content"])
    assert new[2:] == messages[-2:]  # tool call and its result stay together


def test_summarizes_everything_when_only_the_latest_round_exists():
    messages = [SYSTEM, REQUEST, *tool_round(1)]
    plan = compaction.plan(messages, REQUEST["content"])
    assert plan.keep_from == len(messages)
    assert compaction.plan([SYSTEM, REQUEST], REQUEST["content"]) is None  # nothing to compact


def test_summary_is_capped_and_transcript_is_plain_text():
    messages = [SYSTEM, REQUEST, *tool_round(1)]
    plan = compaction.plan(messages, REQUEST["content"])
    new = compaction.compacted(messages, plan, "word " * 5_000, 200)
    assert len(new[1]["content"]) < 200 * compaction.CHARS_PER_TOKEN * compaction.OVERSHOOT + 200
    text = compaction.summarizer_messages(messages[1:], 2_000)[1]["content"]
    assert "USER:\nSummarize the supplier PDFs." in text and "AGENT CALLED TOOL read_document" in text


def test_fallback_elides_old_tool_results_but_not_the_latest():
    messages = [SYSTEM, REQUEST, *tool_round(1), *tool_round(2)]
    out = compaction.elided(messages, compaction.plan(messages, REQUEST["content"]).keep_from)
    assert out[3]["content"] == compaction.ELIDED and out[5]["content"] == "x" * 400


def test_meter_uses_the_models_count_plus_an_estimate():
    meter = compaction.ContextMeter()
    messages = [SYSTEM, REQUEST]
    assert meter.size(messages) == compaction.estimate_tokens(messages)
    meter.measure(50_000, 2)
    messages += tool_round(1, "y" * 4_000)
    assert 51_000 <= meter.size(messages) <= 51_020
