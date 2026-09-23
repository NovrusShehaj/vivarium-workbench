"""``Host``-header allowlist — the DNS-rebinding defense.

The CSRF guard (``lib.csrf``) compares a request's ``Origin`` with its ``Host``.
A DNS-rebinding page passes that check: after rebinding, the browser sends
``Origin: http://evil.example:PORT`` *and* ``Host: evil.example:PORT`` to the
loopback server. Rejecting unexpected ``Host`` values closes that hole.

Policy (evaluated per request, so it follows ``lib.server_runtime``):

* **Loopback bind** (the ``serve`` default): only ``127.0.0.1``, ``localhost``
  and ``[::1]`` (any port) plus operator-configured hosts are accepted.
* **Non-loopback bind** (``--host 0.0.0.0``, containers, ALB): enforcement is
  **opt-in** — an AWS ALB rewrites ``Host`` to an internal name, so a default
  allowlist would break hosted deployments. Configure it with
  ``--allowed-host`` / ``VIVARIUM_WORKBENCH_ALLOWED_HOSTS``.
* **Unknown bind** (in-process imports such as ``TestClient``): enforced only
  when hosts are explicitly configured.
* ``VIVARIUM_WORKBENCH_ALLOWED_HOSTS=*`` disables the guard explicitly.

Rejections are HTTP 400 with the canonical ``{"error": ...}`` envelope. The
middleware is a plain ASGI class (not ``BaseHTTPMiddleware``) so it adds no
buffering or task layer in front of streamed responses.

AI-agnostic; standard library only.
"""
from __future__ import annotations

import json
import os
from typing import Any, Awaitable, Callable, Iterable, Mapping

from vivarium_workbench.lib import server_runtime

ALLOWED_HOSTS_ENV_SUFFIX = "ALLOWED_HOSTS"

LOOPBACK_ALLOWED: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_Scope = dict[str, Any]
_Receive = Callable[[], Awaitable[dict[str, Any]]]
_Send = Callable[[dict[str, Any]], Awaitable[None]]


def configured_allowed_hosts(env: Mapping[str, str] | None = None) -> list[str]:
    """Hosts from ``VIVARIUM_WORKBENCH_ALLOWED_HOSTS`` (comma-separated), normalised."""
    from vivarium_workbench.lib.env_compat import get_env
    raw = get_env(ALLOWED_HOSTS_ENV_SUFFIX, "", env=env) or ""
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def split_host_port(value: str) -> tuple[str, str | None]:
    """Split a ``Host`` header into ``(host, port)``; IPv6 brackets are removed."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        end = v.find("]")
        if end == -1:
            return v, None
        host = v[1:end]
        rest = v[end + 1:]
        return host, (rest[1:] if rest.startswith(":") and rest[1:] else None)
    if v.count(":") == 1:
        host, _, port = v.partition(":")
        return host, (port or None)
    # Bare IPv6 without brackets (not valid in a Host header, but be tolerant).
    return v, None


def effective_allowed_hosts(env: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """The allowlist in force right now, or ``None`` when the guard is inactive."""
    extra = configured_allowed_hosts(env)
    if "*" in extra:
        return None
    bind = server_runtime.bind_host()
    if bind is None or not server_runtime.is_loopback_host(bind):
        # Unknown or non-loopback bind: enforce only what the operator configured.
        return (LOOPBACK_ALLOWED | frozenset(extra)) if extra else None
    return LOOPBACK_ALLOWED | frozenset(extra)


def is_host_allowed(host_header: str | None, allowed: Iterable[str] | None) -> bool:
    """True when ``host_header`` matches ``allowed`` (``None`` = guard inactive).

    An allowlist entry may be a bare host (any port) or ``host:port`` (exact).
    """
    if allowed is None:
        return True
    if not host_header:
        return False
    host, port = split_host_port(host_header)
    for entry in allowed:
        e_host, e_port = split_host_port(entry)
        if e_host != host:
            continue
        if e_port is None or e_port == port:
            return True
    return False


class HostGuardMiddleware:
    """ASGI middleware enforcing :func:`effective_allowed_hosts`."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        allowed = effective_allowed_hosts(os.environ)
        if allowed is not None:
            host = None
            for name, value in scope.get("headers") or []:
                if name == b"host":
                    host = value.decode("latin-1")
                    break
            if not is_host_allowed(host, allowed):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = json.dumps({
                    "error": "host not allowed",
                    "hint": "This server only answers requests addressed to an allowed host "
                            "name. Add the name with `serve --allowed-host` or "
                            "VIVARIUM_WORKBENCH_ALLOWED_HOSTS.",
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"cache-control", b"no-store"),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
