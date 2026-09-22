"""Spine visibility contract for the study-detail page.

History: Thread-C / Task 3 (C1c) un-buried spine-critical content (purpose,
tests summary, multi-axis status, discovery implications). The page was later
fully redesigned (#713, "audit-grade, one narrative"), which re-authored that
markup: purpose renders as the promoted question headline, key assumptions and
pipeline gate are visible sections under "Plan & assumptions", and discovery
implications closes the narrative after the verdicts card. These tests pin the
POST-redesign contract.

Structural (no JS harness): assert the markup.
"""
from __future__ import annotations

from pathlib import Path

_PKG = Path(__file__).parent.parent / "vivarium_workbench"
_HTML = (_PKG / "templates" / "study-detail.html").read_text(encoding="utf-8")


def test_purpose_is_promoted_to_question_headline():
    """Purpose is no longer an Overview-panel h2 nor a <details>: the redesign
    promotes purpose.question (or the legacy top-level question) to the
    #study-question-headline element directly above the tab nav — the study's
    second line, the most visible slot on the page."""
    assert 'id="study-question-headline"' in _HTML, \
        "study-detail.html missing the promoted #study-question-headline"
    assert "study.purpose.question if study.purpose" in _HTML, \
        "headline must source purpose.question (v3) before the legacy question"


def test_discovery_implications_close_the_narrative_after_verdicts():
    """Discovery implications render as the page's closing forward-looking
    section ("where this study's results leave the mechanism model — and what
    to investigate next"), after the conclusion_verdicts card, in the #713
    one-narrative layout. (The C1c-era position — above the verdict form —
    was superseded by the redesign.)"""
    di = _HTML.index('id="discovery-implications-section"')
    verdicts = _HTML.index('data-narrative-card="conclusion_verdicts"')
    assert di > verdicts, "discovery implications must follow the verdicts card"


def test_secondary_content_is_visible_under_plan_and_assumptions():
    """Key assumptions + pipeline gate are no longer <details>-collapsed: the
    redesign renders them as visible overview-label sections under the
    "Plan & assumptions" heading. Only genuinely-secondary provenance
    ("Limitations & provenance") remains collapsed."""
    plan = _HTML.index("Plan &amp; assumptions")
    assumptions = _HTML.index('<h3 class="overview-label">Key assumptions</h3>')
    gate = _HTML.index('<h3 class="overview-label">Pipeline gate</h3>')
    assert plan < gate and plan < assumptions, \
        "key assumptions and pipeline gate must be visible sections under Plan & assumptions"
    # And neither may be inside a <details> drawer.
    collapsed = _HTML.rfind("<details", 0, assumptions)
    if collapsed != -1:
        assert "</details>" in _HTML[collapsed:assumptions], \
            "key assumptions must not sit inside a collapsed <details>"


