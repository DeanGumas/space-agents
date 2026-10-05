"""OpenAI-compatible Chat Completions client: streaming, tool calls, retries, and parameter compatibility.

Works with OpenAI and vLLM-style gateways. Optional parameters that a particular model rejects (e.g. GPT-5 models
reject a custom `temperature`) are dropped automatically and remembered per model, so the same agent config works
across providers. If a provider refuses streaming for a model, we fall back to a single non-streamed response.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Optional request parameters we're willing to drop if a model rejects them.
OPTIONAL_PARAMS = ("temperature", "reasoning_effort", "max_completion_tokens", "parallel_tool_calls", "stream_options")
RETRY_STATUS = (408, 409, 429, 500, 502, 503, 504)


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text, as produced by the model

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Arguments were not valid JSON: {e}") from e
        if not isinstance(value, dict):
            raise ValueError("Arguments must be a JSON object.")
        return value


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class ChatClient:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
        self._dropped: dict[str, set[str]] = {}  # model -> params the provider rejected
        self._no_stream: set[str] = set()  # models the provider won't stream

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        max_completion_tokens: int | None = 8000,
        on_text: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> ChatResult:
        optional: dict[str, Any] = {
            "temperature": temperature,
            "reasoning_effort": reasoning_effort or None,
            "max_completion_tokens": max_completion_tokens,
        }
        if tools:
            optional["parallel_tool_calls"] = True
        for attempt in range(6):
            stream = model not in self._no_stream
            body: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
            optional["stream_options"] = {"include_usage": True} if stream else None
            dropped = self._dropped.get(model, set())
            body.update({k: v for k, v in optional.items() if v is not None and k not in dropped})
            try:
                if stream:
                    return await self._stream(body, on_text)
                result = await self._once(body)
                if on_text and result.content:
                    maybe = on_text(result.content)
                    if asyncio.iscoroutine(maybe):
                        await maybe
                return result
            except _Retry as r:
                if r.drop_param:
                    log.info("Model %s rejected %r; retrying without it", model, r.drop_param)
                    self._dropped.setdefault(model, set()).add(r.drop_param)
                elif r.no_stream:
                    log.info("Streaming not available for %s; using non-streamed responses", model)
                    self._no_stream.add(model)
                else:
                    delay = r.after if r.after is not None else min(2**attempt, 20)
                    log.warning("LLM request failed (%s); retrying in %.1fs", r.reason, delay)
                    await asyncio.sleep(delay)
        raise LLMError("The model server kept failing. Try again in a minute.")

    # --- transport ----------------------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def _raise_for(self, resp: httpx.Response, body: dict[str, Any]) -> None:
        if resp.status_code < 400:
            return
        raw = (await resp.aread()).decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error") or {}
        except (json.JSONDecodeError, AttributeError):
            err = {}
        message = err.get("message") if isinstance(err, dict) else None
        message = message or raw[:300] or resp.reason_phrase
        param = err.get("param") if isinstance(err, dict) else None
        if resp.status_code == 400:
            if param in OPTIONAL_PARAMS and param in body:
                raise _Retry(drop_param=param, reason=message)
            lowered = message.lower()
            for p in OPTIONAL_PARAMS:  # some servers don't fill `param`; match on the message
                if p in body and any(q + p + q in lowered for q in ("'", '"', "`")):
                    raise _Retry(drop_param=p, reason=message)
            if body.get("stream") and "stream" in lowered:
                raise _Retry(no_stream=True, reason=message)
        if resp.status_code in RETRY_STATUS:
            after = resp.headers.get("retry-after")
            raise _Retry(reason=f"HTTP {resp.status_code}", after=float(after) if after and after.isdigit() else None)
        if resp.status_code in (401, 403):
            raise LLMError("The model server rejected the API key.", resp.status_code)
        if resp.status_code == 404 and "model" in message.lower():
            raise LLMError(f"Model '{body.get('model')}' isn't available on this server. {message}", 404)
        raise LLMError(f"Model server error ({resp.status_code}): {message}", resp.status_code)

    async def _once(self, body: dict[str, Any]) -> ChatResult:
        try:
            resp = await self._http.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers())
        except httpx.TransportError as e:
            raise _Retry(reason=str(e)) from e
        await self._raise_for(resp, body)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            calls.append(ToolCall(c.get("id", ""), fn.get("name", ""), fn.get("arguments", "")))
        return ChatResult(msg.get("content") or "", calls, choice.get("finish_reason"), _usage(data.get("usage")))

    async def _stream(self, body: dict[str, Any], on_text: Callable[[str], Any] | None) -> ChatResult:
        result = ChatResult()
        calls: dict[int, dict[str, str]] = {}
        try:
            async with self._http.stream(
                "POST", f"{self.base_url}/chat/completions", json=body, headers=self._headers()
            ) as resp:
                await self._raise_for(resp, body)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        result.usage = _usage(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            result.content += delta["content"]
                            if on_text:
                                maybe = on_text(delta["content"])
                                if asyncio.iscoroutine(maybe):
                                    await maybe
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["arguments"] += fn.get("arguments") or ""
                        if choice.get("finish_reason"):
                            result.finish_reason = choice["finish_reason"]
        except httpx.TransportError as e:
            if result.content or calls:
                raise LLMError(f"The connection to the model server dropped mid-response: {e}") from e
            raise _Retry(reason=str(e)) from e
        result.tool_calls = [ToolCall(c["id"], c["name"], c["arguments"]) for _, c in sorted(calls.items())]
        return result


def _usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    out = {k: int(raw[k]) for k in keys if isinstance(raw.get(k), int)}
    details = raw.get("completion_tokens_details") or {}
    if isinstance(details.get("reasoning_tokens"), int):
        out["reasoning_tokens"] = details["reasoning_tokens"]
    return out


class _Retry(Exception):
    def __init__(self, *, reason: str = "", drop_param: str | None = None, no_stream: bool = False, after=None):
        super().__init__(reason)
        self.reason = reason
        self.drop_param = drop_param
        self.no_stream = no_stream
        self.after = after
