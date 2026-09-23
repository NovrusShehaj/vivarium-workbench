"""Host-header allowlist (DNS-rebinding defense) — lib.host_guard.

A DNS-rebinding page sends ``Origin: http://evil.example:PORT`` together with
``Host: evil.example:PORT`` to the loopback server, which passes the same-origin
CSRF check. The Host guard refuses any Host that is not a loopback name (or an
operator-configured name) whenever the server is bound to loopback.
"""
from __future__ import annotations

import asyncio
import http.client
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vivarium_workbench.api.app import create_app, get_workspace
from vivarium_workbench.lib import host_guard, server_runtime


@pytest.fixture(autouse=True)
def _clean_runtime(monkeypatch):
    monkeypatch.delenv(server_runtime.BIND_HOST_ENV, raising=False)
    monkeypatch.delenv("VIVARIUM_WORKBENCH_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("VIVARIUM_DASHBOARD_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("VIVARIUM_WORKBENCH_TRUST_PROXY", raising=False)
    server_runtime.reset()
    yield
    server_runtime.reset()


def _client(tmp_path: Path) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_workspace] = lambda: tmp_path
    return TestClient(app)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("127.0.0.1:8000", ("127.0.0.1", "8000")),
    ("localhost", ("localhost", None)),
    ("LocalHost:9", ("localhost", "9")),
    ("[::1]:8000", ("::1", "8000")),
    ("[::1]", ("::1", None)),
    ("evil.example:80", ("evil.example", "80")),
])
def test_split_host_port(value, expected):
    assert host_guard.split_host_port(value) == expected


def test_guard_inactive_when_bind_unknown_and_nothing_configured():
    assert host_guard.effective_allowed_hosts({}) is None


def test_loopback_bind_enforces_loopback_names():
    server_runtime.configure(bind_host="127.0.0.1")
    allowed = host_guard.effective_allowed_hosts({})
    assert allowed == frozenset({"127.0.0.1", "localhost", "::1"})


def test_configured_hosts_extend_the_loopback_set():
    server_runtime.configure(bind_host="127.0.0.1")
    env = {"VIVARIUM_WORKBENCH_ALLOWED_HOSTS": " Workbench.Internal , example.test:8443 "}
    allowed = host_guard.effective_allowed_hosts(env)
    assert {"workbench.internal", "example.test:8443", "127.0.0.1"} <= set(allowed)


def test_wildcard_disables_the_guard():
    server_runtime.configure(bind_host="127.0.0.1")
    assert host_guard.effective_allowed_hosts({"VIVARIUM_WORKBENCH_ALLOWED_HOSTS": "*"}) is None


def test_non_loopback_bind_is_opt_in():
    server_runtime.configure(bind_host="0.0.0.0")
    assert host_guard.effective_allowed_hosts({}) is None
    allowed = host_guard.effective_allowed_hosts({"VIVARIUM_WORKBENCH_ALLOWED_HOSTS": "wb.example"})
    assert "wb.example" in allowed


def test_bind_host_env_fallback_activates_the_guard(monkeypatch):
    monkeypatch.setenv(server_runtime.BIND_HOST_ENV, "localhost")
    assert host_guard.effective_allowed_hosts({}) is not None


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:61234", True),
    ("localhost:8000", True),
    ("[::1]:8000", True),
    ("evil.example:8000", False),
    ("127.0.0.1.nip.io:8000", False),
    ("localhost.evil.example", False),
    ("", False),
    (None, False),
])
def test_is_host_allowed_against_loopback(host, ok):
    assert host_guard.is_host_allowed(host, host_guard.LOOPBACK_ALLOWED) is ok


def test_port_pinned_entry_matches_only_that_port():
    allowed = {"wb.example:8443"}
    assert host_guard.is_host_allowed("wb.example:8443", allowed)
    assert not host_guard.is_host_allowed("wb.example:9000", allowed)
    assert not host_guard.is_host_allowed("wb.example", allowed)


# ---------------------------------------------------------------------------
# Through the real middleware stack (TestClient)
# ---------------------------------------------------------------------------

def test_rebound_host_is_rejected_with_400_envelope(tmp_path):
    server_runtime.configure(bind_host="127.0.0.1")
    c = _client(tmp_path)
    r = c.get("/health", headers={"Host": "evil.example:8000"})
    assert r.status_code == 400
    assert r.json()["error"] == "host not allowed"
    assert r.headers["cache-control"] == "no-store"


def test_rebinding_post_is_rejected_even_though_origin_matches_host(tmp_path):
    """The attack the CSRF guard alone cannot see: Origin == Host == attacker name."""
    server_runtime.configure(bind_host="127.0.0.1")
    c = _client(tmp_path)
    r = c.post("/api/__host_probe__", json={},
               headers={"Host": "evil.example:8000", "Origin": "http://evil.example:8000"})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost"])
def test_loopback_hosts_are_served(tmp_path, host):
    server_runtime.configure(bind_host="127.0.0.1")
    c = _client(tmp_path)
    assert c.get("/health", headers={"Host": host}).status_code == 200


def test_configured_host_is_served(tmp_path, monkeypatch):
    server_runtime.configure(bind_host="127.0.0.1")
    monkeypatch.setenv("VIVARIUM_WORKBENCH_ALLOWED_HOSTS", "workbench.internal")
    c = _client(tmp_path)
    assert c.get("/health", headers={"Host": "workbench.internal:8000"}).status_code == 200
    assert c.get("/health", headers={"Host": "other.internal:8000"}).status_code == 400


def test_in_process_client_unaffected_when_bind_unknown(tmp_path):
    """TestClient's default Host is ``testserver``; with no recorded bind the
    guard is inactive, so in-process consumers keep working."""
    c = _client(tmp_path)
    assert c.get("/health").status_code == 200


def test_websocket_scope_is_closed_with_policy_violation():
    server_runtime.configure(bind_host="127.0.0.1")
    reached = []

    async def inner(scope, receive, send):  # pragma: no cover - must not be reached
        reached.append(scope)

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "websocket.connect"}

    mw = host_guard.HostGuardMiddleware(inner)
    scope = {"type": "websocket", "headers": [(b"host", b"evil.example:8000")]}
    asyncio.run(mw(scope, receive, send))
    assert reached == []
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_lifespan_scope_passes_through():
    server_runtime.configure(bind_host="127.0.0.1")
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["type"])

    async def noop(*_a):
        return {}

    asyncio.run(host_guard.HostGuardMiddleware(inner)({"type": "lifespan"}, noop, noop))
    assert reached == ["lifespan"]


# ---------------------------------------------------------------------------
# The real `serve` process (uvicorn, loopback bind recorded by serve_fastapi)
# ---------------------------------------------------------------------------

def _raw_get(base_url: str, path: str, host: str) -> int:
    netloc = base_url.split("//", 1)[1]
    hostname, port = netloc.rsplit(":", 1)
    conn = http.client.HTTPConnection(hostname, int(port), timeout=15)
    try:
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host)
        conn.endheaders()
        return conn.getresponse().status
    finally:
        conn.close()


def test_served_process_rejects_foreign_host(tmp_path, dashboard_client):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "workspace.yaml").write_text("name: host-guard-ws\n")
    client = dashboard_client(ws)
    port = client.base_url.rsplit(":", 1)[1]
    assert _raw_get(client.base_url, "/health", f"127.0.0.1:{port}") == 200
    assert _raw_get(client.base_url, "/health", f"localhost:{port}") == 200
    assert _raw_get(client.base_url, "/health", f"attacker.example:{port}") == 400
