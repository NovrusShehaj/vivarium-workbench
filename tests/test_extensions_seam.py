"""The opt-in extension seam — lib.extensions.

Covers discovery and opt-in, the read-only and broken-install gates, route
prefixing and ordering (extension routes must win over the catch-all), the
asset route's containment, the UI contribution contract (/api/ui-config and the
shell's escaped slots), and that neither published bundles nor extension-less
servers render a side panel.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.routing import Route

from vivarium_workbench.api.app import create_app, get_workspace
from vivarium_workbench.lib import extensions as ext_mod
from vivarium_workbench.lib import server_runtime


class _EP:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name, target=None, exc=None):
        self.name = name
        self._target = target
        self._exc = exc

    def load(self):
        if self._exc is not None:
            raise self._exc
        return self._target


def _make_ext_class(tmp_path: Path, *, ext_id="demo", available=(True, ""), panel=True,
                    title="Demo", panel_label="Demo panel", register_extra=None):
    static = tmp_path / f"{ext_id}-static"
    static.mkdir(exist_ok=True)
    (static / "demo.js").write_text("window.__demo = 1;")
    (static / "demo.css").write_text(".demo{}")
    (static / "nested").mkdir(exist_ok=True)
    (static / "nested" / "x.js").write_text("// nested")

    class DemoExtension:
        id = ext_id

        def __init__(self):
            self.title = title

        def assets(self):
            return ext_mod.ExtensionAssets(
                static_dir=static, scripts=("demo.js",), styles=("demo.css",),
                panel=panel, panel_label=panel_label, settings_section="demo-settings",
            )

        def register(self, router: APIRouter, ctx):
            @router.get("/ping")
            def ping():
                return {"pong": True, "mode": ctx.deployment_mode(), "readonly": ctx.readonly}

            if register_extra:
                register_extra(router)

        def availability(self):
            if isinstance(available, Exception):
                raise available
            return available

    return DemoExtension


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("VIVARIUM_WORKBENCH_EXTENSIONS", "VIVARIUM_DASHBOARD_EXTENSIONS",
                 "VIVARIUM_WORKBENCH_READONLY", "VIVARIUM_DASHBOARD_READONLY",
                 server_runtime.BIND_HOST_ENV):
        monkeypatch.delenv(name, raising=False)
    server_runtime.reset()
    yield
    server_runtime.reset()


def _app_with(monkeypatch, tmp_path, eps: dict, enabled: str | None):
    monkeypatch.setattr(ext_mod, "_entry_points", lambda: eps)
    if enabled is not None:
        monkeypatch.setenv("VIVARIUM_WORKBENCH_EXTENSIONS", enabled)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "workspace.yaml").write_text("name: ext-ws\n")
    app = create_app()
    app.dependency_overrides[get_workspace] = lambda: ws
    return app, TestClient(app)


# ---------------------------------------------------------------------------
# Opt-in and gating
# ---------------------------------------------------------------------------

def test_installed_but_not_enabled_loads_nothing(monkeypatch, tmp_path):
    cls = _make_ext_class(tmp_path)
    app, c = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled=None)
    assert app.state.extensions == []
    assert c.get("/api/ext/demo/ping").status_code == 404
    assert c.get("/api/ui-config").json()["extensions"] == []


def test_enabled_extension_registers_routes_and_assets(monkeypatch, tmp_path):
    cls = _make_ext_class(tmp_path)
    app, c = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled="demo")
    assert [e.id for e in app.state.extensions] == ["demo"]
    r = c.get("/api/ext/demo/ping")
    assert r.status_code == 200
    # Unknown bind (in-process client) fails closed to "hosted".
    assert r.json() == {"pong": True, "mode": "hosted", "readonly": False}
    a = c.get("/ext/demo/assets/demo.js")
    assert a.status_code == 200
    assert a.text == "window.__demo = 1;"
    assert a.headers["content-type"].startswith("application/javascript")
    assert a.headers["x-content-type-options"] == "nosniff"
    assert c.get("/ext/demo/assets/nested/x.js").status_code == 200


def _route_index(app, predicate) -> int:
    for i, route in enumerate(app.router.routes):
        if predicate(route):
            return i
    raise AssertionError("route not found")


def test_extension_routes_precede_the_catch_all(monkeypatch, tmp_path):
    cls = _make_ext_class(tmp_path)
    app, c = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled="demo")
    catch_all = _route_index(app, lambda r: getattr(r, "path", "") == "/{rel:path}")
    # FastAPI >= 0.13x keeps an included router as one lazy entry.
    ext_api = _route_index(app, lambda r: getattr(r, "path", "") == "/api/ext/demo/ping"
                           or getattr(getattr(r, "original_router", None), "prefix", "") == "/api/ext/demo")
    ext_assets = _route_index(app, lambda r: getattr(r, "path", "") == "/ext/demo/assets/{rel:path}")
    assert ext_api < catch_all
    assert ext_assets < catch_all
    # Behavioral: a workspace file at the same URL must not shadow the route.
    shadow = tmp_path / "ws" / "api" / "ext" / "demo" / "ping"
    shadow.parent.mkdir(parents=True)
    shadow.write_text("shadowed")
    r = c.get("/api/ext/demo/ping")
    assert r.status_code == 200 and r.json()["pong"] is True


def test_asset_route_contains_paths(monkeypatch, tmp_path):
    cls = _make_ext_class(tmp_path)
    (tmp_path / "secret.txt").write_text("outside")
    _, c = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled="demo")
    for path in ("/ext/demo/assets/%2e%2e/secret.txt",
                 "/ext/demo/assets/%2e%2e/%2e%2e/secret.txt",
                 "/ext/demo/assets/missing.js",
                 "/ext/demo/assets/"):
        r = c.get(path)
        assert r.status_code in (403, 404), path
        assert "outside" not in r.text


def test_asset_directory_is_not_a_request_parameter(monkeypatch, tmp_path):
    """Regression: the served directory must be bound by closure; a default
    argument would let a client pick the directory with a query parameter."""
    cls = _make_ext_class(tmp_path)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "demo.js").write_text("hijacked")
    app, c = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled="demo")
    r = c.get("/ext/demo/assets/demo.js", params={"_d": str(tmp_path / "other"),
                                                 "static_dir": str(tmp_path / "other")})
    assert r.text == "window.__demo = 1;"
    route = next(rt for rt in app.router.routes if getattr(rt, "path", "") == "/ext/demo/assets/{rel:path}")
    params = {p.name for p in route.dependant.query_params}
    assert params == set()


def test_readonly_server_never_loads_extensions(monkeypatch, tmp_path):
    cls = _make_ext_class(tmp_path)
    monkeypatch.setenv("VIVARIUM_WORKBENCH_READONLY", "1")
    app, c = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled="demo")
    assert app.state.extensions == []
    assert c.get("/api/ext/demo/ping").status_code == 404


def test_enabled_but_not_installed_boots_and_warns(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="vivarium_workbench.extensions"):
        app, c = _app_with(monkeypatch, tmp_path, {}, enabled="assistant")
    assert app.state.extensions == []
    assert c.get("/health").status_code == 200
    assert "not installed" in caplog.text


def test_missing_optional_dependency_logs_install_hint(monkeypatch, tmp_path, caplog):
    ep = _EP("assistant", exc=ImportError("No module named 'httpx'"))
    with caplog.at_level(logging.WARNING, logger="vivarium_workbench.extensions"):
        app, c = _app_with(monkeypatch, tmp_path, {"assistant": ep}, enabled="assistant")
    assert app.state.extensions == []
    assert c.get("/health").status_code == 200
    assert "vivarium-workbench[assistant]" in caplog.text


def test_mismatched_id_is_refused(monkeypatch, tmp_path, caplog):
    cls = _make_ext_class(tmp_path, ext_id="other")
    with caplog.at_level(logging.WARNING, logger="vivarium_workbench.extensions"):
        app, _ = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled="demo")
    assert app.state.extensions == []
    assert "refusing" in caplog.text


def test_non_protocol_object_is_refused(monkeypatch, tmp_path):
    app, _ = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", lambda: object())}, enabled="demo")
    assert app.state.extensions == []


def test_route_escaping_the_prefix_is_refused(monkeypatch, tmp_path):
    def hijack(router):
        router.routes.append(Route("/api/core-hijack", lambda request: None))

    cls = _make_ext_class(tmp_path, register_extra=hijack)
    app, c = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled="demo")
    assert app.state.extensions == []
    paths = [getattr(r, "path", "") for r in app.router.routes]
    assert "/api/core-hijack" not in paths
    assert "/api/ext/demo/ping" not in paths


def test_invalid_ids_in_env_are_ignored():
    env = {"VIVARIUM_WORKBENCH_EXTENSIONS": "Assistant, ../x, ok-1, ok-1, , 9bad, " + "a" * 40}
    assert ext_mod.enabled_extension_ids(env) == ["assistant", "ok-1"]


# ---------------------------------------------------------------------------
# Availability and UI contributions
# ---------------------------------------------------------------------------

def test_ui_config_lists_available_extension(monkeypatch, tmp_path):
    cls = _make_ext_class(tmp_path)
    _, c = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled="demo")
    exts = c.get("/api/ui-config").json()["extensions"]
    assert exts == [{
        "id": "demo", "title": "Demo", "panel": True, "panel_label": "Demo panel",
        "settings_section": "demo-settings",
        "scripts": ["ext/demo/assets/demo.js"], "styles": ["ext/demo/assets/demo.css"],
    }]


@pytest.mark.parametrize("available", [(False, "disabled on shared deployments"),
                                       RuntimeError("boom")])
def test_unavailable_extension_contributes_no_ui_but_keeps_routes(monkeypatch, tmp_path, available):
    cls = _make_ext_class(tmp_path, available=available)
    app, c = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled="demo")
    assert c.get("/api/ui-config").json()["extensions"] == []
    assert ext_mod.ui_contributions(app) == []
    # Routes stay registered so the extension can explain itself.
    assert c.get("/api/ext/demo/ping").status_code == 200


# ---------------------------------------------------------------------------
# Shell slots: escaped, and only when an extension contributes them
# ---------------------------------------------------------------------------

def _render_index(**ctx) -> str:
    import vivarium_workbench as pkg
    from vivarium_workbench.lib.report import _env
    tpl = _env(Path(pkg.__file__).parent / "templates").get_template("index.html.j2")
    return tpl.render(workspace_name="ws", **ctx)


def test_shell_without_extensions_has_no_panel_or_ext_assets():
    html = _render_index(extensions=[])
    assert 'id="viv-sidepanel"' not in html
    assert "sidepanel.js" not in html
    assert "data-viv-extension" not in html
    assert "data-viv-sidepanel-toggle" not in html
    # Core Settings page + theme menu are always present.
    assert 'id="page-settings"' in html
    assert 'id="viv-theme-menu-btn"' in html


def test_shell_with_panel_extension_renders_host_and_assets():
    contrib = {"id": "demo", "title": "Demo", "panel": True, "panel_label": "Demo panel",
               "settings_section": None,
               "scripts": ["ext/demo/assets/demo.js"], "styles": ["ext/demo/assets/demo.css"]}
    html = _render_index(extensions=[contrib])
    assert 'id="viv-sidepanel"' in html
    assert 'role="complementary"' in html
    assert "assets/sidepanel.js" in html
    assert '<script src="ext/demo/assets/demo.js" data-viv-extension="demo"></script>' in html
    assert '<link rel="stylesheet" href="ext/demo/assets/demo.css" data-viv-extension="demo">' in html
    assert 'data-viv-sidepanel-toggle="demo"' in html


def test_shell_escapes_extension_strings():
    evil = '"><script>alert(1)</script>'
    contrib = {"id": "demo", "title": evil, "panel": True, "panel_label": evil,
               "settings_section": None,
               "scripts": ["ext/demo/assets/" + evil], "styles": ["ext/demo/assets/" + evil]}
    html = _render_index(extensions=[contrib])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_published_home_has_no_extension_slots(tmp_path):
    from vivarium_workbench.publish import _render_home_html
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "workspace.yaml").write_text("name: pub-ws\n")
    html = _render_home_html(ws)
    assert 'id="viv-sidepanel"' not in html
    assert "data-viv-extension" not in html
    assert "sidepanel.js" not in html


def test_live_index_passes_contributions(monkeypatch, tmp_path):
    """GET / renders the shell with the available extensions' slots."""
    captured = {}

    def fake_render(ws_root=None, *, today=None, base_path="", extensions=None):
        captured["extensions"] = extensions
        out = Path(ws_root) / "reports" / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html></html>")
        return out

    cls = _make_ext_class(tmp_path)
    monkeypatch.setattr("vivarium_workbench.lib.report.render_workspace_report", fake_render)
    _, c = _app_with(monkeypatch, tmp_path, {"demo": _EP("demo", cls)}, enabled="demo")
    r = c.get("/")
    assert r.status_code == 200
    assert [e["id"] for e in captured.get("extensions") or []] == ["demo"]
