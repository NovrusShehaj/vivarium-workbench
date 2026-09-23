"""Wire codecs: request serialisation and stream parsing (recorded-style fixtures)."""
from __future__ import annotations

import asyncio
import json

import pytest

from vivarium_workbench_assistant.providers import wire_anthropic as wa
from vivarium_workbench_assistant.providers import wire_openai_chat as wo
from vivarium_workbench_assistant.providers.base import (
    Cap,
    ChatMessage,
    ChatRequest,
    DoneEvent,
    ProviderError,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolDef,
    ToolResultPart,
    UsageEvent,
)


def run(parser, events):
    async def gen():
        for e in events:
            yield e

    async def collect():
        return [ev async for ev in parser(gen())]
    return asyncio.run(collect())


REQ = ChatRequest(
    model="m", system="SYS",
    messages=[
        ChatMessage("user", [TextPart("hi")]),
        ChatMessage("assistant", [TextPart("reading"), ToolCallPart("c1", "read_file", {"path": "a.txt"})]),
        ChatMessage("tool", [ToolResultPart("c1", "contents", is_error=False)]),
        ChatMessage("user", [TextPart("thanks")]),
    ],
    tools=[ToolDef("read_file", "Read", {"type": "object", "properties": {"path": {"type": "string"}}})],
    max_output_tokens=100,
)


def test_openai_body():
    b = wo.build_body(REQ, stream_usage=True)
    assert b["stream"] is True and b["stream_options"] == {"include_usage": True}
    assert b["messages"][0] == {"role": "system", "content": "SYS"}
    asst = b["messages"][2]
    assert asst["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path":"a.txt"}'}
    assert b["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "contents"}
    assert b["tools"][0]["function"]["name"] == "read_file" and b["tool_choice"] == "auto"
    assert "max_tokens" not in b      # left to the model default (some reject max_tokens)


def test_openai_stream_text_usage_and_done():
    evs = run(wo.parse_stream, [
        ("message", json.dumps({"choices": [{"delta": {"role": "assistant"}}]})),
        ("message", json.dumps({"choices": [{"delta": {"content": "Hel"}}]})),
        ("message", json.dumps({"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]})),
        ("message", json.dumps({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}})),
        ("message", "[DONE]"),
    ])
    assert [e.text for e in evs if isinstance(e, TextDelta)] == ["Hel", "lo"]
    assert UsageEvent(9, 2) in evs
    assert evs[-1] == DoneEvent("end_turn")


def test_openai_stream_tool_call_fragments():
    evs = run(wo.parse_stream, [
        ("message", json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "search", "arguments": '{"que'}}]}}]})),
        ("message", json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'ry":"x"}'}}]}}]})),
        ("message", json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})),
        ("message", "[DONE]"),
    ])
    assert ToolCallStart("call_1", "search") in evs
    assert "".join(e.arguments_fragment for e in evs if isinstance(e, ToolCallDelta)) == '{"query":"x"}'
    assert ToolCallEnd("call_1") in evs
    assert evs[-1] == DoneEvent("tool_use")


def test_openai_stream_error_and_truncation():
    with pytest.raises(ProviderError) as ei:
        run(wo.parse_stream, [("message", json.dumps({"error": {"message": "Rate limit reached", "code": 429}}))])
    assert ei.value.kind == "rate_limited"
    with pytest.raises(ProviderError) as ei:
        run(wo.parse_stream, [("message", json.dumps({"choices": [{"delta": {"content": "par"}}]}))])
    assert ei.value.kind == "network"
    with pytest.raises(ProviderError):
        run(wo.parse_stream, [("message", "{not json")])


def test_openai_models_openrouter_and_ollama_shapes():
    ms = wo.parse_models({"data": [
        {"id": "a/b", "name": "B", "context_length": 128000, "supported_parameters": ["tools", "temperature"],
         "top_provider": {"max_completion_tokens": 4096}},
        {"id": "llama3", "object": "model"}, {"id": "llama3"}, {"nope": 1}]})
    assert [m.id for m in ms] == ["a/b", "llama3"]
    assert ms[0].context_window == 128000 and ms[0].max_output_tokens == 4096 and Cap.TOOLS in ms[0].caps
    assert ms[1].caps is None


