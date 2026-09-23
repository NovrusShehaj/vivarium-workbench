"""Phase 0 anti-regression gate: retired SMS endpoints must not reappear.

The viva-api core separation retired the SMS surface (E. coli/ParCa/PTools).
This gate enforces the workbench half of the plan
(docs/superpowers/plans/2026-09-21-workbench-sms-retirement-and-smoldyn-backend-plan.md,
§8): the retired endpoint path strings must not appear in *executable* remote
code. Phase 4 removed the last of them, so the pending-removal allowlist is
now empty and any reappearance fails.
"""
from __future__ import annotations

import re
from pathlib import Path

#: Retired endpoint path fragments (viva-api core separation). Anchored to the
#: API surface: a bare "/analyses/" also matches local study output directories
#: (``<study>/analyses/<run_id>/``), which are not an endpoint.
RETIRED_PATTERNS = (
    "/api/v1/simulations",
    "/api/v1/analyses",
    "/core/v1/simulator",
    "/core/v1/simulation/parca",
)

#: Files still referencing a retired endpoint. Phase 4 emptied this: every
#: entry below would be a regression, not a known gap.
_RETIRED_FILES_PENDING_REMOVAL: set[str] = set()


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


#: Modules and symbols the retirement removed outright. The plan's §8 criterion
#: is "no SmsApiClient symbol remains"; the module names catch a file being
#: restored from history without its callers being reviewed.
_RETIRED_SYMBOLS = (
    "SmsApiClient",
    "sms_api_client",
    "remote_run_views",
    "remote_run_jobs",
    "remote_pinned",
    "remote_simulations",
    "remote_build_source",
    "source_build_views",
    "remote_analysis_figures",
    "remote_run_landing",
    "remote_reconcile",
    "comparison_pinning",
)


def test_no_retired_module_or_client_symbol_remains():
    """Phase 4 deleted these; an import of one would not even resolve."""
    offenders: dict[str, list[str]] = {}
    for path in _ROOT.rglob("*.py"):
        rel = path.relative_to(_ROOT.parent).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for symbol in _RETIRED_SYMBOLS:
            # Word-anchored: `_append_remote_simulations` is a retained
            # pass-through seam, not a reference to the deleted module.
            pattern = re.compile(rf"(?<![\w.]){re.escape(symbol)}(?!\w)")
            # Only flag live code, not the prose that records the removal.
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                if pattern.search(line) and ("import " in line or f"{symbol}(" in line):
                    offenders.setdefault(rel, []).append(symbol)
                    break
    assert not offenders, offenders
