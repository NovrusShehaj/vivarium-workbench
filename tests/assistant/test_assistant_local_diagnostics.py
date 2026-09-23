"""Local-model connection diagnostics (plan §6.7, Phase 5): every way a local
endpoint commonly fails gets an actionable message, and never a raw traceback.

Real sockets throughout: the fake OpenAI-compatible server, a server that
answers 404, a TCP listener that never answers, a plain-HTTP port spoken to
over TLS, a closed port, and an unresolvable ``.invalid`` host name.
"""
from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .fake_llm import Scripted, openai_text_events
from vivarium_workbench_assistant.config import ProviderInstance
from vivarium_workbench_assistant.providers.adapters import ProviderAdapter
from vivarium_workbench_assistant.providers.auth_google import GoogleTokenProvider
from vivarium_workbench_assistant.providers.base import ChatMessage, ChatRequest, ProviderError, TextDelta, TextPart
from vivarium_workbench_assistant.secrets import SecretStore


def _adapter(base_url: str, **extra) -> ProviderAdapter:
    inst = ProviderInstance(id="local", type="openai_compatible", display_name="Local", preset="custom",
                            credential={"source": "none"}, base_url=base_url, **extra)
    store = SecretStore()
    return ProviderAdapter(inst, mode="local", secrets=store, google=GoogleTokenProvider(store))


def _validate(ad: ProviderAdapter):
    return asyncio.run(ad.validate())


def _chat(ad: ProviderAdapter, model: str) -> str:
    async def go():
        out = ""
        async for ev in ad.stream_chat(ChatRequest(model=model, system=None,
                                                   messages=[ChatMessage("user", [TextPart("hi")])])):
            if isinstance(ev, TextDelta):
                out += ev.text
        return out
    return asyncio.run(go())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def not_found_server():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            body = b"404 page not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def silent_server():
    """Accepts connections and never sends a byte (a model still loading)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    conns = []
    stop = threading.Event()

    def accept():
        s.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = s.accept()
                conns.append(c)
            except OSError:
                continue
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{s.getsockname()[1]}"
    stop.set()
    t.join(timeout=2)
    for c in conns:
        c.close()
    s.close()


def test_reachable_server_lists_models(fake_llm):
    r = _validate(_adapter(fake_llm.base + "/v1"))
    assert r.ok and r.models_count == 2 and not r.notes


def test_empty_model_list_is_ok_with_a_hint(fake_llm):
    fake_llm.models = {"data": []}
    r = _validate(_adapter(fake_llm.base + "/v1"))
    assert r.ok and r.models_count == 0
    assert "no models" in " ".join(r.notes).lower()


def test_wrong_base_url_404_on_models(not_found_server):
    r = _validate(_adapter(not_found_server + "/v1"))
    assert not r.ok
    assert r.error["kind"] == "not_found" and r.error["status"] == 404
    assert "could not find that model or endpoint (404)" in r.error["message"]


def test_connection_refused(fake_llm):
    port = _free_port()          # nothing listens here
    r = _validate(_adapter(f"http://127.0.0.1:{port}/v1"))
    assert not r.ok and r.error["kind"] == "network"
    assert "connection refused" in r.error["message"].lower()


def test_dns_failure():
    r = _validate(_adapter("http://no-such-host.invalid:11434/v1"))
    assert not r.ok
    assert r.error["kind"] == "network"
    assert r.error["message"].startswith("DNS lookup failed for no-such-host.invalid")


def test_tls_to_a_plain_http_port(fake_llm):
    r = _validate(_adapter(fake_llm.base.replace("http://", "https://") + "/v1"))
    assert not r.ok and r.error["kind"] == "network"
    assert "tls" in r.error["message"].lower()


def test_unknown_model_is_reported(fake_llm):
    fake_llm.queue(Scripted(status=404, json_body={"error": {"message": "model 'nope' not found, try pulling it first"}}))
    with pytest.raises(ProviderError) as exc:
        _chat(_adapter(fake_llm.base + "/v1"), "nope")
    assert exc.value.kind == "not_found"
    assert "model 'nope' not found" in exc.value.safe_message      # the server's own hint is kept


def test_slow_first_token_times_out_with_a_clear_message(silent_server):
    ad = _adapter(silent_server + "/v1", first_token_timeout_s=5)
    with pytest.raises(ProviderError) as exc:
        _chat(ad, "loading-model")
    assert exc.value.kind == "timeout"
    assert "did not respond in time" in exc.value.safe_message


def test_malformed_sse_is_an_error_not_a_crash(fake_llm):
    fake_llm.queue(Scripted(events=[(None, "{not json"), (None, "[DONE]")]))
    with pytest.raises(ProviderError) as exc:
        _chat(_adapter(fake_llm.base + "/v1"), "fake-model")
    assert exc.value.kind == "server" and "malformed" in exc.value.safe_message


def test_happy_chat_after_diagnostics(fake_llm):
    fake_llm.queue(Scripted(events=openai_text_events("ok")))
    assert _chat(_adapter(fake_llm.base + "/v1"), "fake-model") == "ok"