def test_gemini_native_models():
    ms, tok = wo.parse_gemini_native_models({"models": [
        {"name": "models/gemini-x", "displayName": "X", "inputTokenLimit": 1000, "outputTokenLimit": 100,
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "t2"})
    assert [m.id for m in ms] == ["gemini-x"] and ms[0].context_window == 1000 and tok == "t2"


def test_anthropic_body_merges_roles_and_maps_tools():
    b = wa.build_body(REQ)
    assert b["system"] == "SYS" and b["max_tokens"] == 100 and b["stream"] is True
    roles = [t["role"] for t in b["messages"]]
    assert roles == ["user", "assistant", "user"]        # tool result + next user turn merged
    assert b["messages"][1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "read_file",
                                              "input": {"path": "a.txt"}}
    assert b["messages"][2]["content"][0]["type"] == "tool_result"
    assert b["tools"][0]["input_schema"]["type"] == "object"
    default = wa.build_body(ChatRequest(model="m", system=None, messages=[ChatMessage("user", [TextPart("x")])]))
    assert default["max_tokens"] == wa.DEFAULT_MAX_TOKENS and "system" not in default


def test_anthropic_stream_full_sequence():
    events = [
        ("message_start", json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 30}}})),
        ("content_block_start", json.dumps({"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}})),
        ("ping", json.dumps({"type": "ping"})),
        ("content_block_delta", json.dumps({"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": "Hi"}})),
        ("content_block_stop", json.dumps({"type": "content_block_stop", "index": 0})),
        ("content_block_start", json.dumps({"type": "content_block_start", "index": 1,
                                            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search"}})),
        ("content_block_delta", json.dumps({"type": "content_block_delta", "index": 1,
                                            "delta": {"type": "input_json_delta", "partial_json": '{"query":'}})),
        ("content_block_delta", json.dumps({"type": "content_block_delta", "index": 1,
                                            "delta": {"type": "input_json_delta", "partial_json": '"x"}'}})),
        ("content_block_stop", json.dumps({"type": "content_block_stop", "index": 1})),
        ("message_delta", json.dumps({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                                      "usage": {"output_tokens": 12}})),
        ("message_stop", json.dumps({"type": "message_stop"})),
    ]
    evs = run(wa.parse_stream, events)
    assert TextDelta("Hi") in evs
    assert ToolCallStart("toolu_1", "search") in evs and ToolCallEnd("toolu_1") in evs
    assert "".join(e.arguments_fragment for e in evs if isinstance(e, ToolCallDelta)) == '{"query":"x"}'
    assert UsageEvent(30, 12) in evs and evs[-1] == DoneEvent("tool_use")


@pytest.mark.parametrize("etype,kind", [("overloaded_error", "overloaded"), ("rate_limit_error", "rate_limited"),
                                        ("api_error", "server"), ("authentication_error", "auth")])
def test_anthropic_midstream_errors(etype, kind):
    with pytest.raises(ProviderError) as ei:
        run(wa.parse_stream, [("error", json.dumps({"type": "error", "error": {"type": etype, "message": "m"}}))])
    assert ei.value.kind == kind


def test_anthropic_truncated_stream():
    with pytest.raises(ProviderError) as ei:
        run(wa.parse_stream, [("message_start", json.dumps({"type": "message_start", "message": {}}))])
    assert ei.value.kind == "network"


def test_anthropic_models_pagination():
    page, after = wa.parse_models({"data": [{"id": "claude-a", "display_name": "A", "max_input_tokens": 200000,
                                             "max_tokens": 64000, "capabilities": {"image_input": {"supported": True},
                                                                                   "thinking": {"supported": False}}}],
                                   "has_more": True, "last_id": "claude-a"})
    assert after == "claude-a"
    m = page[0]
    assert m.context_window == 200000 and m.max_output_tokens == 64000
    assert Cap.VISION in m.caps and Cap.REASONING not in m.caps and Cap.TOOLS in m.caps
    _page, after2 = wa.parse_models({"data": [], "has_more": False})
    assert after2 is None
