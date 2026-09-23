"""Opt-in workbench extensions — a small, AI-agnostic seam.

An extension is a separately installed Python package that contributes HTTP
routes, static assets, and a UI slot (an optional right-side panel and a
Settings section) to the workbench. The core knows nothing about what an
extension does; it only discovers, gates, registers, and advertises it.

Discovery and opt-in
--------------------
* Extensions advertise themselves with a ``vivarium_workbench.extensions`` entry
  point whose target is a zero-argument factory returning an object that
  satisfies :class:`WorkbenchExtension`.
* **Nothing loads unless explicitly enabled**:
  ``VIVARIUM_WORKBENCH_EXTENSIONS=<id>[,<id>…]`` or
  ``vivarium-workbench serve --enable-extension <id>``.
* Extensions are **never loaded on a read-only server**
  (``VIVARIUM_WORKBENCH_READONLY``) and never rendered into a published static
  bundle (``publish.py`` renders the shell without extension slots).
* An extension whose package or optional dependencies are missing is logged
  once with an actionable hint; the server still boots.

Registration contract
---------------------
* Routes go on an ``APIRouter`` the seam creates with the prefix
  ``/api/ext/<id>`` — an extension cannot claim core paths.
* Static assets are served from :attr:`ExtensionAssets.static_dir` at
  ``/ext/<id>/assets/<file>`` with the same traversal guard as the bundled
  viewers.
* Registration happens inside ``create_app()`` *before* the catch-all
  ``/{rel:path}`` route, so extension routes win route matching.
* Each loaded extension reports whether it is *available* right now
  (:meth:`WorkbenchExtension.availability`) — e.g. an extension may refuse to
  run on a shared, unauthenticated deployment. Unavailable extensions register
  their routes (so they can explain themselves) but contribute no UI.

The import direction is one-way: extensions import the core; the core never
imports an extension package (enforced by an import-linter contract).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from fastapi import APIRouter, FastAPI
from fastapi.responses import Response

from vivarium_workbench.lib import sensitive_paths as _sensitive

log = logging.getLogger("vivarium_workbench.extensions")

ENTRY_POINT_GROUP = "vivarium_workbench.extensions"
EXTENSIONS_ENV_SUFFIX = "EXTENSIONS"
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

_MIME = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".map": "application/json",
    ".html": "text/html",
    ".txt": "text/plain",
}


@dataclass(frozen=True)
class ExtensionAssets:
    """What an extension contributes to the browser shell."""

    #: Directory served at ``/ext/<id>/assets/``. ``None`` = no static assets.
    static_dir: Path | None = None
    #: Files (relative to ``static_dir``) loaded with ``<script>`` at the end of
    #: ``<body>``, in order.
    scripts: tuple[str, ...] = ()
    #: Files (relative to ``static_dir``) loaded with ``<link rel=stylesheet>``.
    styles: tuple[str, ...] = ()
    #: True when the extension mounts into the shared right-side panel host.
    panel: bool = False
    #: Short label for the rail entry that toggles the panel.
    panel_label: str = ""
    #: DOM id of a section the extension renders inside the Settings page.
    settings_section: str | None = None


@dataclass(frozen=True)
class ExtensionContext:
    """Facts and request helpers handed to :meth:`WorkbenchExtension.register`.

    Extensions use these instead of importing private ``api.app`` internals.
    """

    extension_id: str
    readonly: bool
    #: FastAPI dependency resolving the request's workspace root.
    get_workspace: Callable[..., Path]
    #: ``(request) -> session key or None`` — the per-tab routing key.
    session_key_of: Callable[..., str | None]
    #: ``() -> "local" | "hosted"`` evaluated at call time (see lib.server_runtime).
    deployment_mode: Callable[[], str]


@runtime_checkable
class WorkbenchExtension(Protocol):
    """The object an extension's entry-point factory returns."""

    id: str
    title: str

    def assets(self) -> ExtensionAssets: ...

    def register(self, router: APIRouter, ctx: ExtensionContext) -> None: ...

    def availability(self) -> tuple[bool, str]: ...


@dataclass
class LoadedExtension:
    """A successfully registered extension, as the shell sees it."""

    id: str
    title: str
    assets: ExtensionAssets
    _availability: Callable[[], tuple[bool, str]] = field(repr=False)

    def availability(self) -> tuple[bool, str]:
        try:
            ok, reason = self._availability()
            return bool(ok), str(reason or "")
        except Exception as exc:  # noqa: BLE001 - a broken check must fail closed
            log.warning("extension %r availability check failed: %s", self.id, exc)
            return False, "availability check failed"

    def ui_contribution(self) -> dict[str, Any]:
        """JSON-safe description used by the shell template and ``/api/ui-config``."""
        base = f"ext/{self.id}/assets/"
        return {
            "id": self.id,
            "title": self.title,
            "panel": bool(self.assets.panel),
            "panel_label": self.assets.panel_label or self.title,
            "settings_section": self.assets.settings_section,
            "scripts": [base + s for s in self.assets.scripts],
            "styles": [base + s for s in self.assets.styles],
        }


