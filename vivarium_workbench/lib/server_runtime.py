"""Process-level facts about how this server was launched.

``serve`` (``lib.startup.serve_fastapi``) records the bind host and URL base path
here before it imports and runs the app, so request-time policy can ask "is this
a loopback-only, single-user server, or is it reachable from a network?" without
re-deriving it from request headers (which a client controls).

When nothing configured this module (the app was imported in-process, e.g. by a
test's ``TestClient`` or a bare ``uvicorn vivarium_workbench.api.app:app``), the
bind host is unknown and :func:`deployment_mode` fails closed to ``"hosted"``.

AI-agnostic; standard library only.
"""
from __future__ import annotations

import ipaddress
import os
from typing import Literal

DeploymentMode = Literal["local", "hosted"]

LOOPBACK_NAMES: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_bind_host: str | None = None
_base_path: str = ""


def configure(*, bind_host: str | None, base_path: str = "") -> None:
    """Record how the server is bound. Called once by ``serve_fastapi``."""
    global _bind_host, _base_path
    _bind_host = (bind_host or "").strip() or None
    _base_path = (base_path or "").strip()


def reset() -> None:
    """Forget the recorded launch facts (tests)."""
    configure(bind_host=None, base_path="")


#: Fallback for launchers that hand uvicorn an import string (the app is then
#: built in a process that never called :func:`configure`, e.g. ``--reload``).
BIND_HOST_ENV = "VIVARIUM_WORKBENCH_BIND_HOST"


def bind_host() -> str | None:
    """The host the server socket is bound to, or ``None`` when unknown."""
    if _bind_host is not None:
        return _bind_host
    return (os.environ.get(BIND_HOST_ENV) or "").strip() or None


def base_path() -> str:
    """The configured URL prefix (``serve --base-path``), or ``""``."""
    return _base_path


def is_loopback_host(host: str | None) -> bool:
    """True for ``127.0.0.0/8``, ``::1`` and the name ``localhost``."""
    if not host:
        return False
    h = host.strip().strip("[]").lower()
    if h in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _trust_proxy() -> bool:
    from vivarium_workbench.lib.csrf import is_trust_proxy_via_env
    return is_trust_proxy_via_env(os.environ)


def deployment_mode() -> DeploymentMode:
    """``"local"`` only for a loopback bind with no proxy/base-path; else ``"hosted"``.

    A server bound to loopback, not behind a trusted proxy and not serving under
    a URL prefix is a single-user local tool. Everything else — a ``0.0.0.0``
    bind, a container, an ALB prefix, or an unknown bind (in-process import) —
    is treated as potentially shared.
    """
    if not is_loopback_host(bind_host()):
        return "hosted"
    if _base_path or _trust_proxy():
        return "hosted"
    return "local"
