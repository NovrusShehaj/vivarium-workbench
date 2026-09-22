"""HTTP client for the retained (generic) viva-api endpoints.

Split out of ``sms_api_client.py`` per the workbench SMS-retirement plan
(docs/superpowers/plans/2026-09-21-workbench-sms-retirement-and-smoldyn-backend-plan.md,
Phase 1): the env-worker/relay/task-tier, capabilities, and compose surfaces are
generic core functionality that survives the SMS retirement, so they live here
under a name that does not reference the retired product.

Stdlib-only (urllib) to avoid adding a dependency, matching server.py's existing
outbound-HTTP approach. Pure HTTP — no DB, no orchestration. Parameterized by
base_url (the SSM tunnel, default http://localhost:8080).

The retired SMS-specific methods (simulator build/ParCa/workflow/analysis) are
NOT here; ``sms_api_client.SmsApiClient`` keeps them as typed errors until
Phase 4 removes the shim.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def remote_api_base() -> str:
    """Base URL of the viva-api core deployment (the SSM tunnel by default).

    ``VIVA_API_BASE`` is the canonical name. ``SMS_API_BASE`` is kept as a
    fallback alias until the Phase 4 cutover so existing deployments keep
    working through the transition; it is read second and will be removed.
    """
    return os.environ.get("VIVA_API_BASE") or os.environ.get("SMS_API_BASE", "http://localhost:8080")


class SmsApiError(Exception):
    """Raised when a viva-api call fails (non-200 or connection error).

    The historical name is kept (not renamed to ``RemoteApiError``) so every
    existing ``except SmsApiError`` keeps working through the rename; Phase 4
    may alias it. ``status`` carries the HTTP status code when the failure was
    an HTTP error (e.g. 404 from a deployment that predates an endpoint), else
    ``None`` for connection-level failures — so callers can distinguish "old
    server" from "unreachable" without parsing the message.
    """

    def __init__(self, message: str, status: "int | None" = None) -> None:
        super().__init__(message)
        self.status = status


#: Multi-GB native-store / results.zip / workspace-tarball downloads must not run
#: on the same 30s default used for small JSON status calls (CD2 pipeline audit
#: §3.12) — a slow SSM tunnel pulling a multi-GB store easily exceeds that.
#: Callers can still pass an explicit ``timeout=`` per call.
DOWNLOAD_TIMEOUT = 1800.0  # 30 minutes

#: Bounded retry policy for idempotent GET/status calls ONLY. Never applied to
#: POST (a retried ``compose_submit`` could double-submit a run) or DELETE.
#: Exponential backoff between attempts: ``backoff * 2**attempt``.
_GET_RETRIES = 3
_RETRY_BACKOFF = 0.5

#: e3 — timeouts and retry budgets by call class. A probe must fail fast (a
#: wedged tunnel usually fails the first byte); a status poll is latency-
#: sensitive; a list can be large; a download runs on ``DOWNLOAD_TIMEOUT``.
#: ``RemoteApiClient.for_(kind)`` builds a client wired to the right policy so
#: call sites stop hand-rolling ``timeout=``/``max_retries=`` themselves.
_CALL_CLASS_POLICY: "dict[str, tuple[float, int]]" = {
    "probe": (3.0, 1),
    "status": (5.0, 2),
    "list": (15.0, 2),
    "download": (DOWNLOAD_TIMEOUT, 1),
}


def _http_error_detail(e: HTTPError, limit: int = 200) -> str:
    """Best-effort extraction of the server's error body for diagnostics.

    Without this, a FastAPI 422/500 with a JSON ``{"detail": ...}`` body reaches
    the operator as only ``"POST <url> -> 422"`` (CD2 pipeline audit §3.12) —
    ``HTTPError.read()`` is never called. Returns a string like
    ``": missing field 'foo'"`` ready to append to the summary message, or
    ``""`` if the body couldn't be read/decoded (never raises — surfacing a
    better error must not itself produce a worse one).

    A gateway/proxy in front of viva-api (the SSM tunnel, an ALB) answers a
    502/503/504 with a full **HTML error page**, not JSON. Dumping that page
    into the message put a wall of ``<html>…`` markup into the user's alert
    (and the logs). So an HTML body is summarised to its ``<title>`` (e.g.
    ``502 Bad Gateway``) rather than echoed, and every body is capped short.
    """
    try:
        raw = e.read()
    except Exception:  # noqa: BLE001 — reading the error body must never mask the real error
        return ""
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and "detail" in parsed:
        detail = parsed["detail"]
        text = detail if isinstance(detail, str) else json.dumps(detail)
    elif text[:1] == "<" or "<html" in text[:256].lower():
        # HTML error page from a proxy/gateway (e.g. a 502/504 while the tunnel
        # or upstream is down) — summarise, never echo the markup.
        m = re.search(r"<title>\s*(.*?)\s*</title>", text, re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        text = title or "gateway error page"
    if len(text) > limit:
        text = text[:limit] + "…"
    return f": {text}" if text else ""


#: Header viva-api reads caller identity from where a deployment names one
#: (its IDENTITY_HEADER setting). Default matches oauth2-proxy's, which is what
#: the stanford-test deployment is configured for.
IDENTITY_HEADER = "X-Auth-Request-Email"

#: GitHub session sources that identify a PERSON. `token` is deliberately absent:
#: it is `VIVARIUM_WORKBENCH_GH_TOKEN`, a shared machine credential supplied as a
#: k8s Secret, and on a deployed workbench EVERY user resolves to it. Forwarding
#: that would give every user the same identity -- so they could all cancel each
#: other's tasks, while the record claimed a specific owner. That is worse than
#: anonymous: it looks like attribution and provides none.
_PERSONAL_SOURCES = ("device_flow", "gh_cli")


def caller_identity() -> str | None:
    """The signed-in GitHub login, when a PERSON is signed in. Else ``None``.

    NOT authentication, and viva-api's own docs are explicit that its header is
    not either: this is the best attribution the workbench can currently offer,
    which is a real `@login` GitHub already verified, forwarded so a task has an
    owner instead of being unowned and cancellable by anyone.

    The proper answer is the `Principal` that `session_registry.SessionEntry`
    already reserves space for -- a workbench identity that does not depend on a
    user happening to have signed into GitHub for an unrelated reason.

    Never raises. Identity is a nicety on every path that calls it, and an
    unreachable keyring or a slow `gh` must not fail the request it decorates.
    """
    try:
        from vivarium_workbench.lib import github_auth

        session = github_auth.current_session()
    except Exception:  # noqa: BLE001 - see docstring
        return None
    if session is None or session.source not in _PERSONAL_SOURCES:
        return None
    login = (session.login or "").strip()
    # Qualify it: a bare `octocat` next to `you@example.com` in the same column
    # reads as an email that lost its domain. This says where it came from.
    return f"{login}@github" if login else None


class RemoteApiClient:
    """Client for the retained generic viva-api surface.

    Retained families (post core-separation): env workers, the relay, the
    durable task tier, capabilities/health, and the generic compose pipeline.
    """

    def __init__(self, base_url: str = "http://localhost:8080", timeout: float = 30.0,
                 max_retries: int = _GET_RETRIES, *, force_link: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Default retry budget for idempotent GET/status calls made through this
        # client. Latency-sensitive callers (the SWR "fresh" path on
        # /api/simulations) construct a client with a short timeout and
        # ``max_retries=1`` so a wedged tunnel can't pin the request thread for
        # ``timeout * _GET_RETRIES`` seconds. Never consulted by _post/_delete.
        self.max_retries = max_retries
        # When True, this client bypasses the RemoteLink circuit breaker
        # (``check(force=True)``) — used by the breaker's own probe and by
        # user-initiated explicit refreshes, which must be able to re-test a link
        # the breaker currently holds open. Normal calls leave it False so a
        # known-down tunnel fails in microseconds instead of the full timeout.
        self.force_link = force_link

    @classmethod
    def for_(cls, kind: str, base_url: str | None = None, *, force_link: bool = False) -> "RemoteApiClient":
        """Build a client wired to the timeout/retry policy for a call *class*.

        ``kind`` is one of ``"probe"``, ``"status"``, ``"list"``, ``"download"``
        (see :data:`_CALL_CLASS_POLICY`). ``base_url`` defaults to the configured
        endpoint (:func:`remote_api_base`).
        """
        try:
            timeout, retries = _CALL_CLASS_POLICY[kind]
        except KeyError:
            raise ValueError(
                f"unknown call class {kind!r}; expected one of {sorted(_CALL_CLASS_POLICY)}"
            ) from None
        return cls(base_url or remote_api_base(), timeout=timeout,
                   max_retries=retries, force_link=force_link)

    def _link(self) -> Any:
        """The RemoteLink circuit breaker for this client's base_url (lazy import
        to avoid an import cycle: remote_link imports this module)."""
        from vivarium_workbench.lib.remote_link import link

        return link(self.base_url)

    def _mark_link_up(self) -> None:
        """Passive success signal to the breaker — never let bookkeeping raise."""
        try:
            self._link().mark_up()
        except Exception:  # noqa: BLE001 - breaker bookkeeping must not fail a real call
            pass

    def _mark_link_down(self, error: str) -> None:
        """Passive connection-failure signal to the breaker (never raises)."""
        try:
            self._link().mark_down(error)
        except Exception:  # noqa: BLE001 - see _mark_link_up
            pass

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        """Request headers, carrying the caller's identity when there is one.

        Sent on every request rather than only on task submits: viva-api ignores
        an unrecognised header, and a client that identified itself for some
        calls and not others would be harder to reason about than one that
        always does.
        """
        headers = {"Accept": accept}
        identity = caller_identity()
        if identity:
            headers[IDENTITY_HEADER] = identity
        return headers

    def _get(
        self,
        path: str,
        params: dict | None = None,
        *,
        retries: int | None = None,
        backoff: float = _RETRY_BACKOFF,
    ) -> dict:
        """GET a JSON endpoint, retrying transient failures.

        GET is idempotent (unlike ``_post``), so a bounded exponential-backoff
        retry is safe here: connection errors/timeouts and 5xx responses are
        retried up to ``retries`` attempts total; a 4xx is a client error, not a
        transient one, and is raised immediately without retrying.
        """
        if retries is None:
            retries = self.max_retries
        # Fail fast when the tunnel is known-down (raises CircuitOpen, itself an
        # SmsApiError). force_link bypasses it for the probe / explicit refresh.
        self._link().check(force=self.force_link)
        url = self.base_url + path
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        req = Request(url, method="GET", headers=self._headers())
        attempt = 0
        while True:
            attempt += 1
            try:
                with urlopen(req, timeout=self.timeout) as r:  # noqa: S310 — fixed scheme, internal tunnel
                    payload = json.loads(r.read().decode())
                self._mark_link_up()
                return payload
            except HTTPError as e:
                # The server answered — the tunnel is alive; do not trip the
                # breaker on an HTTP status (a 4xx/5xx is not a link failure).
                if e.code >= 500 and attempt < retries:
                    time.sleep(backoff * (2 ** (attempt - 1)))
                    continue
                raise SmsApiError(f"GET {url} -> {e.code}{_http_error_detail(e)}", status=e.code) from e
            except (URLError, OSError) as e:
                if attempt < retries:
                    time.sleep(backoff * (2 ** (attempt - 1)))
                    continue
                self._mark_link_down(str(e))
                raise SmsApiError(f"GET {url} failed (viva-api unreachable — is the tunnel up?): {e}") from e

    # -- env workers (REFACTOR-PLAN §2A.8, #942) ----------------------------
    # The workbench cannot create Jobs (§2B.2 gives it no cluster access), so it
    # asks viva-api to run a simulator image as a worker. We tell it where to
    # dial back and with what token — we already know our own address, so
    # viva-api needs to discover nothing.

    def start_env_worker(self, *, commit: str, callback_host: str, callback_port: int,
                         token: str, workspace: str | None = None,
                         session_key: str | None = None) -> dict:
        """POST /env-worker/v1/workers — run the prebuilt image for ``commit``."""
        body: dict = {
            "commit": commit,
            "callback_host": callback_host,
            "callback_port": callback_port,
            "token": token,
        }
        if workspace:
            body["workspace"] = workspace
        if session_key:
            body["session_key"] = session_key
        return self._post("/env-worker/v1/workers", json_body=body)

    def env_worker_status(self, job_name: str, *, include_logs: bool = False) -> dict:
        return self._get(f"/env-worker/v1/workers/{job_name}",
                         {"include_logs": "true"} if include_logs else None)

    def stop_env_worker(self, job_name: str) -> dict:
        """DELETE /env-worker/v1/workers/{job_name} — idempotent."""
        return self._delete(f"/env-worker/v1/workers/{job_name}")

    # -- relay (plan §C) ----------------------------------------------------
    #
    # The three above run the IN-CLUSTER shape: we tell viva-api where to dial
    # back, because we can be dialled. A laptop cannot — its SSM tunnel is
    # laptop-initiated with no inbound path — so these hand the socket to
    # viva-api instead and reach the worker over HTTP.

    def start_relayed_env_worker(self, *, commit: str, workspace: str | None = None,
                                 session_key: str | None = None,
                                 accept_timeout: float | None = None) -> dict:
        """POST /env-worker/v1/relay/workers — viva-api holds the connection.

        Note what is ABSENT versus ``start_env_worker``: no callback host, port
        or token. viva-api binds its own listener and mints its own token, which
        is the whole point — we have no address a worker could dial.
        """
        body: dict = {"commit": commit}
        if workspace:
            body["workspace"] = workspace
        if session_key:
            body["session_key"] = session_key
        if accept_timeout is not None:
            body["accept_timeout"] = accept_timeout
        return self._post("/env-worker/v1/relay/workers", json_body=body)

    def call_relayed_env_worker(self, job_name: str, *, method: str,
                                params: dict | None = None,
                                timeout: float | None = None) -> dict:
        """POST /env-worker/v1/relay/workers/{job}/call — one JSON-RPC call."""
        body: dict = {"method": method, "params": params or {}}
        if timeout is not None:
            body["timeout"] = timeout
        return self._post(f"/env-worker/v1/relay/workers/{job_name}/call", json_body=body)

    def stop_relayed_env_worker(self, job_name: str) -> dict:
        """DELETE /env-worker/v1/relay/workers/{job_name} — idempotent."""
        return self._delete(f"/env-worker/v1/relay/workers/{job_name}")

    # -- the task tier (plan §E option (e)) ---------------------------------
    #
    # For calls that cannot be a synchronous HTTP request. `run_study` runs a
    # study's baseline and every variant to completion; holding a socket open
    # for that is what produced the double-run bug, because the socket timeout
    # fired and the pool re-ran the whole study.

    def submit_env_worker_task(self, job_name: str, *, method: str,
                               params: dict | None = None) -> dict:
        """POST /env-worker/v1/tasks — 202 with a task_id; the row exists first."""
        return self._post("/env-worker/v1/tasks", json_body={
            "job_name": job_name, "method": method, "params": params or {},
        })

    def get_env_worker_task(self, task_id: int) -> dict:
        return self._get(f"/env-worker/v1/tasks/{task_id}")

    def cancel_env_worker_task(self, task_id: int) -> dict:
        return self._delete(f"/env-worker/v1/tasks/{task_id}")

    def capabilities(self) -> dict:
        """GET /core/v1/capabilities — ``{version, capabilities: [str, ...]}``.

        The deployment's capability advertisement (viva-api #262, dual-engine
        W4/Q5). Clients branch on MEMBERSHIP in ``capabilities``, never on
        ``version`` (which is for humans/logs). A deployment predating the
        endpoint 404s — callers use ``lib.server_capabilities.fetch_capabilities``,
        which maps that to "advertises nothing" per the endpoint's own contract.
        """
        return self._get("/core/v1/capabilities")

    def ping(self, timeout: float | None = None) -> str:
        """GET /version — lightweight reachability probe for the health indicator.

        Returns the viva-api version string; raises :class:`SmsApiError` if the
        endpoint is unreachable. Uses a short timeout by default (min of 5 s and
        the client timeout) so a health check never hangs the UI.
        """
        url = self.base_url + "/version"
        req = Request(url, method="GET", headers=self._headers())
        try:
            with urlopen(req, timeout=timeout or min(self.timeout, 5.0)) as r:  # noqa: S310 — fixed scheme, internal tunnel
                body = r.read().decode().strip()
        except HTTPError as e:
            raise SmsApiError(f"GET {url} -> {e.code}{_http_error_detail(e)}", status=e.code) from e
        except (URLError, OSError) as e:
            raise SmsApiError(f"GET {url} failed (viva-api unreachable — is the tunnel up?): {e}") from e
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return body
        if isinstance(parsed, dict):
            return str(parsed.get("version") or parsed.get("__version__") or body)
        return str(parsed)

    def _delete(self, path: str) -> dict:
        url = self.base_url + path
        req = Request(url, method="DELETE", headers=self._headers())
        try:
            with urlopen(req, timeout=self.timeout) as r:  # noqa: S310 — fixed scheme, internal tunnel
                body = r.read().decode()
                return json.loads(body) if body else {}
        except HTTPError as e:
            raise SmsApiError(f"DELETE {url} -> {e.code}{_http_error_detail(e)}", status=e.code) from e
        except (URLError, OSError) as e:
            raise SmsApiError(f"DELETE {url} failed (viva-api unreachable): {e}") from e

    def _post(self, path: str, params: dict | None = None, json_body: dict | None = None) -> dict:
        # doseq=True so list-valued params become repeated keys (?observables=a&observables=b)
        # Fail fast when the tunnel is known-down (CircuitOpen, an SmsApiError).
        self._link().check(force=self.force_link)
        url = self.base_url + path
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        data = json.dumps(json_body).encode() if json_body is not None else None
        headers = self._headers()
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, method="POST", headers=headers)
        try:
            with urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                payload = json.loads(r.read().decode())
            self._mark_link_up()
            return payload
        except HTTPError as e:
            # Server answered — link is alive; do not trip the breaker on status.
            raise SmsApiError(f"POST {url} -> {e.code}{_http_error_detail(e)}", status=e.code) from e
        except (URLError, OSError) as e:
            self._mark_link_down(str(e))
            raise SmsApiError(f"POST {url} failed (viva-api unreachable — is the tunnel up?): {e}") from e

    # ------------------------------------------------------------------
    # Compose endpoints (generic .pbg runner, Phase C)
    # ------------------------------------------------------------------

    def compose_check(self, pbg_bytes: bytes) -> dict:
        """GET /compose/v1/simulation/check — verify compose endpoint reachability.

        Raises :exc:`SmsApiError` if the server is unreachable or returns a
        non-200 status.
        """
        return self._get("/compose/v1/simulation/check")

    def compose_submit(
        self,
        pbg_bytes: bytes,
        extra_pip_deps: list[str] | None = None,
        interval_time: float = 1.0,
        filename: str = "composite.pbg",
        analysis_options: dict | None = None,
    ) -> int:
        """POST /compose/v1/simulation/run — submit a .pbg file for execution.

        The file is uploaded as multipart/form-data with the field name
        ``uploaded_file`` (required by the viva-api endpoint).  Any
        ``extra_pip_deps`` are appended as repeated ``extra_pip_deps`` query
        parameters so the container can install them before running.

        Parameters
        ----------
        pbg_bytes:
            Raw bytes of the ``.pbg`` JSON document.
        extra_pip_deps:
            Additional pip-installable dependencies (e.g.
            ``["git+https://github.com/org/repo.git@sha"]``).
        interval_time:
            Step interval forwarded to the run endpoint.
        filename:
            Filename reported in the multipart header (cosmetic).
        analysis_options:
            Analysis options to run server-side (composite-auto-results Task 8).
            Sent as a JSON-encoded string in a multipart ``analysis_options``
            form field — NOT a query param. The compose run route reads this via
            ``Form()`` + ``json.loads()``, not ``Query()``; a query param there
            is silently dropped by FastAPI and no analyses ever run (the bug
            behind #1022 being a silent no-op). Omitted entirely (no field at
            all) when ``None``.

        Returns
        -------
        int
            ``simulation_database_id`` from the response.
        """
        boundary = "----vivdash00boundary"
        parts = [
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="uploaded_file"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n"
                "\r\n"
            ).encode() + pbg_bytes + b"\r\n"
        ]
        if analysis_options:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="analysis_options"\r\n'
                    "\r\n"
                    f"{json.dumps(analysis_options)}\r\n"
                ).encode()
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        content_type = f"multipart/form-data; boundary={boundary}"

        params: dict = {"interval_time": interval_time}
        if extra_pip_deps:
            params["extra_pip_deps"] = extra_pip_deps  # list → repeated key via doseq

        url = self.base_url + "/compose/v1/simulation/run"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"

        req = Request(
            url,
            data=body,
            method="POST",
            headers={**self._headers(), "Content-Type": content_type},
        )
        try:
            with urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                data = json.loads(r.read().decode())
        except HTTPError as e:
            raise SmsApiError(f"POST {url} -> {e.code}{_http_error_detail(e)}", status=e.code) from e
        except (URLError, OSError) as e:
            raise SmsApiError(
                f"POST {url} failed (viva-api unreachable — is the tunnel up?): {e}"
            ) from e
        return int(data["simulation_database_id"])

    def compose_status(self, task_id: int) -> dict:
        """GET /compose/v1/simulation/{id}/status — poll run status."""
        return self._get(f"/compose/v1/simulation/{task_id}/status")

    def compose_status_batch(self, ids: "list[int]") -> "list[dict]":
        """GET /compose/v1/simulations/status/batch?ids=… — many runs, one call.

        viva-api returns a JSON **list** here (``list[ComposeHpcRun]``), unlike
        every other endpoint on this client, so the ``_get`` result is widened
        rather than trusted as a dict. Existing to serve reconcile-style polling:
        a caller holding N in-flight ``simulation_id``s asks once instead of N
        times (REFACTOR-PLAN §2A.8 / run-orchestration-consolidation §A2').
        """
        if not ids:
            return []
        raw: Any = self._get("/compose/v1/simulations/status/batch",
                             {"ids": [int(i) for i in ids]})
        return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []

    def download_compose_results(self, sim_id: int, dest: Path, timeout: float | None = None) -> Path:
        """GET /compose/v1/simulation/{id}/results — stream results.tar.gz to dest.

        The route is backend-aware server-side (compose-results-land-p0 T5a):
        a Ray/Batch (GovCloud) simulation streams a gzip tarball of its S3
        output prefix, while a SLURM simulation's SSH/SCP branch is unchanged.
        Both are served under the same ``.tar.gz`` contract this client expects,
        so ``land_remote_run``/``fold_analyses`` (which already read
        ``.tar.gz``) work unmodified.

        Returns
        -------
        Path
            ``dest / "results.tar.gz"``
        """
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / "results.tar.gz"
        url = f"{self.base_url}/compose/v1/simulation/{sim_id}/results"
        req = Request(url, method="GET", headers=self._headers("application/gzip"))
        to = timeout if timeout is not None else DOWNLOAD_TIMEOUT
        try:
            with urlopen(req, timeout=to) as r, open(out_path, "wb") as f:  # noqa: S310
                shutil.copyfileobj(r, f)
        except HTTPError as e:
            raise SmsApiError(f"GET {url} -> {e.code}{_http_error_detail(e)}", status=e.code) from e
        except (URLError, OSError) as e:
            raise SmsApiError(
                f"GET {url} failed (viva-api unreachable — is the tunnel up?): {e}"
            ) from e
        return out_path
