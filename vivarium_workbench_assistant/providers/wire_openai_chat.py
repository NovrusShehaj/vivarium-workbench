"""The ``openai-chat`` wire protocol (Chat Completions, streamed).

Used by OpenAI, Vertex AI's OpenAI-compatible endpoint, Google AI Studio's
OpenAI compatibility layer, OpenRouter and local servers (Ollama, LM Studio,
vLLM, llama.cpp). Streams ``data:`` chunks until ``data: [DONE]``; SSE comment
lines (OpenRouter keep-alives) are ignored. Tool-call argument fragments are
accumulated per ``index`` and emitted as complete calls when the stream ends,
which tolerates servers that send the name after the id.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from vivarium_workbench_assistant.providers.base import (
    Cap,
    ChatRequest,
    DoneEvent,
    ModelInfo,
    ProviderError,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    UsageEvent,
)
from vivarium_workbench_assistant.secrets import redact

CHAT_PATH = "/chat/completions"
MODELS_PATH = "/models"

_STOP_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use",
             "length": "max_tokens", "content_filter": "content_filter"}


def build_body(req: ChatRequest, *, stream_usage: bool = False) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if req.system:
        messages.append({"role": "system", "content": req.system})
    for m in req.messages:
        if m.role == "user":
            messages.append({"role": "user", "content": m.text()})
        elif m.role == "assistant":
            calls = [p for p in m.parts if isinstance(p, ToolCallPart)]
            text = m.text()
            msg: dict[str, Any] = {"role": "assistant", "content": text if (text or not calls) else None}
            if calls:
                msg["tool_calls"] = [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.arguments, separators=(",", ":"))}}
                    for c in calls
                ]
            messages.append(msg)
        elif m.role == "tool":
            for p in m.parts:
                if isinstance(p, ToolResultPart):
                    messages.append({"role": "tool", "tool_call_id": p.tool_call_id, "content": p.content})
                elif isinstance(p, TextPart) and p.text:
                    messages.append({"role": "user", "content": p.text})
    body: dict[str, Any] = {"model": req.model, "messages": messages, "stream": True}
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.tools:
        body["tools"] = [
            {"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.parameters}}
            for t in req.tools
        ]
        body["tool_choice"] = "auto"
    if stream_usage:
        body["stream_options"] = {"include_usage": True}
    return body


def _stream_error(obj: dict[str, Any]) -> ProviderError:
    err = obj.get("error")
    message = ""
    code: Any = None
    if isinstance(err, dict):
        message = str(err.get("message") or "")
        code = err.get("code") or err.get("type") or err.get("status")
    elif isinstance(err, str):
        message = err
    safe = redact(message)[:300] or "the provider reported an error"
    low = f"{code} {safe}".lower()
    if "rate" in low or str(code) == "429":
        return ProviderError("rate_limited", f"Rate limited: {safe}")
    if "overload" in low or str(code) == "529":
        return ProviderError("overloaded", f"The provider is overloaded: {safe}")
    if "context" in low or "too long" in low:
        return ProviderError("context_length", f"Too much context: {safe}", retryable=False)
    return ProviderError("server", f"The provider reported an error mid-stream: {safe}", retryable=False)


async def parse_stream(events: AsyncIterator[tuple[str, str]]) -> AsyncIterator[StreamEvent]:
    calls: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    stop_reason: str | None = None
    saw_done = False
    async for _event, data in events:
        payload = data.strip()
        if not payload:
            continue
        if payload == "[DONE]":
            saw_done = True
            break
        try:
            obj = json.loads(payload)
        except ValueError:
            raise ProviderError("server", "The provider sent malformed stream data.", retryable=False) from None
        if not isinstance(obj, dict):
            continue
        if obj.get("error") is not None:
            raise _stream_error(obj)
        usage = obj.get("usage")
        if isinstance(usage, dict):
            yield UsageEvent(input_tokens=_int(usage.get("prompt_tokens")),
                             output_tokens=_int(usage.get("completion_tokens")))
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield TextDelta(content)
                for tc in delta.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    idx = tc.get("index", 0) if isinstance(tc.get("index", 0), int) else 0
                    slot = calls.get(idx)
                    if slot is None:
                        slot = {"id": "", "name": "", "args": ""}
                        calls[idx] = slot
                        order.append(idx)
                    if tc.get("id"):
                        slot["id"] = str(tc["id"])
                    fn = tc.get("function") or {}
                    if isinstance(fn, dict):
                        if fn.get("name") and not slot["name"]:
                            slot["name"] = str(fn["name"])
                        if isinstance(fn.get("arguments"), str):
                            slot["args"] += fn["arguments"]
            fr = choice.get("finish_reason")
            if isinstance(fr, str) and fr:
                stop_reason = _STOP_MAP.get(fr, fr)
    if not saw_done and stop_reason is None and not calls:
        # The connection ended without [DONE] or a finish_reason: truncated.
        raise ProviderError("network", "The provider stream ended unexpectedly.")
    for i, idx in enumerate(order):
        slot = calls[idx]
        cid = slot["id"] or f"call_{i}"
        yield ToolCallStart(id=cid, name=slot["name"])
        if slot["args"]:
            yield ToolCallDelta(id=cid, arguments_fragment=slot["args"])
        yield ToolCallEnd(id=cid)
    if calls and stop_reason in (None, "end_turn"):
        stop_reason = "tool_use"
    yield DoneEvent(stop_reason=stop_reason or "end_turn")


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_models(data: Any) -> list[ModelInfo]:
    """``GET /models`` → models (OpenAI, OpenRouter, Ollama, LM Studio, compat layers)."""
    items = data.get("data") if isinstance(data, dict) else data
    out: list[ModelInfo] = []
    seen: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        mid = it.get("id")
        if not isinstance(mid, str) or not mid or mid in seen:
            continue
        seen.add(mid)
        ctx = _int(it.get("context_length")) or _int(it.get("context_window"))
        top = it.get("top_provider") if isinstance(it.get("top_provider"), dict) else {}
        max_out = _int(top.get("max_completion_tokens")) if top else None
        params = it.get("supported_parameters")
        caps: frozenset[Cap] | None = None
        if isinstance(params, list):
            c = {Cap.STREAMING, Cap.SYSTEM_PROMPT}
            if "tools" in params:
                c.add(Cap.TOOLS)
            if "structured_outputs" in params or "response_format" in params:
                c.add(Cap.JSON_SCHEMA)
            caps = frozenset(c)
        name = it.get("name") if isinstance(it.get("name"), str) else None
        out.append(ModelInfo(id=mid, display_name=name, context_window=ctx,
                             max_output_tokens=max_out, caps=caps, source="discovered"))
    return out


def parse_gemini_native_models(data: Any) -> tuple[list[ModelInfo], str | None]:
    """Gemini ``GET /v1beta/models`` page → chat-capable models + next page token.

    Native ids are ``models/<id>``; the OpenAI compatibility layer takes the
    bare ``<id>``, which is what is stored.
    """
    out: list[ModelInfo] = []
    if not isinstance(data, dict):
        return out, None
    for it in data.get("models") or []:
        if not isinstance(it, dict):
            continue
        methods = it.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        name = str(it.get("name") or "")
        mid = name.split("/", 1)[1] if name.startswith("models/") else name
        if not mid:
            continue
        caps = {Cap.STREAMING, Cap.SYSTEM_PROMPT, Cap.TOOLS}
        if it.get("thinking"):
            caps.add(Cap.REASONING)
        out.append(ModelInfo(
            id=mid,
            display_name=it.get("displayName") if isinstance(it.get("displayName"), str) else None,
            context_window=_int(it.get("inputTokenLimit")),
            max_output_tokens=_int(it.get("outputTokenLimit")),
            caps=frozenset(caps), source="discovered",
        ))
    token = data.get("nextPageToken")
    return out, (token if isinstance(token, str) and token else None)
