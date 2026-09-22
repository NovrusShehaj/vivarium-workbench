"""HTTP client for the viva-smoldyn simulation service.

Phase 2 of the workbench SMS-retirement plan
(docs/superpowers/plans/2026-09-21-workbench-sms-retirement-and-smoldyn-backend-plan.md):
viva-smoldyn now owns its own HTTP endpoint,
``POST /smoldyn/v1/simulations`` — a **bounded synchronous** run that returns
per-step molecule counts in the response body. There are no job IDs, no status
polling, and no artifact URLs; clients submit a config and wait.

Stdlib-only (urllib), matching ``remote_api_client``. Reuses the existing
``RemoteLink`` circuit breaker (keyed by base_url, so a new endpoint needs no
breaker changes) and mirrors the call-class timeout policy pattern.

Error contract (viva-smoldyn docs/api.md):
* 422 — request validation (FastAPI detail; surfaced verbatim)
* 413 — body or resource limits
* 500 — native failure / abnormal exit / invalid output
* 503 — missing native runtime, capacity, or shutdown
* 504 — wall-clock timeout

Runtime error bodies carry only ``detail.code``, a safe message and a request
ID — this client surfaces those, never the submitted model.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from vivarium_workbench.lib.remote_api_client import (
    IDENTITY_HEADER,
    caller_identity,
)


def smoldyn_api_base() -> str | None:
    """Configured base URL of the viva-smoldyn service, or ``None``.

    Unset means "backend hidden": callers gate the Smoldyn backend on this
    (mirroring how the workbench treats other optional remote links).
    A trailing slash is stripped so callers can join paths unconditionally.
    """
    base = os.environ.get("SMOLDYN_API_BASE")
    return base.rstrip("/") if base else None


class SmoldynApiError(Exception):
    """A viva-smoldyn call failed (non-200 or connection error).

    ``status`` carries the HTTP status when the failure was an HTTP error,
    else ``None`` for connection-level failures. ``code`` carries the service's
    safe ``detail.code`` when present (never includes the submitted model).
    """

    def __init__(self, message: str, status: "int | None" = None,
                 code: "str | None" = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _error_parts(e: HTTPError, limit: int = 200) -> tuple[str, str | None]:
    """Extract ``(detail_suffix, code)`` from an error body with ONE read.

    ``HTTPError.read()`` consumes the stream, so the detail text and the safe
    ``detail.code`` must be derived from a single read. The detail suffix is
    ``": <text>"`` ready to append to the summary message (``""`` when
    unreadable); ``code`` is the service's safe error code when present.
    Runtime error bodies carry only ``detail.code``, a safe message and a
    request ID; validation errors (422) carry FastAPI validation detail. An
    HTML gateway page is summarised to its title, never echoed. Never raises.
    """
    try:
        raw = e.read()
    except Exception:  # noqa: BLE001 — reading the error body must never mask the real error
        return "", None
    if not raw:
        return "", None
    text = raw.decode("utf-8", errors="replace").strip()
    code: str | None = None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        candidate = parsed.get("code")
        if isinstance(candidate, str):
            code = candidate
        detail = parsed.get("detail")
        if isinstance(detail, dict):
            candidate = detail.get("code")
            if isinstance(candidate, str):
                code = candidate
            detail = detail.get("message", detail)
        if "detail" in parsed:
            text = detail if isinstance(detail, str) else json.dumps(detail)
    elif text[:1] == "<" or "<html" in text[:256].lower():
        m = re.search(r"<title>\s*(.*?)\s*</title>", text, re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        text = title or "gateway error page"
    if len(text) > limit:
        text = text[:limit] + "…"
    return (f": {text}" if text else ""), code


class SmoldynApiClient:
    """Client for ``POST /smoldyn/v1/simulations`` (bounded synchronous run).

    The timeout must exceed the service's wall-clock cap (default
    ``SMOLDYN_API_MAX_SECONDS=15``) plus spawn overhead; there is deliberately
    no retry — resubmitting a simulation is a new run (POSTs are not
    idempotent, and the endpoint has no job IDs to dedupe by).
    """

    #: Call-class policy, mirroring remote_api_client's ``for_()`` pattern.
    #: ``run`` must exceed the service's ``MAX_SECONDS`` cap plus spawn/setup
    #: overhead. Single operation for now; ``probe`` is reachability only.
    _CALL_CLASS_POLICY: "dict[str, tuple[float, int]]" = {
        "run": (30.0, 1),  # service cap 15s + spawn/serialization overhead; no retries
        "probe": (3.0, 1),
    }

    def __init__(self, base_url: "str | None" = None, timeout: float = 30.0,
                 *, force_link: bool = False) -> None:
        base = base_url or smoldyn_api_base()
        if not base:
            raise SmoldynApiError(
                "SMOLDYN_API_BASE is not configured — the Smoldyn backend is hidden"
            )
        self.base_url = base.rstrip("/")
        self.timeout = timeout
        self.force_link = force_link

    @classmethod
    def for_(cls, kind: str = "run", base_url: "str | None" = None, *,
             force_link: bool = False) -> "SmoldynApiClient":
        """Build a client wired to the timeout policy for a call class."""
        try:
            timeout, _retries = cls._CALL_CLASS_POLICY[kind]
        except KeyError:
            raise ValueError(
                f"unknown call class {kind!r}; expected one of {sorted(cls._CALL_CLASS_POLICY)}"
            ) from None
        return cls(base_url=base_url, timeout=timeout, force_link=force_link)

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept}
        identity = caller_identity()
        if identity:
            headers[IDENTITY_HEADER] = identity
        return headers

    def _link(self) -> Any:
        """The RemoteLink circuit breaker for this base_url (lazy import to
        avoid the import cycle: remote_link imports remote_api_client)."""
        from vivarium_workbench.lib.remote_link import link

        return link(self.base_url)

    def run_simulation(self, payload: dict) -> dict:
        """POST /smoldyn/v1/simulations — submit and wait for the counts.

        ``payload`` is the request document per viva-smoldyn's contract:
        ``{duration, species, dimensions, bounds, boundary_type, dt, seed,
        reactions, interval}``. Unknown fields are rejected server-side
        (``extra=forbid``); the response is ``{samples: [{time,
        molecule_counts}, ...]}``.

        The server caps concurrent runs; a saturated service answers 503
        rather than queuing — surfaced as an HTTP error with ``code``.
        """
        url = f"{self.base_url}/smoldyn/v1/simulations"
        data = json.dumps(payload).encode()
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        req = Request(url, data=data, method="POST", headers=headers)
        try:
            with urlopen(req, timeout=self.timeout) as r:  # noqa: S310 — operator-configured private-network URL
                out = json.loads(r.read().decode())
        except HTTPError as e:
            detail, code = _error_parts(e)
            raise SmoldynApiError(
                f"POST {url} -> {e.code}{detail}",
                status=e.code,
                code=code,
            ) from e
        except (URLError, OSError) as e:
            # Connection-level failure: trip the breaker (a 4xx/5xx never does —
            # the service answered, so the link is alive).
            try:
                self._link().mark_down(str(e))
            except Exception:  # noqa: BLE001 — breaker bookkeeping must not mask the error
                pass
            raise SmoldynApiError(f"POST {url} failed (service unreachable): {e}") from e
        if not isinstance(out, dict) or not isinstance(out.get("samples"), list):
            raise SmoldynApiError(f"POST {url} returned an unexpected response shape")
        return out

    def probe(self, timeout: float | None = None) -> dict:
        """Lightweight reachability probe for the UI health indicator.

        Uses a plain ``GET /`` — the service has no documented root route, so
        a 404 still means *reachable*. Raises :class:`SmoldynApiError` when
        the service cannot be reached at all.
        """
        url = f"{self.base_url}/"
        req = Request(url, method="GET", headers=self._headers())
        try:
            with urlopen(req, timeout=timeout or 3.0) as r:  # noqa: S310
                body = r.read().decode()
        except HTTPError as e:
            if e.code == 404:
                return {"reachable": True}
            raise SmoldynApiError(f"GET {url} -> {e.code}", status=e.code) from e
        except (URLError, OSError) as e:
            raise SmoldynApiError(f"GET {url} failed (service unreachable): {e}") from e
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        return {"reachable": True, **parsed} if isinstance(parsed, dict) else {"reachable": True}
