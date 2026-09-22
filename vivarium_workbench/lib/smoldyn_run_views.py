"""Quick-run views for the remote Smoldyn backend (Phase 3).

The viva-smoldyn service exposes one operation — a bounded, synchronous
simulation (``POST /smoldyn/v1/simulations``) — so its workbench surface is
deliberately small: submit a composite-derived request, wait, return the
per-step counts. There is no job ID to poll and no artifact to land; the plan
(§5.2/§7) explicitly forbids faking a durable-job queue on top of a
synchronous endpoint.

Error contract mirrors the retired SMS submit route's load-bearing UX rules
(backlog item 51): a genuinely-external failure surfaces as a clean
``502 {"error": ..., "reachable": false}`` rather than FastAPI's generic
unhandled-exception 500, and pre-flight problems surface as a ``422`` naming
each violated constraint so the user can act (shorten duration, raise dt,
fix a reaction) instead of decoding a server error.

No auth gate, unlike the retired SMS submit: a bounded ≤15 s run on an
operator-configured private-network service is a different cost class from
the cloud campaigns the old gate protected; the endpoint's own operator
limits (MAX_SECONDS/MAX_PARTICLES/MAX_CONCURRENT_RUNS) are the boundary.
"""

from __future__ import annotations

from pathlib import Path

from vivarium_workbench.lib import study_spec
from vivarium_workbench.lib.smoldyn_api_client import (
    SmoldynApiClient,
    SmoldynApiError,
    smoldyn_api_base,
)
from vivarium_workbench.lib.smoldyn_composite_adapter import (
    build_request,
    preflight,
)


def smoldyn_run(ws_root: Path, body: dict) -> tuple[dict, int]:
    """Run a composite remotely on the viva-smoldyn service and return counts.

    Body: ``{study, composite, duration[, interval, dt, seed, overrides]}`` —
    ``study`` names a workspace study (for spec provenance and future
    param plumbing), ``composite`` is the composite id to resolve, and
    ``duration`` is the simulation run length. Returns
    ``({"samples": [...]}, 200)`` on success.
    """
    body = body or {}
    study = (body.get("study") or "").strip()
    composite = (body.get("composite") or "").strip()
    if not study:
        return {"error": "study is required"}, 400
    if not composite:
        return {"error": "composite is required"}, 400
    duration = body.get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) \
            or duration <= 0:
        return {"error": "duration must be a positive number"}, 400

    spec_path = study_spec.study_spec_path(ws_root, study)
    if spec_path is None or not spec_path.is_file():
        return {"error": f"study {study!r} not found"}, 404

    from vivarium_workbench.lib.composite_resolve import resolve_composite_for_request

    resolved = resolve_composite_for_request(ws_root, composite, body.get("overrides") or {})
    if resolved is None:
        return {"error": f"composite not found: {composite}"}, 404
    if resolved.get("error") and not resolved.get("parameters"):
        return {"error": resolved["error"]}, 422

    try:
        request = build_request(
            resolved,
            duration=duration,
            interval=body.get("interval"),
            dt=body.get("dt"),
            seed=body.get("seed"),
            overrides=body.get("overrides") or None,
        )
    except ValueError as e:
        return {"error": str(e)}, 400

    violations = preflight(request)
    if violations:
        return {"error": "request violates the Smoldyn service contract",
                "violations": violations}, 422

    if smoldyn_api_base() is None:
        return {"error": "Smoldyn backend is not configured (SMOLDYN_API_BASE unset)",
                "smoldyn_configured": False}, 503

    try:
        samples = SmoldynApiClient.for_("run").run_simulation(request)
    except SmoldynApiError as e:
        if e.status is None:
            # Connection-level failure: the service is unreachable. Same UX
            # contract as the retired SMS submit path (backlog item 51): a
            # clean, honest 502 — never a generic 500.
            return {"error": str(e), "reachable": False}, 502
        # HTTP failure from the service: pass its status through (413/422/500/
        # 503/504 all carry a safe message and code already).
        return {"error": str(e), **({"code": e.code} if e.code else {})}, e.status
    return samples, 200


def smoldyn_status() -> tuple[dict, int]:
    """Backend health for the UI: configured? reachable?"""
    base = smoldyn_api_base()
    if base is None:
        return {"configured": False, "reachable": False}, 200
    try:
        probe = SmoldynApiClient(base_url=base).probe()
    except SmoldynApiError:
        return {"configured": True, "reachable": False}, 200
    return {"configured": True, "reachable": True, **probe}, 200
