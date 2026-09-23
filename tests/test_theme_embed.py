"""Standalone generated documents follow the theme (lib/theme_embed.py).

The comparative time-series viz is written to disk and shown in an iframe
inside the workbench (and opened on its own). It inlines the pre-paint boot
verbatim, the tokens it uses for both themes (read from tokens.css), and a
live-sync script; its Plotly backgrounds are transparent and its text/grid
colours come from the chart tokens.
"""
from __future__ import annotations

import re

import pytest

from vivarium_workbench.lib import theme_embed
from vivarium_workbench.lib.comparative_viz import _VIZ_THEME_TOKENS, render_comparative_time_series
from vivarium_workbench.lib.report import theme_boot_snippet
from vivarium_workbench.lib.static_serving import STATIC_DIR

TOKENS_CSS = (STATIC_DIR / "tokens.css").read_text(encoding="utf-8")


def _css_value(block_re: str, name: str) -> str:
    body = re.search(block_re + r"\s*\{([^}]*)\}", TOKENS_CSS).group(1)
    return " ".join(re.search(re.escape(name) + r"\s*:\s*([^;]+);", body).group(1).split())


@pytest.mark.parametrize("name", ["--surface", "--text", "--chart-text", "--chart-grid", "--chart-axis"])
def test_token_values_come_from_tokens_css(name):
    light, dark = theme_embed.token_values([name])
    assert light[name] == _css_value(r"(?m)^:root", name)
    assert dark[name] == _css_value(r':root\[data-theme="dark"\]', name)
    assert light[name] != dark[name]


def test_unknown_token_is_an_error():
    with pytest.raises(KeyError):
        theme_embed.token_values(["--no-such-token"])


def test_inline_style_defines_both_themes():
    style = theme_embed.inline_token_style(["--surface", "--text"])
    light, dark = theme_embed.token_values(["--surface", "--text"])
    assert style.startswith("<style>:root{color-scheme:light;")
    assert f"--surface:{light['--surface']};" in style
    assert ':root[data-theme="dark"]{color-scheme:dark;' in style
    assert f"--surface:{dark['--surface']};" in style


def test_head_reuses_the_boot_verbatim_and_adds_live_sync():
    head = theme_embed.standalone_theme_head(["--surface"])
    boot = theme_boot_snippet()
    assert head.startswith(boot)
    fn = re.search(r"<script>(\(function\(r\)\{.*\}\))\(document\.documentElement\);</script>", boot, re.S).group(1)
    live = theme_embed.live_sync_script()
    assert "var boot=" + fn in live, "the live script must run the exact boot algorithm"
    assert "addEventListener('storage'" in live and "prefers-color-scheme: dark" in live
    assert "window.dispatchEvent(new CustomEvent('viv:themechange'" in live


def test_comparative_viz_is_themed(tmp_path):
    out = render_comparative_time_series(
        runs=[{"label": "a"}], observable_path="agents.x", title="T <x>", y_label="y",
        output_path=tmp_path / "viz.html")
    html = out.read_text(encoding="utf-8")
    head = html[:html.index("</head>")]
    assert theme_embed.standalone_theme_head(_VIZ_THEME_TOKENS) in head
    assert head.index('<meta name="color-scheme"') < head.index("plotly-2.27.0")
    assert '"plot_bgcolor": "rgba(0,0,0,0)"' in html and '"paper_bgcolor": "rgba(0,0,0,0)"' in html
    assert "#fafafa" not in html and "background:#fff" not in html
    assert "background:var(--surface);color:var(--text)" in html
    assert "_vivChartTheme" in html and 'window.addEventListener("viv:themechange"' in html
    assert "T &lt;x&gt;" in html
