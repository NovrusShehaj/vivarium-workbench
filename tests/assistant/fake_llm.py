"""A scriptable fake model provider — a real HTTP server on 127.0.0.1.

Speaks enough of three wire formats for contract tests, with no mocks inside
the code under test (the assistant talks to it over real sockets exactly as it
would to a provider):

* OpenAI Chat Completions: ``GET /v1/models``, ``POST /v1/chat/completions``
  (SSE; also under ``/v1beta/openai/`` and a Vertex-style
  ``/v1/projects/…/endpoints/openapi/`` prefix);
* Anthropic Messages: ``GET /v1/models``, ``POST /v1/messages`` (SSE);
* Gemini native model list: ``GET /v1beta/models``.

Each request is recorded (method, path, headers, JSON body). A test scripts
the next responses with ``queue(...)``; a streamed response can be slowed down
(``delay_s``) so tests can cancel or disconnect mid-stream, and the server
records whether the client went away.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Scripted:
    """One scripted response."""

    status: int = 200
    #: SSE events: list of (event_name_or_None, data_str)
    events: list[tuple[str | None, str]] = field(default_factory=list)
    json_body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0
    content_type: str = "text/event-stream"
    raw: bytes | None = None


def openai_text_events(*chunks: str, usage: tuple[int, int] | None = (12, 5)) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = [(None, json.dumps({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}))]
    for c in chunks:
        out.append((None, json.dumps({"choices": [{"index": 0, "delta": {"content": c}}]})))
    out.append((None, json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})))
    if usage:
        out.append((None, json.dumps({"choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})))
    out.append((None, "[DONE]"))
    return out


def openai_tool_call_events(call_id: str, name: str, args: dict[str, Any], text: str = "") -> list[tuple[str | None, str]]:
    raw = json.dumps(args)
    half = len(raw) // 2
    out: list[tuple[str | None, str]] = []
    if text:
        out.append((None, json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]})))
    out.append((None, json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": raw[:half]}}]}}]})))
    out.append((None, json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": raw[half:]}}]}}]})))
    out.append((None, json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})))
    out.append((None, "[DONE]"))
    return out


def anthropic_text_events(*chunks: str) -> list[tuple[str | None, str]]:
    ev: list[tuple[str | None, str]] = [
        ("message_start", json.dumps({"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 21}}})),
        ("content_block_start", json.dumps({"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}})),
        ("ping", json.dumps({"type": "ping"})),
    ]
    for c in chunks:
        ev.append(("content_block_delta", json.dumps({"type": "content_block_delta", "index": 0,
                                                      "delta": {"type": "text_delta", "text": c}})))
    ev += [
        ("content_block_stop", json.dumps({"type": "content_block_stop", "index": 0})),
        ("message_delta", json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                      "usage": {"output_tokens": 7}})),
        ("message_stop", json.dumps({"type": "message_stop"})),
    ]
    return ev


class FakeLLM:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.queue_: list[Scripted] = []
        self.default: Scripted | None = None
        self.disconnects = 0
        self.completed_streams = 0
        self._lock = threading.Lock()
        self.models = {"data": [{"id": "fake-model", "object": "model"}, {"id": "fake-tools", "object": "model"}]}
        self.anthropic_models = {"data": [{"id": "claude-fake", "display_name": "Claude Fake", "max_input_tokens": 200000,
                                           "max_tokens": 8192, "capabilities": {"image_input": {"supported": True}}}],
                                 "has_more": False, "last_id": "claude-fake"}
        self.gemini_models = {"models": [
            {"name": "models/gemini-fake", "displayName": "Gemini Fake", "inputTokenLimit": 1048576,
             "outputTokenLimit": 8192, "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/embed-fake", "supportedGenerationMethods": ["embedContent"]}]}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:   # keep test output clean
                pass

            def _record(self, body: Any) -> None:
                with fake._lock:
                    fake.requests.append({"method": self.command, "path": self.path,
                                          "headers": {k.lower(): v for k, v in self.headers.items()}, "json": body})

            def _send_json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                self._record(None)
                path = self.path.split("?", 1)[0]
                if path.endswith("/v1beta/models"):
                    self._send_json(200, fake.gemini_models)
                elif path.endswith("/v1/models") and self.headers.get("x-api-key"):
                    self._send_json(200, fake.anthropic_models)
                elif path.endswith("/models"):
                    self._send_json(200, fake.models)
                else:
                    self._send_json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                self._record(body)
                with fake._lock:
                    script = fake.queue_.pop(0) if fake.queue_ else fake.default
                if script is None:
                    script = Scripted(events=openai_text_events("ok"))
                if script.status >= 300 or script.json_body is not None:
                    self._send_json(script.status, script.json_body or {}, script.headers)
                    return
                self.send_response(script.status)
                self.send_header("Content-Type", script.content_type)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                for k, v in script.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    if script.raw is not None:
                        self.wfile.write(script.raw)
                        self.wfile.flush()
                    for name, data in script.events:
                        frame = (f"event: {name}\n" if name else "") + f"data: {data}\n\n"
                        self.wfile.write(frame.encode())
                        self.wfile.flush()
                        if script.delay_s:
                            time.sleep(script.delay_s)
                    with fake._lock:
                        fake.completed_streams += 1
                except (BrokenPipeError, ConnectionResetError):
                    with fake._lock:
                        fake.disconnects += 1
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def queue(self, *scripts: Scripted) -> None:
        with self._lock:
            self.queue_.extend(scripts)

    def start(self) -> "FakeLLM":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def posts(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]


# -- helpers shared by the assistant tests -----------------------------------

def add_local_provider(client, fake_llm, *, iid: str = "local", model: str | None = "fake-model",
                       **extra) -> dict:
    body = {"id": iid, "type": "openai_compatible", "preset": "custom", "base_url": fake_llm.base + "/v1",
            "credential": {"source": "none"}, **extra}
    if model:
        body["default_model"] = model
    r = client.post("/api/ext/assistant/providers", json=body)
    assert r.status_code == 201, r.text
    return r.json()["provider"]


def read_sse(text: str) -> list[tuple[str, dict]]:
    import json
    events = []
    for frame in text.split("\n\n"):
        name, data = "message", []
        for line in frame.splitlines():
            if line.startswith(":"):
                continue
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events
