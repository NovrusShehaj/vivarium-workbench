"""Tests for the retained viva-api client (lib/remote_api_client.py).

The client was split out of sms_api_client per the 2026-09-21 SMS-retirement
plan (Phase 1): the env-worker/relay/task/capabilities/compose plumbing and
its tests are retained; the SMS-specific method tests (run_simulation,
simulator build registry, observables, analyses) retired with their methods —
the shim now raises typed RetiredEndpointError, asserted here, and the
submission-UX contracts moved to the Smoldyn/compose surfaces
(see tests/test_smoldyn_backend.py, tests/test_smoldyn_endpoints.py).

Monkeypatch targets point at ``remote_api_client`` (where urlopen/time now
live); the SmsApiClient aliases here assert the shim's inheritance and its
typed retired-method behavior, which Phase 4 deletes along with the shim.
"""
from __future__ import annotations

import io
import json
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import pytest

from vivarium_workbench.lib.remote_api_client import RemoteApiClient, SmsApiError
from vivarium_workbench.lib.sms_api_client import RetiredEndpointError, SmsApiClient


class _Resp(io.BytesIO):
    status = 200

    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@contextmanager
def _patch_urlopen(monkeypatch, capture, payload, status=200):
    def fake_urlopen(req, timeout=None):
        capture["url"] = req.full_url
        capture["method"] = req.get_method()
        capture["body"] = req.data
        if status != 200:
            from urllib.error import HTTPError

            raise HTTPError(req.full_url, status, "err", {}, io.BytesIO(b"boom"))
        return _Resp(payload, status)

    monkeypatch.setattr("vivarium_workbench.lib.remote_api_client.urlopen", fake_urlopen)
    yield


def test_non_200_raises_on_retained_method(monkeypatch):
    cap = {}
    with _patch_urlopen(monkeypatch, cap, {}, status=404):
        c = RemoteApiClient("http://h:8080")
        with pytest.raises(SmsApiError):
            c.capabilities()


def test_capabilities_gets_correct_url(monkeypatch):
    cap = {}
    with _patch_urlopen(monkeypatch, cap, {"version": "1.0", "capabilities": []}):
        c = RemoteApiClient("http://h:8080")
        out = c.capabilities()
    assert out["capabilities"] == []
    assert cap["method"] == "GET"
    assert cap["url"] == "http://h:8080/core/v1/capabilities"


def _http_error(status, body):
    from urllib.error import HTTPError
    return HTTPError("http://h:8080/x", status, "err", {}, io.BytesIO(body))


def test_http_error_detail_summarises_html_gateway_page():
    """A 502/504 from a proxy is an HTML page, not JSON — summarise it to its
    <title> instead of echoing the whole markup into the user's alert/logs."""
    from vivarium_workbench.lib.remote_api_client import _http_error_detail
    html = (b"<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n"
            b"<body>\r\n<center><h1>502 Bad Gateway</h1></center>\r\n</body>\r\n</html>")
    out = _http_error_detail(_http_error(502, html))
    assert out == ": 502 Bad Gateway"
    assert "<html" not in out and "<h1>" not in out


def test_http_error_detail_keeps_json_detail():
    """A FastAPI {"detail": ...} body still surfaces the useful message."""
    from vivarium_workbench.lib.remote_api_client import _http_error_detail
    out = _http_error_detail(_http_error(422, b'{"detail": "missing field foo"}'))
    assert out == ": missing field foo"


def test_http_error_detail_caps_length():
    from vivarium_workbench.lib.remote_api_client import _http_error_detail
    out = _http_error_detail(_http_error(500, b"x" * 5000))
    assert len(out) <= 210 and out.endswith("…")


def test_non_200_surfaces_server_error_body(monkeypatch):
    """CD2 pipeline audit §3.12: a FastAPI 422/500's JSON ``detail`` must reach
    the raised SmsApiError, not just the bare status code."""
    cap = {}

    def fake_urlopen(req, timeout=None):
        from urllib.error import HTTPError

        cap["url"] = req.full_url
        body = json.dumps({"detail": "num_generations must be >= 1"}).encode()
        raise HTTPError(req.full_url, 422, "Unprocessable Entity", {}, io.BytesIO(body))

    monkeypatch.setattr("vivarium_workbench.lib.remote_api_client.urlopen", fake_urlopen)
    c = RemoteApiClient("http://h:8080")
    with pytest.raises(SmsApiError) as exc_info:
        c.capabilities()
    assert "422" in str(exc_info.value)
    assert "num_generations must be >= 1" in str(exc_info.value)
    assert exc_info.value.status == 422


