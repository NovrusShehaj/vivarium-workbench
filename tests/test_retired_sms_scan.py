"""Phase 0 anti-regression gate: retired SMS endpoints must not reappear.

The viva-api core separation retired the SMS surface (E. coli/ParCa/PTools).
This gate enforces the workbench half of the plan
(docs/superpowers/plans/2026-09-21-workbench-sms-retirement-and-smoldyn-backend-plan.md,
§8): the retired endpoint path strings must not appear in *executable* remote
code. Phase 4 deletes the remaining files; until then they are pinned in
_RETIRED_FILES_PENDING_REMOVAL so the gate tightens automatically as phases
land (a file removed from the tree drops out of the allowlist; a NEW file
introducing one of these strings fails).
"""
from __future__ import annotations

import re
from pathlib import Path

#: Retired endpoint path fragments (viva-api core separation).
RETIRED_PATTERNS = (
    "/api/v1/simulations",
    "/core/v1/simulator",
    "/analyses/",
)

#: Files that still reference retired endpoints pending the Phase 4 removal.
#: Everything here is scheduled for deletion or rewrite in Phase 4; a file
#: dropping OFF this list (deleted or cleaned) is a tightening, and any OTHER
#: file carrying one of these strings fails the gate.
_RETIRED_FILES_PENDING_REMOVAL = {
    "vivarium_workbench/api/app.py",
    "vivarium_workbench/env_worker.py",
    "vivarium_workbench/lib/composite_resolve.py",
    "vivarium_workbench/lib/composite_runs.py",
    "vivarium_workbench/lib/composite_test_run_views.py",
    "vivarium_workbench/lib/remote_analysis_figures.py",
    "vivarium_workbench/lib/remote_build_source.py",
    "vivarium_workbench/lib/remote_pinned.py",
    "vivarium_workbench/lib/remote_run_landing.py",
    "vivarium_workbench/lib/remote_run_views.py",
    "vivarium_workbench/lib/remote_simulations.py",
    "vivarium_workbench/lib/sms_api_client.py",
    "vivarium_workbench/lib/source_build_views.py",
    "vivarium_workbench/lib/study_runs.py",
}

_ROOT = Path(__file__).resolve().parents[1] / "vivarium_workbench"


def test_no_new_retired_endpoint_references():
    """No file outside the Phase 4 pending-removal set references a retired
    endpoint path."""
    offenders: dict[str, list[str]] = {}
    for path in _ROOT.rglob("*.py"):
        rel = path.relative_to(_ROOT.parent).as_posix()
        if rel in _RETIRED_FILES_PENDING_REMOVAL:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in RETIRED_PATTERNS:
            if pattern in text:
                offenders.setdefault(rel, []).append(pattern)
    assert not offenders, (
        "Retired SMS endpoint strings appeared outside the Phase 4 "
        f"pending-removal set: {offenders}"
    )


def test_pending_removal_set_only_tracks_files_that_exist():
    """The pending set may only shrink: every listed file must still exist.

    (When Phase 4 deletes them, the entries go with them; a listed file that
    vanished without updating this gate is exactly the drift it exists to
    catch.)
    """
    missing = [rel for rel in _RETIRED_FILES_PENDING_REMOVAL
               if not (_ROOT.parent / rel).exists()]
    assert not missing, (
        "Files listed in _RETIRED_FILES_PENDING_REMOVAL no longer exist — "
        f"remove them from the allowlist: {missing}"
    )


def test_retired_strings_absent_from_new_backends():
    """The new backends and shim are clean by construction: the retained
    client carries no retired path, and the shim names endpoints only in
    its error strings (never sends to them)."""
    clean_files = [
        "vivarium_workbench/lib/remote_api_client.py",
        "vivarium_workbench/lib/smoldyn_api_client.py",
        "vivarium_workbench/lib/smoldyn_composite_adapter.py",
    ]
    for rel in clean_files:
        text = (_ROOT.parent / rel).read_text(encoding="utf-8")
        for pattern in RETIRED_PATTERNS:
            assert pattern not in text, f"{rel} unexpectedly references {pattern}"
