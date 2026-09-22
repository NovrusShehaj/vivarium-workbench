"""Phase 2 tests: the viva-smoldyn backend (client + composite adapter).

Covers the two new modules from the SMS-retirement plan
(docs/superpowers/plans/2026-09-21-workbench-sms-retirement-and-smoldyn-backend-plan.md):

* ``lib.smoldyn_api_client`` — real HTTP against a stdlib stand-in server
  (the repo's standing convention: only the genuinely-external boundary is
  faked, never the layer under test): success, 422/413/503/504 mapping,
  ``detail.code`` extraction, unreachable service, and the hidden-backend
  behavior when ``SMOLDYN_API_BASE`` is unset.
* ``lib.smoldyn_composite_adapter`` — extraction of the Smoldyn config from a
  resolved composite payload, ``${param}`` substitution, request building, and
  pre-flight validation that names each violated constraint (unimolecular
  reactions, species/reaction caps, particle/sample/step limits, integer
  multiples of dt, colors, names, bounds, body size).

The adapter fixtures mirror the real packaged viva-smoldyn composite dialect
(``viva_smoldyn/composites/*.composite.yaml``): a ``smoldyn`` process node
whose config carries species/reactions/bounds/dt with ``${param}``
placeholders against declared parameters.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vivarium_workbench.lib.smoldyn_api_client import (
    SmoldynApiClient,
    SmoldynApiError,
    smoldyn_api_base,
)
from vivarium_workbench.lib.smoldyn_composite_adapter import (
    MAX_PARTICLES,
    build_request,
    extract_smoldyn_config,
    preflight,
)


# ---------------------------------------------------------------------------
# Stand-in viva-smoldyn service (stdlib only).
# ---------------------------------------------------------------------------


class _FakeSmoldynHandler(BaseHTTPRequestHandler):
    run_status: int = 200
    run_body: dict = {"samples": [{"time": 0.0, "molecule_counts": {"A": 2}}]}
    error_code: str | None = "resource_limit"
    captured: list[dict] = []

    def do_POST(self):  # noqa: N802 (stdlib-mandated method name)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/smoldyn/v1/simulations":
            type(self).captured.append(json.loads(body))
            payload = type(self).run_body
            if type(self).run_status >= 400:
                payload = {"detail": {"code": type(self).error_code,
                                      "message": "rejected"}}
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
def fake_smoldyn():
    server = HTTPServer(("127.0.0.1", 0), _FakeSmoldynHandler)
    _FakeSmoldynHandler.run_status = 200
    _FakeSmoldynHandler.run_body = {"samples": [{"time": 0.0, "molecule_counts": {"A": 2}}]}
    _FakeSmoldynHandler.error_code = "resource_limit"
    _FakeSmoldynHandler.captured = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# ---------------------------------------------------------------------------
# Client.
# ---------------------------------------------------------------------------


class TestSmoldynClient:
    def test_run_success_returns_samples(self, fake_smoldyn):
        client = SmoldynApiClient(base_url=fake_smoldyn, timeout=5)
        payload = {"duration": 0.03, "dt": 0.01, "species": {"A": {"count": 2}}}
        out = client.run_simulation(payload)
        assert out["samples"][0]["molecule_counts"] == {"A": 2}
        assert _FakeSmoldynHandler.captured[-1] == payload

    def test_error_mapping_carries_code(self, fake_smoldyn):
        _FakeSmoldynHandler.run_status = 413
        client = SmoldynApiClient(base_url=fake_smoldyn, timeout=5)
        with pytest.raises(SmoldynApiError) as exc:
            client.run_simulation({"duration": 1, "species": {"A": {"count": 1}}})
        assert exc.value.status == 413
        assert exc.value.code == "resource_limit"

    def test_timeout_maps_to_504_detail(self, fake_smoldyn):
        _FakeSmoldynHandler.run_status = 504
        _FakeSmoldynHandler.error_code = "wall_timeout"
        client = SmoldynApiClient(base_url=fake_smoldyn, timeout=5)
        with pytest.raises(SmoldynApiError) as exc:
            client.run_simulation({"duration": 1, "species": {"A": {"count": 1}}})
        assert exc.value.status == 504
        assert exc.value.code == "wall_timeout"

    def test_capacity_503(self, fake_smoldyn):
        _FakeSmoldynHandler.run_status = 503
        client = SmoldynApiClient(base_url=fake_smoldyn, timeout=5)
        with pytest.raises(SmoldynApiError) as exc:
            client.run_simulation({"duration": 1, "species": {"A": {"count": 1}}})
        assert exc.value.status == 503

    def test_validation_422(self, fake_smoldyn):
        _FakeSmoldynHandler.run_status = 422
        _FakeSmoldynHandler.error_code = None
        client = SmoldynApiClient(base_url=fake_smoldyn, timeout=5)
        with pytest.raises(SmoldynApiError) as exc:
            client.run_simulation({"duration": 1})
        assert exc.value.status == 422

    def test_unreachable_service(self):
        client = SmoldynApiClient(base_url="http://127.0.0.1:1", timeout=1)
        with pytest.raises(SmoldynApiError) as exc:
            client.run_simulation({"duration": 1, "species": {"A": {"count": 1}}})
        assert exc.value.status is None

    def test_unexpected_response_shape(self, fake_smoldyn):
        _FakeSmoldynHandler.run_body = {"unexpected": True}
        client = SmoldynApiClient(base_url=fake_smoldyn, timeout=5)
        with pytest.raises(SmoldynApiError):
            client.run_simulation({"duration": 1, "species": {"A": {"count": 1}}})

    def test_hidden_backend_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("SMOLDYN_API_BASE", raising=False)
        assert smoldyn_api_base() is None
        with pytest.raises(SmoldynApiError):
            SmoldynApiClient()

    def test_env_configures_base(self, monkeypatch):
        monkeypatch.setenv("SMOLDYN_API_BASE", "http://smoldyn.internal:8001/")
        assert smoldyn_api_base() == "http://smoldyn.internal:8001"
        client = SmoldynApiClient()
        assert client.base_url == "http://smoldyn.internal:8001"

    def test_call_class_policy(self):
        assert SmoldynApiClient.for_("run", base_url="http://x").timeout == 30.0
        assert SmoldynApiClient.for_("probe", base_url="http://x").timeout == 3.0
        with pytest.raises(ValueError):
            SmoldynApiClient.for_("nope", base_url="http://x")

    def test_probe_reachability(self, fake_smoldyn):
        client = SmoldynApiClient(base_url=fake_smoldyn)
        assert client.probe() == {"reachable": True}


# ---------------------------------------------------------------------------
# Composite adapter fixtures (real viva-smoldyn composite dialect).
# ---------------------------------------------------------------------------


def _resolved_diffusion_reaction(**param_overrides) -> dict:
    """Mirror of the packaged diffusion-reaction-demo composite, resolved."""
    params = {"interval": 5.0, "dt": 0.01, "count_a": 200, "count_b": 0, "rate_ab": 0.01}
    params.update(param_overrides)
    return {
        "id": "diffusion-reaction-demo",
        "parameters": {name: {"type": "float" if name in ("interval", "dt", "rate_ab") else "int",
                              "default": value}
                       for name, value in params.items()},
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
            }
        },
    }


class TestExtract:
    def test_extracts_single_smoldyn_config(self):
        cfg, _node_interval, reason = extract_smoldyn_config(_resolved_diffusion_reaction())
        assert reason is None
        assert cfg["species"]["A"]["count"] == "${count_a}"

    def test_rejects_payload_without_state(self):
        cfg, _node_interval, reason = extract_smoldyn_config({"id": "x"})
        assert cfg is None and "no state wiring" in reason

    def test_rejects_payload_without_smoldyn(self):
        payload = {"state": {"other": {"address": "local:SomethingElse", "config": {}}}}
        cfg, _node_interval, reason = extract_smoldyn_config(payload)
        assert cfg is None and "no SmoldynProcess" in reason

    def test_rejects_multi_smoldyn(self):
        payload = _resolved_diffusion_reaction()
        payload["state"]["smoldyn2"] = payload["state"]["smoldyn"]
        cfg, _node_interval, reason = extract_smoldyn_config(payload)
        assert cfg is None and "2 SmoldynProcess" in reason

    def test_node_level_interval_returned(self):
        # Packaged composites (e.g. three-species-crowding) declare the step
        # interval as a SIBLING of config, not inside it — extract must carry it.
        payload = _resolved_diffusion_reaction()
        payload["state"]["smoldyn"]["interval"] = 0.5
        cfg, node_interval, reason = extract_smoldyn_config(payload)
        assert reason is None and node_interval == 0.5 and cfg is not None


class TestBuildRequest:
    def test_node_level_interval_wins_over_param_fallback(self):
        # The packaged-composite shape: interval at the state-node level beats
        # the declared-parameter fallback, but loses to an explicit arg.
        payload = _resolved_diffusion_reaction()
        payload["state"]["smoldyn"]["interval"] = 0.5
        request = build_request(payload, duration=10.0)
        assert request["interval"] == 0.5
        assert preflight(request) == []
        assert build_request(payload, duration=10.0, interval=2.0)["interval"] == 2.0
    def test_substitutes_params_and_builds(self):
        request = build_request(_resolved_diffusion_reaction(), duration=20.0)
        assert request["duration"] == 20.0
        assert request["dt"] == 0.01
        assert request["interval"] == 5.0
        assert request["seed"] == 12345
        assert request["species"]["A"] == {"count": 200, "difc": 1.0, "color": "red",
                                           "display_size": 3.0}
        assert request["species"]["B"]["count"] == 0
        assert request["reactions"] == [{"name": "convert", "subs": ["A"], "prds": ["B"],
                                         "rate": 0.01}]
        assert preflight(request) == []

    def test_overrides_win_over_defaults(self):
        request = build_request(_resolved_diffusion_reaction(count_a=7), duration=10.0)
        assert request["species"]["A"]["count"] == 7

    def test_explicit_args_win_over_composite(self):
        request = build_request(_resolved_diffusion_reaction(), duration=10.0,
                                dt=0.5, interval=2.0, seed=7)
        assert request["dt"] == 0.5 and request["interval"] == 2.0 and request["seed"] == 7

    def test_undeclared_placeholder_raises_named_error(self):
        payload = _resolved_diffusion_reaction()
        del payload["parameters"]["rate_ab"]
        with pytest.raises(ValueError, match="rate_ab"):
            build_request(payload, duration=10.0)

    def test_non_numeric_parameter_raises_named_error(self):
        with pytest.raises(ValueError, match="species A.count.*'many'"):
            build_request(_resolved_diffusion_reaction(count_a="many"), duration=10.0)


class TestPreflight:
    def test_clean_request_passes(self):
        request = build_request(_resolved_diffusion_reaction(), duration=20.0)
        assert preflight(request) == []

    def test_bimolecular_reaction_named(self):
        request = build_request(_resolved_diffusion_reaction(), duration=20.0)
        request["reactions"].append({"name": "dimer", "subs": ["A", "B"],
                                     "prds": ["A"], "rate": 0.1})
        problems = preflight(request)
        assert any("not unimolecular" in p for p in problems)

    def test_species_cap_named(self):
        payload = _resolved_diffusion_reaction()
        payload["state"]["smoldyn"]["config"]["species"] = {
            f"S{i}": {"count": 1} for i in range(65)
        }
        request = build_request(payload, duration=10.0)
        problems = preflight(request)
        assert any("64" in p and "species" in p for p in problems)

    def test_particle_cap_named(self):
        payload = _resolved_diffusion_reaction(count_a=MAX_PARTICLES, count_b=1)
        request = build_request(payload, duration=10.0)
        problems = preflight(request)
        assert any("MAX_PARTICLES" in p for p in problems)

    def test_step_cap_named(self):
        payload = _resolved_diffusion_reaction(dt=0.01)
        request = build_request(payload, duration=1000.0)  # 100k steps
        problems = preflight(request)
        assert any("MAX_STEPS" in p for p in problems)

    def test_sample_cap_named(self):
        payload = _resolved_diffusion_reaction(dt=0.01, interval=0.01)  # sample every step
        request = build_request(payload, duration=10.0)
        problems = preflight(request)
        assert any("MAX_SAMPLES" in p for p in problems)

    def test_non_multiple_of_dt_named(self):
        request = build_request(_resolved_diffusion_reaction(), duration=10.0)
        request["duration"] = 10.0005
        problems = preflight(request)
        assert any("integer multiple of dt" in p for p in problems)

    def test_bad_color_named(self):
        request = build_request(_resolved_diffusion_reaction(), duration=10.0)
        request["species"]["A"]["color"] = "chartreuse"
        problems = preflight(request)
        assert any("chartreuse" in p for p in problems)

    def test_undefined_species_ref_named(self):
        request = build_request(_resolved_diffusion_reaction(), duration=10.0)
        request["reactions"].append({"name": "bad", "subs": ["Z"], "prds": ["A"],
                                     "rate": 1.0})
        problems = preflight(request)
        assert any("undefined species" in p and "Z" in p for p in problems)

    def test_bad_bounds_named(self):
        request = build_request(_resolved_diffusion_reaction(), duration=10.0)
        request["bounds"] = [[10, 0], [0, 100]]
        problems = preflight(request)
        assert any("low < high" in p for p in problems)

    def test_bad_seed_named(self):
        request = build_request(_resolved_diffusion_reaction(), duration=10.0)
        request["seed"] = 2**40
        problems = preflight(request)
        assert any("seed" in p for p in problems)
