"""The pre-paint theme boot is shared, byte-identical, first, and consistent.

Every HTML entry point — the SPA shell (index.html.j2), the study iframe
(study-detail.html), the loom viewer served live, and the loom copy in a
published bundle — must run the same boot (templates/_theme_boot.html) before
any stylesheet, so the first paint is already in the resolved theme.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import vivarium_workbench as pkg
from vivarium_workbench.lib import report
from vivarium_workbench.lib.static_serving import STATIC_DIR, TEMPLATES_DIR

BOOT = (TEMPLATES_DIR / "_theme_boot.html").read_text(encoding="utf-8")
THEME_JS = (STATIC_DIR / "theme.js").read_text(encoding="utf-8")


def _render_index(**ctx) -> str:
    tpl = report._env(Path(pkg.__file__).parent / "templates").get_template("index.html.j2")
    return tpl.render(workspace_name="ws", asset_version="123", **ctx)


@pytest.fixture
def study_html(tmp_path) -> str:
    from vivarium_workbench.lib.study_page import render_study_detail_html
    from vivarium_workbench.lib.study_spec import load_study_detail_spec
    ws = tmp_path / "ws"
    sd = ws / "studies" / "s1"
    sd.mkdir(parents=True)
    (ws / "workspace.yaml").write_text("name: ws\n")
    (sd / "study.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "name": "s1", "objective": "o", "status": "draft",
        "baseline": [{"name": "core", "composite": "pkg.composites.core", "params": {}}],
        "variants": [], "runs": [],
    }))
    spec = load_study_detail_spec(ws, "s1")
    return render_study_detail_html(ws, "s1", spec)


def _first_index(html: str, *needles: str) -> int:
    idx = [html.find(n) for n in needles if html.find(n) != -1]
    return min(idx) if idx else -1


# ---------------------------------------------------------------------------
# The partial itself
# ---------------------------------------------------------------------------

def test_partial_shape():
    assert BOOT.startswith('<meta name="color-scheme" content="light dark">\n<script>')
    assert BOOT.rstrip().endswith("</script>")
    script = re.search(r"<script>(.*)</script>", BOOT, re.S).group(1)
    assert len(script.encode("utf-8")) < 600, "boot must stay tiny (inline, render-blocking)"
    for forbidden in ("src=", "fetch(", "XMLHttpRequest", "import("):
        assert forbidden not in BOOT, "boot must not touch the network"
    assert "{#" not in BOOT and "{{" not in BOOT, "partial must be Jinja-inert"


def test_default_preference_is_system():
    # Flipped from 'light' after the Phase 2a dark-surface audit (plan §15, R4).
    assert "D='system'" in BOOT
    assert "var DEFAULT_PREFERENCE = 'system';" in THEME_JS


def test_boot_default_matches_runtime_default():
    boot_default = re.search(r"D='(system|light|dark)'", BOOT).group(1)
    runtime_default = re.search(r"var DEFAULT_PREFERENCE = '(system|light|dark)';", THEME_JS).group(1)
    assert boot_default == runtime_default


def test_boot_and_runtime_share_key_and_cookie_names():
    assert "K='viv.theme'" in BOOT
    assert "var KEY = 'viv.theme';" in THEME_JS
    assert "viv_theme=(system|light|dark)" in BOOT
    assert "var COOKIE = 'viv_theme';" in THEME_JS


def test_theme_boot_snippet_is_the_partial():
    assert report.theme_boot_snippet() == BOOT


# ---------------------------------------------------------------------------
# SPA shell
# ---------------------------------------------------------------------------

def test_index_includes_boot_byte_identical_right_after_charset():
    html = _render_index(extensions=[])
    assert html.count(BOOT) == 1
    head = html[: html.index("</head>")]
    charset_end = head.index('<meta charset="UTF-8">') + len('<meta charset="UTF-8">')
    boot_at = head.index(BOOT)
    # Only whitespace (and a stripped Jinja comment) between charset and boot.
    assert head[charset_end:boot_at].strip() == ""
    first_css_or_script = _first_index(head[boot_at + len(BOOT):], "<link", "<script")
    assert first_css_or_script != -1
    assert boot_at < head.index("<link")
    assert head.index(BOOT) < head.index("<title>")


def test_index_loads_tokens_before_style_and_theme_after_session():
    html = _render_index(extensions=[])
    assert html.index("assets/tokens.css?v=123") < html.index("assets/style.css?v=123")
    assert html.index("assets/session.js?v=123") < html.index("assets/theme.js?v=123")
    assert html.index("assets/theme.js?v=123") < html.index("</head>")
    assert "assets/settings.js?v=123" in html


def test_index_no_longer_has_legacy_boot_or_binary_switch():
    html = _render_index(extensions=[])
    assert "if(t==='dark'||t==='light')" not in html
    assert 'role="switch"' not in html
    assert 'id="viv-theme-toggle"' not in html


def test_index_theme_menu_semantics():
    html = _render_index(extensions=[])
    assert 'aria-haspopup="menu"' in html
    assert len(re.findall(r'role="menuitemradio"', html)) == 3
    for choice in ("system", "light", "dark"):
        assert f'data-theme-choice="{choice}"' in html
    assert 'href="#settings"' in html
    # Authoritative Settings page with a native radio group.
    assert 'id="page-settings"' in html
    assert "<legend>Theme</legend>" in html
    assert len(re.findall(r'name="viv-theme-pref"', html)) == 3
    assert "System follows your operating system" in html


# ---------------------------------------------------------------------------
# Study iframe
# ---------------------------------------------------------------------------

def test_study_detail_includes_boot_first_and_theme_assets(study_html):
    assert study_html.count(BOOT) == 1
    head = study_html[: study_html.index("</head>")]
    assert head.index('<meta charset="utf-8">') < head.index(BOOT) < head.index("<link")
    assert head.index("/assets/tokens.css") < head.index("/assets/style.css")
    assert "/assets/theme.js" in head
    assert "if(t==='dark'||t==='light')" not in study_html


# ---------------------------------------------------------------------------
# Loom (served, not rendered) and the published copy
# ---------------------------------------------------------------------------

def test_theme_head_snippet_contents():
    snip = report.theme_head_snippet("/wb/assets/")
    assert snip.startswith(BOOT)
    assert '<link rel="stylesheet" href="/wb/assets/tokens.css">' in snip
    assert '<script src="/wb/assets/theme.js"></script>' in snip
    assert report.theme_head_snippet("../assets").count("../assets/tokens.css") == 1


def test_inject_head_snippet_lands_after_a_leading_charset():
    html = '<!doctype html><html><head>\n  <meta charset="UTF-8" />\n  <title>t</title><script src="x.js"></script></head></html>'
    out = report.inject_head_snippet(html, "<!--S-->")
    assert out.index('<meta charset="UTF-8" />') < out.index("<!--S-->") < out.index("<title>")


def test_inject_head_snippet_without_charset_goes_after_head():
    out = report.inject_head_snippet("<html><head><title>t</title></head></html>", "<!--S-->")
    assert out.startswith("<html><head><!--S--><title>")
    assert report.inject_head_snippet("<p>x</p>", "<!--S-->") == "<!--S--><p>x</p>"
    assert report.inject_head_snippet("<html><head></head></html>", "") == "<html><head></head></html>"


def test_published_loom_index_is_themed_relatively(tmp_path):
    from vivarium_workbench.publish import _theme_loom_index
    idx = tmp_path / "index.html"
    idx.write_text('<html><head><meta charset="UTF-8" /><script type="module" src="./assets/i.js"></script></head></html>')
    _theme_loom_index(idx)
    html = idx.read_text()
    assert BOOT in html
    assert 'href="../assets/tokens.css"' in html
    assert 'src="../assets/theme.js"' in html
    assert html.index(BOOT) < html.index("./assets/i.js")
    _theme_loom_index(tmp_path / "missing.html")   # absent file is a no-op


def test_normalize_asset_urls_leaves_extension_assets_alone():
    html = '<script src="ext/assistant/assets/a.js"></script><link href="/ext/x/assets/b.css">'
    assert report._normalize_asset_urls(html) == html


def test_live_base_path_prefixes_extension_assets():
    html = '<script src="ext/assistant/assets/a.js"></script><script src="/ext/x/assets/b.js"></script>'
    out = report._apply_live_base_path(html, "/wb")
    assert 'src="/wb/ext/assistant/assets/a.js"' in out
    assert 'src="/wb/ext/x/assets/b.js"' in out
