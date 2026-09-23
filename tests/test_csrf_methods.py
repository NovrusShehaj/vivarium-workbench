"""The same-origin guard covers EVERY unsafe HTTP method, not just POST/DELETE.

Before this change the FastAPI middleware checked only ``POST``/``DELETE``, so
the ``PATCH`` routes (and any future ``PUT``) accepted cross-origin requests.
``lib.csrf.is_unsafe_method`` now defines the guarded set as "anything but
GET/HEAD/OPTIONS/TRACE".
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vivarium_workbench.api.app import create_app, get_workspace
from vivarium_workbench.lib import csrf, server_runtime

_PROBE = "/api/__csrf_method_probe__"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("VIVARIUM_WORKBENCH_DISABLE_CSRF", raising=False)
    monkeypatch.delenv("VIVARIUM_DASHBOARD_DISABLE_CSRF", raising=False)
    monkeypatch.delenv("VIVARIUM_WORKBENCH_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("VIVARIUM_WORKBENCH_TRUST_PROXY", raising=False)
    server_runtime.reset()
    app = create_app()
    app.dependency_overrides[get_workspace] = lambda: tmp_path
    return TestClient(app)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "PROPFIND"])
def test_cross_origin_unsafe_methods_are_forbidden(client, method):
    r = client.request(method, _PROBE, headers={"Origin": "http://evil.example"}, content=b"{}")
    assert r.status_code == 403
    assert r.json() == {"error": "cross-origin request forbidden"}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_same_origin_unsafe_methods_reach_routing(client, method):
    # TestClient's Host is "testserver"; a matching Origin passes the guard and
    # the bogus path then 404/405s (side-effect free).
    r = client.request(method, _PROBE, headers={"Origin": "http://testserver"}, content=b"{}")
    assert r.status_code in (404, 405)


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_originless_unsafe_methods_still_allowed_for_cli(client, method):
    r = client.request(method, _PROBE, content=b"{}")
    assert r.status_code in (404, 405)


def test_safe_methods_are_never_blocked(client):
    for method in ("GET", "HEAD", "OPTIONS"):
        r = client.request(method, "/health", headers={"Origin": "http://evil.example"})
        assert r.status_code != 403, method


def test_real_patch_route_is_guarded(client):
    """A route that exists and mutates (PATCH) must be refused cross-origin."""
    paths = [getattr(rt, "path", "") for rt in client.app.routes
             if "PATCH" in (getattr(rt, "methods", None) or set())]
    assert paths, "expected at least one PATCH route in the app"
    path = paths[0].replace("{", "").replace("}", "")
    r = client.patch(path, json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("method,unsafe", [
    ("GET", False), ("head", False), ("OPTIONS", False), ("TRACE", False),
    ("POST", True), ("put", True), ("PATCH", True), ("DELETE", True), ("MKCOL", True),
    ("", True),
])
def test_is_unsafe_method(method, unsafe):
    assert csrf.is_unsafe_method(method) is unsafe


def test_browser_only_variant_requires_origin():
    assert csrf.is_browser_request_allowed(None, "127.0.0.1:8000", disabled=False) is False
    assert csrf.is_browser_request_allowed("", "127.0.0.1:8000", disabled=False) is False
    assert csrf.is_browser_request_allowed(
        "http://127.0.0.1:8000", "127.0.0.1:8000", disabled=False) is True
    assert csrf.is_browser_request_allowed(
        "http://evil.example", "127.0.0.1:8000", disabled=False) is False
    # The documented bypass still applies (tests / trusted tooling).
    assert csrf.is_browser_request_allowed(None, "127.0.0.1:8000", disabled=True) is True