def enabled_extension_ids(env: Mapping[str, str] | None = None) -> list[str]:
    """Extension ids the operator enabled (``VIVARIUM_WORKBENCH_EXTENSIONS``).

    Invalid ids are dropped with a warning; order is preserved and duplicates
    removed.
    """
    from vivarium_workbench.lib.env_compat import get_env
    raw = get_env(EXTENSIONS_ENV_SUFFIX, "", env=env) or ""
    out: list[str] = []
    for token in raw.split(","):
        ext_id = token.strip().lower()
        if not ext_id:
            continue
        if not _ID_RE.match(ext_id):
            log.warning("ignoring invalid extension id %r in %s", ext_id, EXTENSIONS_ENV_SUFFIX)
            continue
        if ext_id not in out:
            out.append(ext_id)
    return out


def _entry_points() -> dict[str, metadata.EntryPoint]:
    try:
        eps = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 - broken metadata must not break boot
        log.warning("could not read %s entry points: %s", ENTRY_POINT_GROUP, exc)
        return {}
    return {ep.name: ep for ep in eps}


def _serve_extension_asset(static_dir: Path, rel: str) -> Response:
    """Serve one extension asset with traversal + symlink containment."""
    target = _sensitive.resolve_contained(static_dir, rel or "")
    if target is None:
        return Response(status_code=404)
    if not target.is_file():
        return Response(status_code=404)
    mime = _MIME.get(target.suffix.lower(), "application/octet-stream")
    return Response(
        content=target.read_bytes(),
        headers={"Content-Type": mime, "Cache-Control": "no-store",
                 "X-Content-Type-Options": "nosniff"},
    )


def _make_asset_route(static_dir: Path) -> Callable[[str], Response]:
    """Build the asset endpoint for one extension.

    The directory is captured by closure — NOT as a default argument, which
    FastAPI would expose as a client-controllable query parameter.
    """
    def _asset_route(rel: str = "") -> Response:
        return _serve_extension_asset(static_dir, rel)
    return _asset_route


def _instantiate(ext_id: str, ep: metadata.EntryPoint) -> WorkbenchExtension | None:
    try:
        factory = ep.load()
        # The entry point names a zero-argument factory (usually the class).
        ext = factory() if callable(factory) else factory
    except ImportError as exc:
        log.warning(
            "extension %r is enabled but could not be imported (%s). Install its "
            "dependencies (for the assistant: pip install 'vivarium-workbench[assistant]').",
            ext_id, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - one broken extension must not break boot
        log.warning("extension %r failed to load: %s", ext_id, exc)
        return None
    if not isinstance(ext, WorkbenchExtension):
        log.warning("extension %r does not implement the WorkbenchExtension protocol", ext_id)
        return None
    if getattr(ext, "id", None) != ext_id:
        log.warning("extension entry point %r returned id %r; refusing it", ext_id, getattr(ext, "id", None))
        return None
    return ext


def load_and_register(
    app: FastAPI,
    *,
    readonly: bool,
    get_workspace: Callable[..., Path],
    session_key_of: Callable[..., str | None],
    env: Mapping[str, str] | None = None,
) -> list[LoadedExtension]:
    """Discover, gate, and register enabled extensions on ``app``.

    Must be called from ``create_app()`` before the catch-all route. Returns the
    registered extensions (also stored by the caller on ``app.state.extensions``).
    """
    ids = enabled_extension_ids(env if env is not None else os.environ)
    if not ids:
        return []
    if readonly:
        log.info("extensions %s not loaded: the server is read-only", ids)
        return []

    from vivarium_workbench.lib import server_runtime

    eps = _entry_points()
    loaded: list[LoadedExtension] = []
    for ext_id in ids:
        ep = eps.get(ext_id)
        if ep is None:
            log.warning(
                "extension %r is enabled but not installed (no %r entry point).",
                ext_id, ENTRY_POINT_GROUP,
            )
            continue
        ext = _instantiate(ext_id, ep)
        if ext is None:
            continue
        try:
            assets = ext.assets()
            router = APIRouter(prefix=f"/api/ext/{ext_id}")
            ctx = ExtensionContext(
                extension_id=ext_id,
                readonly=readonly,
                get_workspace=get_workspace,
                session_key_of=session_key_of,
                deployment_mode=server_runtime.deployment_mode,
            )
            ext.register(router, ctx)
            for route in router.routes:
                path = getattr(route, "path", "")
                if not path.startswith(f"/api/ext/{ext_id}"):
                    raise ValueError(f"route {path!r} escapes the /api/ext/{ext_id} prefix")
            app.include_router(router)
            if assets.static_dir is not None:
                app.add_api_route(
                    f"/ext/{ext_id}/assets/{{rel:path}}",
                    _make_asset_route(Path(assets.static_dir)),
                    methods=["GET"], include_in_schema=False,
                )
        except Exception as exc:  # noqa: BLE001 - never let one extension break boot
            log.warning("extension %r failed to register: %s", ext_id, exc)
            continue
        loaded.append(LoadedExtension(
            id=ext_id, title=str(getattr(ext, "title", ext_id)), assets=assets,
            _availability=ext.availability,
        ))
        log.info("extension %r registered", ext_id)
    return loaded


def ui_contributions(app: Any) -> list[dict[str, Any]]:
    """UI contributions of the *available* extensions registered on ``app``."""
    loaded = getattr(getattr(app, "state", None), "extensions", None) or []
    out: list[dict[str, Any]] = []
    for ext in loaded:
        ok, _reason = ext.availability()
        if ok:
            out.append(ext.ui_contribution())
    return out
