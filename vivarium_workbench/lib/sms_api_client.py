"""Deprecation shim: the SMS-specific client retired per the 2026-09-21 plan.

The retained generic viva-api client (env-worker/relay/task tier, capabilities,
compose) moved to :mod:`vivarium_workbench.lib.remote_api_client` — import from
there. This module keeps the historical import paths working during the
transition:

* ``RemoteApiClient`` re-exported as ``SmsApiClient`` (same construction
  signature and retained methods), so existing call sites keep working;
* every RETIRED SMS method raises :class:`RetiredEndpointError` — a typed,
  immediate failure naming the replacement — instead of a confusing 404 from a
  server that no longer has the route.

Phase 4 of the plan deletes this shim and retires its callers.
"""

from __future__ import annotations

from pathlib import Path

from vivarium_workbench.lib.remote_api_client import (
    DOWNLOAD_TIMEOUT,
    IDENTITY_HEADER,
    RemoteApiClient,
    SmsApiError,
    _http_error_detail,
    caller_identity,
    remote_api_base,
)

# Intentional re-exports (Phase 1 shim): historical import paths keep working
# until Phase 4 deletes this module. ruff F401 is satisfied via __all__.
__all__ = [
    "DOWNLOAD_TIMEOUT",
    "IDENTITY_HEADER",
    "RetiredEndpointError",
    "RemoteApiClient",
    "SmsApiClient",
    "SmsApiError",
    "caller_identity",
    "remote_api_base",
    "sms_api_base",
]

#: Historical name for the base-URL lookup (``sms_api_base``) — kept as an alias
#: so ``workspace_deps_views`` / ``remote_simulations`` re-exports keep working.
sms_api_base = remote_api_base

# Re-exported for tests that import the helper directly from here.
_http_error_detail = _http_error_detail


class RetiredEndpointError(SmsApiError):
    """A retired SMS endpoint was called from retained code.

    Raised immediately, client-side — no HTTP round-trip. ``status`` is None
    (nothing was contacted). The message names the plan that retired the
    endpoint and, where one exists, what replaces it.
    """


def _retired(name: str, replacement: str | None = None) -> RetiredEndpointError:
    hint = f" Replacement: {replacement}." if replacement else ""
    return RetiredEndpointError(
        f"{name} is a retired SMS endpoint "
        f"(viva-api core separation; workbench plan 2026-09-21 Phase 4)."
        f"{hint}"
    )


class SmsApiClient(RemoteApiClient):
    """Retained client + typed errors for the retired SMS surface.

    Construction, the ``for_()`` call-class policy, and every retained method
    behave exactly as before (inherited from :class:`RemoteApiClient`). Calling
    a retired SMS method raises :class:`RetiredEndpointError`.
    """

    # -- simulator build registry (RETIRED) ---------------------------------

    def latest_simulator(self, repo_url: str, branch: str) -> dict:
        raise _retired("GET /core/v1/simulator/latest")

    def register_simulator(self, repo_url: str, branch: str, commit: str) -> dict:
        raise _retired("POST /core/v1/simulator/upload")

    def upload_simulator(self, simulator: dict, force: bool = False) -> dict:
        raise _retired("POST /core/v1/simulator/upload")

    def simulator_status(self, simulator_id: int) -> dict:
        raise _retired("GET /core/v1/simulator/status")

    def list_simulators(self) -> dict:
        raise _retired("GET /core/v1/simulator/versions")

    def composite_resolve(self, simulator_id: int, composite_ref: str,
                          overrides: dict | None = None, timeout: float | None = None) -> dict:
        raise _retired(
            "POST /core/v1/simulator/{id}/composite-resolve",
            "local resolution via vivarium_workbench.lib.composite_resolve",
        )

    def download_workspace(self, simulator_id: int, dest_dir: Path,
                           timeout: float | None = None) -> Path:
        raise _retired("GET /api/v1/simulations/workspace")

    # -- SMS workflow runs (RETIRED) -----------------------------------------

    def list_build_simulations(self, simulator_id: int) -> list:
        raise _retired("GET /api/v1/simulations")

    def simulation_status(self, simulation_id: int) -> dict:
        raise _retired("GET /api/v1/simulations/{id}/status")

    def get_simulation(self, simulation_id: int) -> dict:
        raise _retired("GET /api/v1/simulations/{id}")

    def simulator_commit(self, simulator_id: int) -> str | None:
        raise _retired("GET /core/v1/simulator/versions (via simulator_commit)")

    def simulation_chain_progress(self, simulation_id: int) -> dict:
        raise _retired("GET /api/v1/simulations/{id}/chain-progress")

    def observables(self, simulation_id: int, names: list[str], seed: int = 0) -> dict:
        raise _retired("GET /api/v1/simulations/{id}/observables")

    def run_simulation(self, **kwargs) -> dict:
        raise _retired("POST /api/v1/simulations")

    def download_data(self, simulation_id: int, dest_dir: Path,
                      timeout: float | None = None) -> Path:
        raise _retired("POST /api/v1/simulations/{id}/data")

    # -- SMS analysis (RETIRED) ----------------------------------------------

    def run_analysis(self, simulation_id: int, modules: dict) -> dict:
        raise _retired("POST /api/v1/simulations/{id}/analysis")

    def analysis_status(self, analysis_id: int) -> dict:
        raise _retired("GET /analyses/{id}/status")

    def list_analyses(self, simulation_id: int) -> list:
        raise _retired("GET /api/v1/simulations/{id}/analyses")
