import json

import httpx
import pytest

from space_agents.llm.client import ChatClient, LLMError


def sse(*chunks):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def client(handler):
    return ChatClient("https://llm.test/v1", "k", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_streams_text_and_tool_calls_and_usage():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer k"
        return sse(
            {"choices": [{"delta": {"content": "Let me "}}]},
            {"choices": [{"delta": {"content": "check."}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_document"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": '}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.pdf"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        )

    deltas = []
    result = await client(handler).chat(model="m", messages=[], tools=[{"type": "function"}], on_text=deltas.append)
    assert result.content == "Let me check." and deltas == ["Let me ", "check."]
    assert result.tool_calls[0].name == "read_document"
    assert result.tool_calls[0].parsed_arguments() == {"path": "a.pdf"}
    assert result.usage["total_tokens"] == 15
    assert seen[0]["stream"] is True and seen[0]["tool_choice"] == "auto"


async def test_drops_rejected_params_and_remembers():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "temperature" in body:
            err = {"message": "Unsupported value: 'temperature' does not support 0.2", "param": "temperature"}
            return httpx.Response(400, json={"error": err})
        return sse({"choices": [{"delta": {"content": "ok"}}]})

    c = client(handler)
    assert (await c.chat(model="gpt-5-nano", messages=[], temperature=0.2)).content == "ok"
    assert (await c.chat(model="gpt-5-nano", messages=[], temperature=0.2)).content == "ok"
    assert [("temperature" in b) for b in bodies] == [True, False, False]  # remembered for the model


async def test_falls_back_to_non_streaming():
    def handler(request):
        body = json.loads(request.content)
        if body["stream"]:
            return httpx.Response(400, json={"error": {"message": "Your organization must be verified to stream"}})
        assert "stream_options" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "whole"}, "finish_reason": "stop"}]})

    got = []
    result = await client(handler).chat(model="m", messages=[], on_text=got.append)
    assert result.content == "whole" and got == ["whole"]


async def test_auth_and_model_errors():
    with pytest.raises(LLMError, match="API key"):
        await client(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}})).chat(model="m", messages=[])
    nf = {"error": {"message": "The model `x` does not exist"}}
    with pytest.raises(LLMError, match="isn't available"):
        await client(lambda r: httpx.Response(404, json=nf)).chat(model="x", messages=[])


async def test_retries_server_errors(monkeypatch):
    import space_agents.llm.client as mod

    async def no_sleep(_):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return sse({"choices": [{"delta": {"content": "finally"}}]})

    assert (await client(handler).chat(model="m", messages=[])).content == "finally"
    assert len(calls) == 3
