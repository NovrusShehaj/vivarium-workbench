"""Theme support for standalone generated HTML documents.

Some documents the workbench generates (the comparative time-series viz, for
example) are written to disk and shown in iframes inside the workbench, but
are also opened on their own. They cannot count on ``/tokens.css`` or
``/theme.js`` being reachable, so they inline what they need instead:

* the design tokens they use, for both themes. The values are read from
  ``static/tokens.css`` so there is one source of truth and no duplicated hex.
* the pre-paint boot from ``templates/_theme_boot.html``, verbatim.
* a small live-sync script. It re-runs that same boot function when another
  same-origin document changes the preference (``storage`` event) or the OS
  scheme changes, then dispatches ``viv:themechange`` on ``window`` (as
  ``static/theme.js`` does) so page scripts such as Plotly relayouts can
  follow.

Opened standalone (``file://``), the document resolves to the default
preference exactly like any other entry point.
"""
from __future__ import annotations

import re
from functools import lru_cache

from vivarium_workbench.lib.report import theme_boot_snippet
from vivarium_workbench.lib.static_serving import STATIC_DIR

_TOKENS_CSS = STATIC_DIR / "tokens.css"
_DECL_RE = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")
_BOOT_FN_RE = re.compile(r"<script>(\(function\(r\)\{.*\}\))\(document\.documentElement\);</script>", re.S)


def _block(css: str, selector_re: str) -> dict[str, str]:
    m = re.search(selector_re + r"\s*\{([^}]*)\}", css)
    if not m:
        raise ValueError(f"tokens.css: block {selector_re!r} not found")
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return {k: " ".join(v.split()) for k, v in _DECL_RE.findall(body)}


@lru_cache(maxsize=4)
def _token_tables(mtime_ns: int) -> tuple[dict[str, str], dict[str, str]]:
    css = _TOKENS_CSS.read_text(encoding="utf-8")
    return _block(css, r"(?m)^:root"), _block(css, r':root\[data-theme="dark"\]')


def token_values(names: tuple[str, ...] | list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """``(light, dark)`` values of the given tokens, as declared in tokens.css."""
    light, dark = _token_tables(_TOKENS_CSS.stat().st_mtime_ns)
    missing = [n for n in names if n not in light or n not in dark]
    if missing:
        raise KeyError(f"tokens not defined in both themes: {missing}")
    return {n: light[n] for n in names}, {n: dark[n] for n in names}


def inline_token_style(names: tuple[str, ...] | list[str]) -> str:
    """A ``<style>`` block defining ``names`` for both themes."""
    light, dark = token_values(names)
    lt = "".join(f"{k}:{v};" for k, v in light.items())
    dk = "".join(f"{k}:{v};" for k, v in dark.items())
    return (f'<style>:root{{color-scheme:light;{lt}}}'
            f':root[data-theme="dark"]{{color-scheme:dark;{dk}}}</style>')


def _boot_function() -> str:
    m = _BOOT_FN_RE.search(theme_boot_snippet())
    if not m:
        raise ValueError("templates/_theme_boot.html: boot function not found")
    return m.group(1)


def live_sync_script() -> str:
    """Re-resolve the theme on preference/OS changes and announce it."""
    return (
        "<script>(function(){var boot=" + _boot_function() + ",r=document.documentElement;"
        "function apply(){var before=r.getAttribute('data-theme');boot(r);"
        "if(r.getAttribute('data-theme')!==before){try{window.dispatchEvent(new CustomEvent('viv:themechange',"
        "{detail:{resolved:r.getAttribute('data-theme'),preference:r.getAttribute('data-theme-pref')}}));}catch(e){}}}"
        "window.addEventListener('storage',function(e){if(!e||e.key===null||e.key==='viv.theme')apply();});"
        "try{var q=window.matchMedia('(prefers-color-scheme: dark)');"
        "if(q.addEventListener)q.addEventListener('change',apply);else if(q.addListener)q.addListener(apply);}catch(e){}"
        "})();</script>"
    )


def standalone_theme_head(names: tuple[str, ...] | list[str]) -> str:
    """Everything a standalone document needs in ``<head>`` to follow the theme."""
    return theme_boot_snippet() + inline_token_style(names) + live_sync_script()