def test_post_error_surfaces_server_error_body(monkeypatch):
    def fake_urlopen(req, timeout=None):
        from urllib.error import HTTPError

        body = json.dumps({"detail": {"loc": ["body", "commit"], "msg": "field required"}}).encode()
        raise HTTPError(req.full_url, 422, "Unprocessable Entity", {}, io.BytesIO(body))

    monkeypatch.setattr("vivarium_workbench.lib.remote_api_client.urlopen", fake_urlopen)
    c = RemoteApiClient("http://h:8080")
    with pytest.raises(SmsApiError) as exc_info:
        c._post("/core/v1/capabilities")
    assert "field required" in str(exc_info.value)


def test_error_body_read_failure_does_not_mask_original_error(monkeypatch):
    """A body that can't be read/decoded must not prevent the original error
    from being raised (§3.12 fix must be strictly additive)."""

    def fake_urlopen(req, timeout=None):
        from urllib.error import HTTPError

        class _BrokenBody:
            def read(self):
                raise OSError("body already consumed")

        e = HTTPError(req.full_url, 500, "err", {}, None)
        e.fp = _BrokenBody()
        raise e

    monkeypatch.setattr("vivarium_workbench.lib.remote_api_client.urlopen", fake_urlopen)
    c = RemoteApiClient("http://h:8080")
    with pytest.raises(SmsApiError) as exc_info:
        c._get("/x")
    assert "500" in str(exc_info.value)


def test_get_retries_on_5xx_then_succeeds(monkeypatch):
    """GET is idempotent -- a transient 5xx should be retried, not raised
    immediately."""
    from urllib.error import HTTPError

    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b""))
        return _Resp({"capabilities": []})

    class _FakeTime:
        """Stand-in for the ``time`` module, scoped to this module's own
        name binding rather than the real stdlib module (see the original
        sms_api_client test for the process-wide-patch hazard this avoids)."""

        def sleep(self, s):
            sleeps.append(s)

    monkeypatch.setattr("vivarium_workbench.lib.remote_api_client.urlopen", fake_urlopen)
    monkeypatch.setattr("vivarium_workbench.lib.remote_api_client.time", _FakeTime())
    c = RemoteApiClient("http://h:8080")
    out = c.capabilities()
    assert out == {"capabilities": []}
    assert calls["n"] == 3
    assert len(sleeps) == 2  # two retries before success


def test_get_gives_up_after_max_retries(monkeypatch):
    """After the retry budget is exhausted, the last error is raised -- it
    must not retry forever."""
    from urllib.error import HTTPError

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b'{"detail": "db down"}'))

    class _FakeTime:
        def sleep(self, s):
            pass

    monkeypatch.setattr("vivarium_workbench.lib.remote_api_client.urlopen", fake_urlopen)
    monkeypatch.setattr("vivarium_workbench.lib.remote_api_client.time", _FakeTime())
    c = RemoteApiClient("http://h:8080")
    with pytest.raises(SmsApiError) as exc_info:
        c.capabilities()
    assert calls["n"] == 3  # default retry budget, not unbounded
    assert "db down" in str(exc_info.value)


def test_get_does_not_retry_on_4xx(monkeypatch):
    """A 4xx is a client error, not transient -- retrying it would just waste
    time and could not possibly succeed."""
    from urllib.error import HTTPError

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))

    monkeypatch.setattr("vivarium_workbench.lib.remote_api_client.urlopen", fake_urlopen)
    c = RemoteApiClient("http://h:8080")
    with pytest.raises(SmsApiError):
        c.capabilities()
    assert calls["n"] == 1  # no retry


def test_post_is_never_retried_on_5xx(monkeypatch):
    """POST (submit/dispatch) must NOT be auto-retried -- a retried submit
    could double-run a simulation."""
    from urllib.error import HTTPError

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b""))

    monkeypatch.setattr("vivarium_workbench.lib.remote_api_client.urlopen", fake_urlopen)
    c = RemoteApiClient("http://h:8080")
    with pytest.raises(SmsApiError):
        c._post("/compose/v1/simulation/run")
    assert calls["n"] == 1  # single attempt, no retry


