"""Shrink-only ratchet on hard-coded colour debt (plan §4.8 / §17.1, T11).

Counts, per UI file, the colour patterns that do not follow the theme:

* ``hex``          hex colour literals (``#fff``, ``#1e293b``)
* ``rgb``          ``rgb()`` / ``rgba()`` literals
* ``dark_rules``   lines carrying ``data-theme="dark"`` (per-selector dark overrides)
* ``style_star``   ``[style*=`` substring selectors that retarget inline styles
* ``inline_color`` inline colour declarations with a literal value in markup/JS
                   (``color:#…``, ``background:#…``, ``border:1px solid #…``)

and compares them with the committed ``tests/theme_baseline.json``. Like
``known_failures.txt``, the numbers may only go DOWN: new code uses the tokens
in ``static/tokens.css`` (the one file allowed to define colours). A file that
is not in the baseline is new and must be colour-free.

When a count drops, lower the baseline with::

    python tests/test_theme_ratchet.py --write

which refuses to raise any number.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / "tests" / "theme_baseline.json"

# UI sources the ratchet watches (relative to the repo root).
GLOBS = (
    "vivarium_workbench/static/*.css",
    "vivarium_workbench/static/*.js",
    "vivarium_workbench/static/*.html",
    "vivarium_workbench/templates/*.html",
    "vivarium_workbench/templates/*.j2",
    "vivarium_workbench_assistant/static/*.css",
    "vivarium_workbench_assistant/static/*.js",
)
# The palette itself.
EXEMPT = {"vivarium_workbench/static/tokens.css"}

HEX_RE = re.compile(r"(?<![\w&/-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b(?![\w-])")
RGB_RE = re.compile(r"\brgba?\(")
DARK_RE = re.compile(r'data-theme="dark"|data-theme=\'dark\'')
STYLE_STAR_RE = re.compile(r"\[style\*=")
INLINE_RE = re.compile(
    r"(?<![\w-])(?:color|background(?:-color)?|border(?:-[a-z]+)*|outline(?:-color)?|fill|stroke)"
    r"\s*:\s*[^;\"'{}<>\n]*(?:#[0-9a-fA-F]{3,8}\b|rgba?\()")
METRICS = ("hex", "rgb", "dark_rules", "style_star", "inline_color")


def _files() -> list[str]:
    out: set[str] = set()
    for pattern in GLOBS:
        for p in REPO.glob(pattern):
            rel = p.relative_to(REPO).as_posix()
            if rel not in EXEMPT:
                out.add(rel)
    return sorted(out)


def _strip_comments(text: str) -> str:
    """Drop comments so issue numbers (``#754``) and prose never count."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)          # CSS / JS block comments
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)          # Jinja comments
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)          # HTML comments
    return re.sub(r"(?m)(^|[\s;{}(,])//.*$", r"\1", text)      # JS line comments (not ``https://``)


def measure(rel: str) -> dict[str, int]:
    text = _strip_comments((REPO / rel).read_text(encoding="utf-8"))
    counts = {
        "hex": len(HEX_RE.findall(text)),
        "rgb": len(RGB_RE.findall(text)),
        "dark_rules": sum(1 for line in text.splitlines() if DARK_RE.search(line)),
        "style_star": len(STYLE_STAR_RE.findall(text)),
        "inline_color": 0,
    }
    if not rel.endswith(".css"):
        # In markup/JS a colour declaration is an inline style (CSS files and
        # <style> blocks are covered by the hex/rgb counts).
        body = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S | re.I)
        counts["inline_color"] = len(INLINE_RE.findall(body))
    return counts


def measure_all() -> dict[str, dict[str, int]]:
    return {rel: measure(rel) for rel in _files()}


def _load_baseline() -> dict[str, dict[str, int]]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))["files"]


def test_baseline_is_well_formed():
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert set(data) == {"_comment", "files"}
    for rel, counts in data["files"].items():
        assert set(counts) == set(METRICS), rel
        assert all(isinstance(v, int) and v >= 0 for v in counts.values()), rel


@pytest.mark.parametrize("rel", _files())
def test_color_debt_never_grows(rel):
    baseline = _load_baseline()
    now = measure(rel)
    if rel not in baseline:
        grown = {k: v for k, v in now.items() if v}
        assert not grown, (
            f"{rel} is a new UI file with hard-coded colours {grown}; use the tokens in "
            "static/tokens.css (var(--…)) instead")
        return
    grown = {k: (baseline[rel][k], v) for k, v in now.items() if v > baseline[rel][k]}
    assert not grown, (
        f"{rel}: colour debt grew {grown} (baseline, now). Use tokens from static/tokens.css; "
        "never add :root[data-theme=\"dark\"] overrides, [style*=] selectors or literal colours.")


def test_tokens_css_is_the_only_exemption():
    assert EXEMPT == {"vivarium_workbench/static/tokens.css"}


def test_new_theme_files_are_colour_free():
    # Files introduced by the theme/extension work start (and stay) at zero.
    for rel in ("vivarium_workbench/static/theme.js", "vivarium_workbench/static/settings.js",
                "vivarium_workbench/static/sidepanel.js", "vivarium_workbench/static/dialog.js",
                "vivarium_workbench/templates/_theme_boot.html"):
        assert measure(rel) == dict.fromkeys(METRICS, 0), rel


def _write() -> int:
    current = measure_all()
    old = _load_baseline() if BASELINE.exists() else {}
    for rel, counts in current.items():
        if rel in old:
            raised = {k: v for k, v in counts.items() if v > old[rel][k]}
        elif old:
            # Not listed = colour-free (or new): it may not start carrying debt.
            raised = {k: v for k, v in counts.items() if v}
        else:
            raised = {}
        if raised:
            print(f"refusing to raise the baseline for {rel}: {raised}", file=sys.stderr)
            return 1
    data = {
        "_comment": "Shrink-only colour-debt counts per UI file; see tests/test_theme_ratchet.py.",
        "files": {rel: counts for rel, counts in current.items() if any(counts.values())},
    }
    BASELINE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {BASELINE.relative_to(REPO)} ({len(data['files'])} files)")
    return 0


if __name__ == "__main__":
    if "--write" in sys.argv:
        raise SystemExit(_write())
    print(json.dumps(measure_all(), indent=2))
