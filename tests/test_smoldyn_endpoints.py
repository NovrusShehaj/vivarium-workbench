"""Phase 3 tests: the /api/smoldyn-run and /api/smoldyn-status endpoints.

Exercises the real server (``dashboard_client`` subprocess, per the repo's
standing no-mock-the-layer-under-test rule) against a stdlib stand-in
viva-smoldyn service, over a real workspace holding a Smoldyn composite that
mirrors the packaged viva-smoldyn composite dialect.

Covers the UX contracts this route owns after the SMS retirement (plan §6
Phase 3): fast failure with named pre-flight violations, honest reachability
mapping (502 ``reachable:false`` — backlog item 51's contract, moved here
from the retired SMS submit), pass-through of the service's error statuses,
and hidden-backend 503 when ``SMOLDYN_API_BASE`` is unset.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Real local stand-in for the genuinely-external viva-smoldyn boundary.
# ---------------------------------------------------------------------------


class _FakeSmoldynHandler(BaseHTTPRequestHandler):
    run_status: int = 200
    run_body: dict = {"samples": [{"time": 0.0, "molecule_counts": {"A": 2}}]}
    captured: list[dict] = []

    def do_POST(self):  # noqa: N802 (stdlib-mandated method name)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/smoldyn/v1/simulations":
            type(self).captured.append(json.loads(body))
            payload = type(self).run_body
            if type(self).run_status >= 400:
                payload = {"detail": {"code": "resource_limit", "message": "rejected"}}
            out = json.dumps(payload).encode()
            self.send_response(type(self).run_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path == "/":
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):  # noqa: A002
        pass


@pytest.fixture
def fake_smoldyn_env(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _FakeSmoldynHandler)
    _FakeSmoldynHandler.run_status = 200
    _FakeSmoldynHandler.captured = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("SMOLDYN_API_BASE", base)
    yield base
    monkeypatch.delenv("SMOLDYN_API_BASE", raising=False)
    server.shutdown()


# ---------------------------------------------------------------------------
# Minimal workspace holding a Smoldyn composite (real dialect).
# ---------------------------------------------------------------------------


_COMPOSITE_YAML = {
    "name": "demo-diffusion",
    "description": "demo",
    "requires": {"processes": ["SmoldynProcess", "RAMEmitter"]},
    "parameters": {
        "interval": {"type": "float", "default": 5.0},
        "dt": {"type": "float", "default": 0.01},
        "count_a": {"type": "int", "default": 200},
        "count_b": {"type": "int", "default": 0},
        "rate_ab": {"type": "float", "default": 0.01},
    },
    "state": {
        "smoldyn": {
            "_type": "process",
            "address": "local:SmoldynProcess",
            "config": {
                "dimensions": 2,
                "bounds": [[0, 100], [0, 100]],
                "boundary_type": "r",
                "dt": "${dt}",
                "seed": 12345,
                "species": {
                    "A": {"difc": 1.0, "count": "${count_a}", "color": "red",
                          "display_size": 3},
                    "B": {"difc": 0.3, "count": "${count_b}", "color": "blue",
                          "display_size": 3},
                },
                "reactions": [{"name": "convert", "subs": ["A"], "prds": ["B"],
                               "rate": "${rate_ab}"}],
            },
            "inputs": {},
            "outputs": {"molecule_counts": ["stores", "molecule_counts"]},
        }
    },
}


@pytest.fixture
def smoldyn_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "workspace.yaml").write_text("name: demo-ws\n")
    comp_dir = ws / "pbg_demo_ws" / "composites"
    comp_dir.mkdir(parents=True)
    (comp_dir / "demo-diffusion.composite.yaml").write_text(
        yaml.safe_dump(_COMPOSITE_YAML)
    )
    sd = ws / "studies" / "demo"
    sd.mkdir(parents=True)
    (sd / "study.yaml").write_text(yaml.safe_dump({
        "name": "demo",
        "baseline": [{"name": "core", "composite": "pbg_demo_ws.composites.demo-diffusion",
                      "params": {}}],
    }))
    return ws


def _make_client(dashboard_client, ws: Path):
    return dashboard_client(workspace=ws)


# ---------------------------------------------------------------------------
# Endpoint tests.
# ---------------------------------------------------------------------------


class TestSmoldynRunEndpoint:
    def test_run_returns_samples_over_real_http(self, dashboard_client,
                                                smoldyn_workspace,
                                                fake_smoldyn_env):
        client = _make_client(dashboard_client, smoldyn_workspace)
        res = client.post("/api/smoldyn-run", json={
            "study": "demo",
            "composite": "pbg_demo_ws.composites.demo-diffusion",
            "duration": 20.0,
        })
        assert res.status_code == 200, res.text
        samples = res.json()["samples"]
        assert samples[0]["molecule_counts"] == {"A": 2}
        # The composite's declared parameters were substituted in.
        sent = _FakeSmoldynHandler.captured[-1]
        assert sent["species"]["A"]["count"] == 200
        assert sent["reactions"][0]["rate"] == 0.01
        assert sent["dt"] == 0.01

    def test_preflight_violation_is_422_with_named_constraints(
            self, dashboard_client, smoldyn_workspace, fake_smoldyn_env):
        client = _make_client(dashboard_client, smoldyn_workspace)
        # duration=1000 at dt=0.01 → 100k steps > MAX_STEPS.
        res = client.post("/api/smoldyn-run", json={
            "study": "demo",
            "composite": "pbg_demo_ws.composites.demo-diffusion",
            "duration": 1000.0,
        })
        assert res.status_code == 422, res.text
        body = res.json()
        assert "MAX_STEPS" in body["error"] + str(body.get("violations"))

    def test_unreachable_service_is_502_reachable_false(self, dashboard_client,
                                                        smoldyn_workspace,
                                                        monkeypatch):
        # Point at a port with no listener: connection-level failure.
        monkeypatch.setenv("SMOLDYN_API_BASE", "http://127.0.0.1:1")
        client = _make_client(dashboard_client, smoldyn_workspace)
        res = client.post("/api/smoldyn-run", json={
            "study": "demo",
            "composite": "pbg_demo_ws.composites.demo-diffusion",
            "duration": 10.0,
        })
        assert res.status_code == 502, res.text
        assert res.json()["reachable"] is False

    def test_service_error_status_passes_through(self, dashboard_client,
                                                 smoldyn_workspace,
                                                 fake_smoldyn_env):
        _FakeSmoldynHandler.run_status = 503
        client = _make_client(dashboard_client, smoldyn_workspace)
        res = client.post("/api/smoldyn-run", json={
            "study": "demo",
            "composite": "pbg_demo_ws.composites.demo-diffusion",
            "duration": 10.0,
        })
        assert res.status_code == 503, res.text

    def test_hidden_backend_is_503(self, dashboard_client, smoldyn_workspace,
                                   monkeypatch):
        monkeypatch.delenv("SMOLDYN_API_BASE", raising=False)
        client = _make_client(dashboard_client, smoldyn_workspace)
        res = client.post("/api/smoldyn-run", json={
            "study": "demo",
            "composite": "pbg_demo_ws.composites.demo-diffusion",
            "duration": 10.0,
        })
        assert res.status_code == 503, res.text
        assert res.json()["smoldyn_configured"] is False

    def test_validation_errors(self, dashboard_client, smoldyn_workspace,
                               fake_smoldyn_env):
        client = _make_client(dashboard_client, smoldyn_workspace)
        for body, expected in (
            ({"composite": "x", "duration": 1.0}, 400),   # missing study
            ({"study": "demo", "duration": 1.0}, 400),    # missing composite
            ({"study": "demo", "composite": "x"}, 400),   # missing duration
            ({"study": "demo", "composite": "x", "duration": -1}, 400),
            ({"study": "nope", "composite": "x", "duration": 1.0}, 404),
            ({"study": "demo", "composite": "pbg_demo_ws.composites.missing",
              "duration": 1.0}, 404),
        ):
            res = client.post("/api/smoldyn-run", json=body)
            assert res.status_code == expected, (body, res.status_code, res.text)


class TestSmoldynUI:
    def test_study_page_carries_quickrun_host_and_script(self, dashboard_client,
                                                          smoldyn_workspace):
        """The study page mounts the Phase 3 quick-run host div + script.
        The script itself self-removes when the backend is unconfigured."""
        client = _make_client(dashboard_client, smoldyn_workspace)
        res = client.get("/studies/demo")
        assert res.status_code == 200, res.text[:200]
        html = res.text
        assert 'id="smoldyn-quickrun"' in html
        assert 'data-study="demo"' in html
        # The page builder prefixes asset srcs with /assets/.
        assert "smoldyn-quickrun.js" in html

    def test_quickrun_script_served(self, dashboard_client, smoldyn_workspace):
        client = _make_client(dashboard_client, smoldyn_workspace)
        res = client.get("/smoldyn-quickrun.js")
        assert res.status_code == 200
        assert "smoldyn-run" in res.text


class TestSmoldynStatusEndpoint:
    def test_unconfigured_reports_hidden(self, dashboard_client,
                                         smoldyn_workspace, monkeypatch):
        monkeypatch.delenv("SMOLDYN_API_BASE", raising=False)
        client = _make_client(dashboard_client, smoldyn_workspace)
        res = client.get("/api/smoldyn-status")
        assert res.status_code == 200
        assert res.json() == {"configured": False, "reachable": False}

    def test_configured_and_reachable(self, dashboard_client,
                                      smoldyn_workspace, fake_smoldyn_env):
        client = _make_client(dashboard_client, smoldyn_workspace)
        res = client.get("/api/smoldyn-status")
        assert res.status_code == 200
        assert res.json()["configured"] is True
        assert res.json()["reachable"] is True