def test_env_worker_task_submit_posts_body(monkeypatch):
    cap = {}
    with _patch_urlopen(monkeypatch, cap, {"task_id": 12, "status": "queued"}):
        c = RemoteApiClient("http://h:8080")
        out = c.submit_env_worker_task("job-1", method="run_study", params={"a": 1})
    assert out["task_id"] == 12
    assert cap["method"] == "POST"
    assert cap["url"] == "http://h:8080/env-worker/v1/tasks"
    body = json.loads(cap["body"].decode())
    assert body == {"job_name": "job-1", "method": "run_study", "params": {"a": 1}}


def test_env_worker_task_get_and_cancel(monkeypatch):
    cap = {}
    with _patch_urlopen(monkeypatch, cap, {"task_id": 12, "status": "running"}):
        c = RemoteApiClient("http://h:8080")
        out = c.get_env_worker_task(12)
    assert out["status"] == "running"
    assert cap["url"] == "http://h:8080/env-worker/v1/tasks/12"

    cap2 = {}
    with _patch_urlopen(monkeypatch, cap2, {}):
        c = RemoteApiClient("http://h:8080")
        c.cancel_env_worker_task(12)
    assert cap2["method"] == "DELETE"
    assert cap2["url"] == "http://h:8080/env-worker/v1/tasks/12"


def test_compose_status_batch_widens_list(monkeypatch):
    cap = {}
    with _patch_urlopen(monkeypatch, cap, [{"simulation_database_id": 1},
                                           {"simulation_database_id": 2},
                                           "junk"]):
        c = RemoteApiClient("http://h:8080")
        out = c.compose_status_batch([1, 2])
    assert out == [{"simulation_database_id": 1}, {"simulation_database_id": 2}]
    qs = parse_qs(urlsplit(cap["url"]).query)
    assert qs["ids"] == ["1", "2"]


def test_compose_status_batch_empty_is_local_noop():
    assert RemoteApiClient("http://h:8080").compose_status_batch([]) == []


# ---------------------------------------------------------------------------
# The shim: inheritance + typed retired-method behavior (deleted in Phase 4).
# ---------------------------------------------------------------------------


def test_shim_is_the_retained_client():
    assert issubclass(SmsApiClient, RemoteApiClient)


def test_shim_reexports_shared_names():
    from vivarium_workbench.lib import sms_api_client as sac
    from vivarium_workbench.lib import remote_api_client as rac

    assert sac.SmsApiError is rac.SmsApiError
    assert sac.DOWNLOAD_TIMEOUT is rac.DOWNLOAD_TIMEOUT
    assert sac._http_error_detail is rac._http_error_detail
    assert callable(sac.sms_api_base)


_RETIRED_METHOD_CASES = {
    "latest_simulator": ("r", "b"),
    "register_simulator": ("r", "b", "c"),
    "upload_simulator": ({},),
    "simulator_status": (1,),
    "list_simulators": (),
    "composite_resolve": (1, "ref"),
    "download_workspace": (1, "/tmp"),
    "list_build_simulations": (1,),
    "simulation_status": (1,),
    "get_simulation": (1,),
    "simulator_commit": (1,),
    "simulation_chain_progress": (1,),
    "observables": (1, ["n"]),
    "run_simulation": (),
    "download_data": (1, "/tmp"),
    "run_analysis": (1, {}),
    "analysis_status": (1,),
    "list_analyses": (1,),
}


@pytest.mark.parametrize("method,args", sorted(_RETIRED_METHOD_CASES.items()))
def test_retired_methods_raise_typed_error(method, args):
    """Every retired SMS method fails fast, client-side, with a typed error —
    no HTTP round-trip, no confusing 404 from a server without the route."""
    c = SmsApiClient("http://127.0.0.1:1")  # nothing listening
    with pytest.raises(RetiredEndpointError) as exc:
        getattr(c, method)(*args)
    assert "retired SMS endpoint" in str(exc.value)
    assert exc.value.status is None  # nothing was contacted


def test_retired_error_names_the_plan():
    c = SmsApiClient("http://127.0.0.1:1")
    with pytest.raises(RetiredEndpointError) as exc:
        c.run_simulation()
    assert "2026-09-21" in str(exc.value)
