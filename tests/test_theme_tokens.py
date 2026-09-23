"""static/tokens.css: complete in both themes and WCAG 2.2 AA where it matters.

Parses the light ``:root`` block and the ``:root[data-theme="dark"]`` block,
checks that both themes define exactly the same tokens, that the legacy aliases
exist, and computes WCAG relative-luminance contrast for every declared
foreground/background pair: 4.5:1 for text (1.4.3), 3:1 for UI boundaries,
focus indicators and chart marks (1.4.11). Translucent colours are composited
over the surface they sit on before measuring.
"""
from __future__ import annotations

import re

import pytest

from vivarium_workbench.lib.static_serving import STATIC_DIR

CSS = (STATIC_DIR / "tokens.css").read_text(encoding="utf-8")


def _block(selector_re: str) -> dict[str, str]:
    m = re.search(selector_re + r"\s*\{([^}]*)\}", CSS)
    assert m, f"block {selector_re!r} not found"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return {k.strip(): v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


LIGHT = _block(r"(?m)^:root")
DARK = _block(r':root\[data-theme="dark"\]')
ALIASES = _block(r':root,\s*:root\[data-theme="dark"\]')
THEMES = {"light": LIGHT, "dark": DARK}


def _resolve(tokens: dict[str, str], value: str, depth: int = 0) -> str:
    m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
    if not m:
        return value.strip()
    assert depth < 5, "var() cycle"
    name = m.group(1)
    src = tokens.get(name) or ALIASES.get(name)
    assert src is not None, f"undefined token {name}"
    return _resolve(tokens, src, depth + 1)


def _rgba(value: str) -> tuple[float, float, float, float]:
    v = value.strip().lower()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        assert len(h) == 6, value
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", v)
    assert m, f"unsupported colour {value!r}"
    a = float(m.group(4)) if m.group(4) is not None else 1.0
    return (float(m.group(1)), float(m.group(2)), float(m.group(3)), a)


def _over(fg: tuple[float, float, float, float], bg: tuple[float, float, float, float]):
    a = fg[3]
    return (fg[0] * a + bg[0] * (1 - a), fg[1] * a + bg[1] * (1 - a), fg[2] * a + bg[2] * (1 - a), 1.0)


def _lum(c) -> float:
    def ch(x: float) -> float:
        s = x / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def contrast(theme: str, fg: str, bg: str, base: str = "--surface") -> float:
    tokens = THEMES[theme]
    base_c = _rgba(_resolve(tokens, tokens[base]))
    bg_c = _over(_rgba(_resolve(tokens, tokens[bg])), base_c)
    fg_c = _over(_rgba(_resolve(tokens, tokens[fg])), bg_c)
    hi, lo = sorted((_lum(fg_c), _lum(bg_c)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------

REQUIRED = {
    "--bg", "--rail", "--surface", "--surface-2", "--surface-3", "--surface-elevated",
    "--field", "--overlay", "--border", "--border-faint", "--border-2", "--border-control",
    "--text", "--heading", "--text-secondary", "--text-muted", "--text-subtle", "--text-placeholder",
    "--text-disabled", "--link", "--accent", "--accent2", "--accent-text", "--on-accent", "--on-fill",
    "--accent2-bg", "--accent2-border", "--accent-bg", "--accent-border",
    "--hover", "--active", "--active-fg", "--active-indicator", "--focus-ring",
    "--selection-bg",
    "--success-fg", "--success-bg", "--success-border",
    "--warning-fg", "--warning-bg", "--warning-border",
    "--danger-fg", "--danger-bg", "--danger-border",
    "--info-fg", "--info-bg", "--info-border",
    "--code-bg", "--code-fg",
    "--syntax-string", "--syntax-number", "--syntax-boolean", "--syntax-keyword", "--syntax-comment",
    "--diff-add-bg", "--diff-add-fg", "--diff-del-bg", "--diff-del-fg",
    "--chart-text", "--chart-axis", "--chart-grid", "--chart-series-1", "--chart-series-2",
    "--figure-surface", "--shadow-1", "--shadow-2", "--scrollbar-thumb", "--scrollbar-track",
}


def test_every_required_token_is_defined_in_both_themes():
    assert REQUIRED <= set(LIGHT), sorted(REQUIRED - set(LIGHT))
    assert REQUIRED <= set(DARK), sorted(REQUIRED - set(DARK))


def test_both_themes_define_the_same_tokens():
    assert set(LIGHT) == set(DARK), sorted(set(LIGHT) ^ set(DARK))


def test_legacy_aliases_follow_the_theme():
    assert ALIASES == {
        "--panel": "var(--surface)", "--gray": "var(--text-muted)", "--ink": "var(--text)",
        "--ink-2": "var(--text-muted)", "--line": "var(--border)",
        "--string": "var(--syntax-string)", "--num": "var(--syntax-number)",
        "--bool": "var(--syntax-boolean)",
    }


def test_color_scheme_declared_per_theme():
    assert re.search(r"(?m)^:root\s*\{\s*color-scheme:\s*light;", CSS)
    assert re.search(r':root\[data-theme="dark"\]\s*\{\s*color-scheme:\s*dark;', CSS)


def test_global_accessibility_rules_present():
    assert ":focus-visible" in CSS and "var(--focus-ring)" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert "@media (forced-colors: active)" in CSS
    assert "::selection" in CSS
    assert ".viv-theme-switching *" in CSS and "transition: none !important" in CSS


def test_style_css_no_longer_redefines_the_palette():
    style = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^:root\s*\{", style), "light tokens belong in tokens.css"
    assert not re.search(r':root\[data-theme="dark"\]\s*\{[^}]*--bg\s*:', style), "dark tokens belong in tokens.css"


# ---------------------------------------------------------------------------
# Contrast (WCAG 2.2 AA)
# ---------------------------------------------------------------------------

TEXT_PAIRS = [
    # (foreground, background)
    ("--text", "--bg"), ("--text", "--surface"), ("--text", "--surface-2"),
    ("--text", "--surface-3"), ("--text", "--surface-elevated"), ("--text", "--field"),
    ("--text", "--hover"), ("--text", "--active"), ("--text", "--rail"),
    ("--heading", "--bg"), ("--heading", "--surface"),
    ("--text-secondary", "--bg"), ("--text-secondary", "--surface"), ("--text-secondary", "--surface-2"),
    ("--bg", "--link"),                      # primary buttons: page-colour text on a link-coloured fill
    ("--text-muted", "--bg"), ("--text-muted", "--surface"), ("--text-muted", "--surface-2"),
    ("--text-muted", "--surface-elevated"), ("--text-muted", "--rail"),
    ("--text-subtle", "--bg"), ("--text-subtle", "--surface"), ("--text-subtle", "--surface-2"),
    ("--text-placeholder", "--field"),
    ("--link", "--bg"), ("--link", "--surface"), ("--link", "--surface-2"),
    ("--link", "--surface-elevated"),
    ("--accent-text", "--bg"), ("--accent-text", "--surface"),
    ("--accent2", "--accent2-bg"), ("--accent2", "--surface"), ("--accent-text", "--accent-bg"),
    # solid status fills carry page-colour text (badges, grade pills, DAG status chips)
    ("--bg", "--success-fg"), ("--bg", "--warning-fg"), ("--bg", "--danger-fg"),
    ("--bg", "--text-muted"), ("--bg", "--accent-text"),
    ("--active-fg", "--active"),
    ("--on-accent", "--accent"),
    ("--success-fg", "--success-bg"), ("--warning-fg", "--warning-bg"),
    ("--danger-fg", "--danger-bg"), ("--info-fg", "--info-bg"),
    ("--success-fg", "--surface"), ("--warning-fg", "--surface"),
    ("--danger-fg", "--surface"), ("--info-fg", "--surface"),
    ("--code-fg", "--code-bg"),
    ("--syntax-string", "--code-bg"), ("--syntax-number", "--code-bg"),
    ("--syntax-boolean", "--code-bg"), ("--syntax-keyword", "--code-bg"),
    ("--syntax-comment", "--code-bg"),
    ("--syntax-string", "--surface-2"), ("--syntax-number", "--surface-2"),
    ("--syntax-boolean", "--surface-2"), ("--syntax-keyword", "--surface-2"),
    ("--syntax-comment", "--surface-2"),
    ("--syntax-string", "--surface"), ("--syntax-number", "--surface"),
    ("--syntax-boolean", "--surface"),
    ("--diff-add-fg", "--diff-add-bg"), ("--diff-del-fg", "--diff-del-bg"),
    ("--chart-text", "--surface"), ("--chart-text", "--bg"),
]

NON_TEXT_PAIRS = [
    ("--border-control", "--surface"), ("--border-control", "--field"),
    ("--border-control", "--bg"),
    ("--focus-ring", "--bg"), ("--focus-ring", "--surface"), ("--focus-ring", "--surface-elevated"),
    ("--focus-ring", "--field"),
    ("--chart-axis", "--surface"),
    ("--chart-series-1", "--surface"), ("--chart-series-2", "--surface"),
    ("--active-indicator", "--active"), ("--active-indicator", "--rail"),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_contrast_aa(theme, fg, bg):
    # Diff backgrounds are translucent in dark mode and sit on the elevated
    # assistant surface; everything else composites over the page surface.
    base = "--surface-elevated" if bg.startswith("--diff") else "--surface"
    ratio = contrast(theme, fg, bg, base=base)
    assert ratio >= 4.5, f"{theme}: {fg} on {bg} = {ratio:.2f}:1 (< 4.5)"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg", NON_TEXT_PAIRS)
def test_non_text_contrast_aa(theme, fg, bg):
    ratio = contrast(theme, fg, bg)
    assert ratio >= 3.0, f"{theme}: {fg} vs {bg} = {ratio:.2f}:1 (< 3)"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_disabled_text_stays_legible(theme):
    # WCAG exempts disabled controls; the design floor is ~2.5:1 (plan §4.11).
    assert contrast(theme, "--text-disabled", "--surface") >= 2.5


def test_known_values_match_the_audit():
    """Spot-check the calculator against ratios recorded in the plan (§4.4)."""
    assert round(contrast("light", "--link", "--surface"), 2) == 5.17
    assert round(contrast("light", "--active-fg", "--active"), 2) == 7.08
    assert round(contrast("dark", "--text", "--surface"), 1) >= 10.0
