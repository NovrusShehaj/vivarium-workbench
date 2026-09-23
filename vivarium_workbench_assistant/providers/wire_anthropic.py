"""The ``anthropic-messages`` wire protocol (Messages API, streamed).

``POST {base}/v1/messages`` with ``stream: true`` and the headers
``x-api-key`` + ``anthropic-version: 2023-06-01``. Consumes the events
``message_start``, ``content_block_start``, ``content_block_delta``
(``text_delta``, ``input_json_delta``; ``thinking_delta``/``signature_delta``
are not surfaced), ``content_block_stop``, ``message_delta`` (``stop_reason``,
usage), ``message_stop``, ``ping`` and ``error``.
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

MESSAGES_PATH = "/v1/messages"
MODELS_PATH = "/v1/models"
DEFAULT_MAX_TOKENS = 4096


def build_body(req: ChatRequest, *, default_max_tokens: int = DEFAULT_MAX_TOKENS) -> dict[str, Any]:
    turns: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)      # the API needs alternating roles
        else:
            turns.append({"role": role, "content": list(blocks)})

    for m in req.messages:
        if m.role == "user":
            text = m.text()
            push("user", [{"type": "text", "text": text}] if text else [])
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            for p in m.parts:
                if isinstance(p, TextPart) and p.text:
                    blocks.append({"type": "text", "text": p.text})
                elif isinstance(p, ToolCallPart):
                    blocks.append({"type": "tool_use", "id": p.id, "name": p.name, "input": p.arguments})
            push("assistant", blocks)
        elif m.role == "tool":
            blocks = []
            for p in m.parts:
                if isinstance(p, ToolResultPart):
                    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": p.tool_call_id,
                                             "content": p.content}
                    if p.is_error:
                        block["is_error"] = True
                    blocks.append(block)
            push("user", blocks)
    body: dict[str, Any] = {
        "model": req.model,
        "max_tokens": int(req.max_output_tokens or default_max_tokens),
        "messages": turns,
        "stream": True,
    }
    if req.system:
        body["system"] = req.system
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.tools:
        body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                         for t in req.tools]
    return body


_ERROR_KINDS = {
    "authentication_error": "auth",
    "permission_error": "permission",
    "not_found_error": "not_found",
    "rate_limit_error": "rate_limited",
    "overloaded_error": "overloaded",
    "api_error": "server",
    "invalid_request_error": "invalid_request",
    "request_too_large": "context_length",
}


def _stream_error(obj: dict[str, Any]) -> ProviderError:
    raw = obj.get("error")
    err: dict[str, Any] = raw if isinstance(raw, dict) else {}
    etype = str(err.get("type") or "")
    message = redact(str(err.get("message") or etype or "error"))[:300]
    kind = _ERROR_KINDS.get(etype, "server")
    if kind == "invalid_request" and ("too long" in message.lower() or "context" in message.lower()):
        kind = "context_length"
    return ProviderError(kind, f"Anthropic reported an error mid-stream: {message}")  # type: ignore[arg-type]


async def parse_stream(events: AsyncIterator[tuple[str, str]]) -> AsyncIterator[StreamEvent]:
    blocks: dict[int, dict[str, Any]] = {}
    input_tokens: int | None = None
    stop_reason: str | None = None
    finished = False
    async for event, data in events:
        try:
            obj = json.loads(data)
        except ValueError:
            raise ProviderError("server", "Anthropic sent malformed stream data.", retryable=False) from None
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type") or event
        if etype == "ping":
            continue
        if etype == "error":
            raise _stream_error(obj)
        if etype == "message_start":
            usage = (obj.get("message") or {}).get("usage") or {}
            if isinstance(usage.get("input_tokens"), int):
                input_tokens = usage["input_tokens"]
            continue
        if etype == "content_block_start":
            idx = obj.get("index", 0)
            block = obj.get("content_block") or {}
            btype = block.get("type")
            if btype == "tool_use":
                blocks[idx] = {"kind": "tool", "id": str(block.get("id") or f"toolu_{idx}")}
                yield ToolCallStart(id=blocks[idx]["id"], name=str(block.get("name") or ""))
            else:
                blocks[idx] = {"kind": btype or "text"}
                if btype == "text" and block.get("text"):
                    yield TextDelta(str(block["text"]))
            continue
        if etype == "content_block_delta":
            idx = obj.get("index", 0)
            delta = obj.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta" and isinstance(delta.get("text"), str):
                if delta["text"]:
                    yield TextDelta(delta["text"])
            elif dtype == "input_json_delta":
                b = blocks.get(idx)
                if b and b.get("kind") == "tool" and isinstance(delta.get("partial_json"), str):
                    yield ToolCallDelta(id=b["id"], arguments_fragment=delta["partial_json"])
            continue
        if etype == "content_block_stop":
            b = blocks.get(obj.get("index", 0))
            if b and b.get("kind") == "tool":
                yield ToolCallEnd(id=b["id"])
            continue
        if etype == "message_delta":
            delta = obj.get("delta") or {}
            if isinstance(delta.get("stop_reason"), str):
                stop_reason = delta["stop_reason"]
            usage = obj.get("usage") or {}
            if isinstance(usage.get("output_tokens"), int):
                yield UsageEvent(input_tokens=input_tokens, output_tokens=usage["output_tokens"])
            continue
        if etype == "message_stop":
            finished = True
            break
    if not finished:
        raise ProviderError("network", "The Anthropic stream ended unexpectedly.")
    yield DoneEvent(stop_reason=stop_reason or "end_turn")


_CAP_MAP = {
    "image_input": Cap.VISION,
    "vision": Cap.VISION,
    "structured_outputs": Cap.JSON_SCHEMA,
    "thinking": Cap.REASONING,
    "extended_thinking": Cap.REASONING,
}


def _supported(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, dict):
        return bool(v.get("supported", True))
    return v is not None


def parse_models(data: Any) -> tuple[list[ModelInfo], str | None]:
    """One page of ``GET /v1/models`` → models + the ``after_id`` for the next page."""
    out: list[ModelInfo] = []
    if not isinstance(data, dict):
        return out, None
    for it in data.get("data") or []:
        if not isinstance(it, dict) or not isinstance(it.get("id"), str):
            continue
        caps = {Cap.STREAMING, Cap.SYSTEM_PROMPT, Cap.TOOLS}
        raw_caps = it.get("capabilities")
        if isinstance(raw_caps, dict):
            for key, cap in _CAP_MAP.items():
                if key in raw_caps and _supported(raw_caps[key]):
                    caps.add(cap)
        out.append(ModelInfo(
            id=it["id"],
            display_name=it.get("display_name") if isinstance(it.get("display_name"), str) else None,
            context_window=it.get("max_input_tokens") if isinstance(it.get("max_input_tokens"), int) else None,
            max_output_tokens=it.get("max_tokens") if isinstance(it.get("max_tokens"), int) else None,
            caps=frozenset(caps), source="discovered",
        ))
    after = data.get("last_id") if data.get("has_more") else None
    return out, (after if isinstance(after, str) and after else None)
