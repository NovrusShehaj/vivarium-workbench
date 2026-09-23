"""Outbound HTTP policy: SSRF defences, DNS pinning, redirects, limits."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from vivarium_workbench_assistant.config import ProviderInstance
from vivarium_workbench_assistant.policy import HostedPolicy
from vivarium_workbench_assistant.providers import http as oh
from vivarium_workbench_assistant.providers.base import ProviderError


def inst(**kw):
    base = {"id": "p", "type": "openai_compatible", "display_name": "P", "credential": {"source": "none"},
            "base_url": "http://127.0.0.1:9/v1"}
    base.update(kw)
    return ProviderInstance.model_validate(base)


def blocked(fn, *a, **kw):
    with pytest.raises(ProviderError) as ei:
        fn(*a, **kw)
    assert ei.value.kind == "blocked_by_policy", ei.value.safe_message
    return ei.value


# -- static checks ------------------------------------------------------------

def test_cloud_profile_defaults_to_its_official_host():
    d = oh.destination_for(inst(type="anthropic", base_url=None, credential={"source": "keyring"}), mode="local")
    assert d.host == "api.anthropic.com" and d.scheme == "https"
    assert d.allow_private is False and d.allow_http is False


@pytest.mark.parametrize("url", [
    "ftp://127.0.0.1/v1", "file:///etc/passwd", "gopher://x", "javascript:alert(1)",
])
def test_unsupported_schemes(url):
    blocked(oh.destination_for, inst(base_url=url), mode="local")


def test_credential_bearing_urls_are_refused():
    blocked(oh.destination_for, inst(base_url="https://user:pass@example.com/v1"), mode="local")
    blocked(oh.destination_for, inst(base_url="https://example.com/v1?key=abc"), mode="local")
    blocked(oh.destination_for, inst(base_url="https://example.com/v1#frag"), mode="local")


def test_plain_http_is_only_for_local_endpoints():
    blocked(oh.destination_for, inst(type="openai", base_url="http://api.openai.com/v1",
                                     credential={"source": "keyring"}), mode="hosted",
            hosted=HostedPolicy(enabled=True, providers_allowed=frozenset({"openai"})))


def test_hosted_requires_provider_and_url_allowlist():
    hp = HostedPolicy(enabled=True, providers_allowed=frozenset({"anthropic"}))
    oh.destination_for(inst(type="anthropic", base_url=None, credential={"source": "env", "env_var": "K"}),
                       mode="hosted", hosted=hp)
    blocked(oh.destination_for, inst(type="openai", base_url=None, credential={"source": "env", "env_var": "K"}),
            mode="hosted", hosted=hp)
    # A custom base URL (gateway) needs the operator allowlist in hosted mode.
    blocked(oh.destination_for, inst(type="anthropic", base_url="https://gw.internal/anthropic",
                                     credential={"source": "env", "env_var": "K"}), mode="hosted", hosted=hp)
    hp2 = HostedPolicy(enabled=True, providers_allowed=frozenset({"anthropic"}),
                       base_url_allowlist=("https://gw.internal/anthropic",))
    d = oh.destination_for(inst(type="anthropic", base_url="https://gw.internal/anthropic/",
                                credential={"source": "env", "env_var": "K"}), mode="hosted", hosted=hp2)
    assert d.allow_private is True


def test_custom_ca_bundle_only_in_local_mode(tmp_path):
    hp = HostedPolicy(enabled=True, providers_allowed=frozenset({"openai_compatible"}),
                      base_url_allowlist=("https://llm.internal/v1",))
    blocked(oh.destination_for, inst(base_url="https://llm.internal/v1", ca_bundle_path=str(tmp_path / "ca.pem")),
            mode="hosted", hosted=hp)


# -- address checks -----------------------------------------------------------

def _dest(host, *, scheme="https", allow_private=False, allow_http=False, port=None):
    from vivarium_workbench_assistant.providers.profiles import PROFILES
    return oh.Destination(profile=PROFILES["openai_compatible"], base_url=f"{scheme}://{host}", scheme=scheme,
                          host=host, port=port or (443 if scheme == "https" else 80),
                          allow_private=allow_private, allow_http=allow_http)


@pytest.mark.parametrize("ip", ["169.254.169.254", "169.254.0.1", "100.100.100.200", "0.0.0.0", "224.0.0.1",
                                "255.255.255.255", "fe80::1", "fd00:ec2::254", "::", "::ffff:169.254.169.254"])
def test_metadata_link_local_and_reserved_always_blocked(ip):
    host = ip if ":" not in ip else ip
    d = _dest(host, allow_private=True)
    url = httpx.URL(f"https://[{ip}]/v1" if ":" in ip else f"https://{ip}/v1")
    with pytest.raises(ProviderError) as ei:
        asyncio.run(oh.check_and_pin(d, url))
    assert ei.value.kind == "blocked_by_policy"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "192.168.1.5", "172.16.0.9", "100.64.1.1", "::1", "fd12::1"])
def test_private_addresses_blocked_for_cloud(ip):
    d = _dest(ip, allow_private=False)
    url = httpx.URL(f"https://[{ip}]/v1" if ":" in ip else f"https://{ip}/v1")
    with pytest.raises(ProviderError) as ei:
        asyncio.run(oh.check_and_pin(d, url))
    assert ei.value.kind == "blocked_by_policy"


def test_private_address_allowed_for_configured_local_endpoint():
    d = _dest("127.0.0.1", scheme="http", allow_private=True, allow_http=True)
    assert asyncio.run(oh.check_and_pin(d, httpx.URL("http://127.0.0.1/v1"))) == "127.0.0.1"


def test_http_to_public_address_is_refused_even_when_http_allowed():
    d = _dest("93.184.216.34", scheme="http", allow_private=True, allow_http=True)
    with pytest.raises(ProviderError):
        asyncio.run(oh.check_and_pin(d, httpx.URL("http://93.184.216.34/v1")))


def test_request_cannot_leave_the_configured_endpoint():
    d = _dest("127.0.0.1", scheme="http", allow_private=True, allow_http=True, port=8080)
    with pytest.raises(ProviderError):
        asyncio.run(oh.check_and_pin(d, httpx.URL("http://127.0.0.1:9999/v1")))
    with pytest.raises(ProviderError):
        asyncio.run(oh.check_and_pin(d, httpx.URL("http://localhost.evil:8080/v1")))


def test_dns_rebinding_to_metadata_is_blocked(monkeypatch):
    async def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]
    loop_policy_host = "api.example.com"
    d = _dest(loop_policy_host)

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        await oh.check_and_pin(d, httpx.URL(f"https://{loop_policy_host}/v1"))
    with pytest.raises(ProviderError) as ei:
        asyncio.run(run())
    assert "metadata" in ei.value.safe_message or "link-local" in ei.value.safe_message


def test_url_builder_refuses_absolute_urls():
    client = oh.OutboundHTTP(_dest("127.0.0.1", scheme="http", allow_private=True, allow_http=True))
    with pytest.raises(ProviderError):
        client.url("http://evil.example/steal")
    with pytest.raises(ProviderError):
        client.url("//evil.example/steal")
    assert str(client.url("/models")) == "http://127.0.0.1/models"


# -- live behaviour against a local server -----------------------------------------

class _Server:
    def __init__(self, handler):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _client_for(port, **kw):
    d = oh.destination_for(inst(base_url=f"http://127.0.0.1:{port}/v1", **kw), mode="local")
    return oh.OutboundHTTP(d)


def test_redirects_are_not_followed():
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
    s = _Server(H)
    try:
        with pytest.raises(ProviderError) as ei:
            asyncio.run(_client_for(s.port).request_json("GET", "/models", headers={}))
        assert ei.value.kind == "blocked_by_policy"
        assert seen == ["/v1/models"]
    finally:
        s.close()


def test_pinned_request_carries_the_original_host_header():
    got = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            got["host"] = self.headers.get("Host")
            body = b'{"data": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    s = _Server(H)
    try:
        d = oh.destination_for(inst(base_url=f"http://localhost:{s.port}/v1"), mode="local")
        assert asyncio.run(oh.OutboundHTTP(d).request_json("GET", "/models", headers={})) == {"data": []}
        assert got["host"] == f"localhost:{s.port}"
    finally:
        s.close()


def test_oversized_json_response_is_refused(monkeypatch):
    monkeypatch.setattr(oh, "MAX_JSON_BODY", 1024)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"data": ["x" * 5000]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    s = _Server(H)
    try:
        with pytest.raises(ProviderError):
            asyncio.run(_client_for(s.port).request_json("GET", "/models", headers={}))
    finally:
        s.close()


def test_error_bodies_are_redacted_and_never_forwarded_raw():
    secret = "sk-proj-THISISASECRETVALUE0123456789"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"error": {"message": f"Incorrect API key provided: {secret}"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    s = _Server(H)
    try:
        with pytest.raises(ProviderError) as ei:
            asyncio.run(_client_for(s.port).request_json("GET", "/models", headers={}))
        assert ei.value.kind == "auth"
        assert secret not in ei.value.safe_message
    finally:
        s.close()


def test_connection_refused_is_a_clear_network_error():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(ProviderError) as ei:
        asyncio.run(_client_for(port).request_json("GET", "/models", headers={}))
    assert ei.value.kind == "network"
    assert "refused" in ei.value.safe_message.lower()


@pytest.mark.parametrize("status,kind,retryable", [
    (401, "auth", False), (403, "permission", False), (404, "not_found", False), (429, "rate_limited", True),
    (529, "overloaded", True), (500, "server", True), (503, "server", True), (400, "invalid_request", False),
])
def test_http_error_mapping(status, kind, retryable):
    e = oh.map_http_error(status, '{"error":{"message":"x"}}', {"retry-after": "7"}, "Prov")
    assert e.kind == kind and e.retryable is retryable
    if status in (429, 529):
        assert e.retry_after_s == 7.0


def test_context_length_detection():
    e = oh.map_http_error(400, '{"error":{"message":"prompt is too long: 300000 tokens > 200000 maximum"}}', {}, "A")
    assert e.kind == "context_length"


def test_retry_after_forms():
    assert oh.parse_retry_after({"retry-after-ms": "1500"}) == 1.5
    assert oh.parse_retry_after({"retry-after": "3"}) == 3.0
    assert oh.parse_retry_after({}) is None


def test_sse_line_cap(monkeypatch):
    monkeypatch.setattr(oh, "MAX_SSE_LINE", 1000)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: " + b"x" * 5000)       # no newline, ever
            self.wfile.flush()
    s = _Server(H)

    async def run():
        client = _client_for(s.port)
        async with client.stream("POST", "/chat/completions", headers={}, json_body={}) as resp:
            async for _ in oh.iter_sse_lines(resp):
                pass
    try:
        with pytest.raises(ProviderError):
            asyncio.run(run())
    finally:
        s.close()


def test_sse_event_parser_handles_comments_multiline_and_crlf():
    async def lines():
        for line in [": keep-alive", "event: a", "data: one", "data: two", "", "data: {\"x\":1}", "", "id: 5", ""]:
            yield line

    async def collect():
        return [e async for e in oh.iter_sse_events(lines())]
    assert asyncio.run(collect()) == [("a", "one\ntwo"), ("message", '{"x":1}')]
