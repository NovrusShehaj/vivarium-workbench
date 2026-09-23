"""axe-core accessibility checks in both themes (plan §17.4, T9).

axe-core ships inside the ``axe-playwright-python`` package (MIT wrapper around
Deque's MPL-2.0 ``axe.min.js``, unmodified), installed by the ``e2e`` extra.
Every WCAG 2.0/2.1/2.2 A and AA rule is run; the audited pages must have no
violations at all, in the light and the dark theme.
"""
from __future__ import annotations

import pytest

from .e2e_support import Workbench, new_context

WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]

PAGES = [
    ("registry", "/#modules", None),
    ("catalog", "/#market", None),
    ("studies", "/#investigations", None),
    ("investigation", "/?investigation=e2e-inv", None),
    ("study", "/studies/growth-partial", None),
    ("study-tests", "/studies/growth-partial", "tests"),
    ("settings", "/#settings", None),
    ("about", "/#about", None),
]


def _axe():
    mod = pytest.importorskip("axe_playwright_python.sync_playwright")
    return mod.Axe()


def _violations(page, context=None) -> list[dict]:
    res = _axe().run(page, context=context,
                     options={"runOnly": {"type": "tag", "values": WCAG_TAGS}, "resultTypes": ["violations"]})
    return [{"id": v["id"], "impact": v["impact"], "help": v["help"],
             "nodes": [(n["target"], (n.get("failureSummary") or "")[:200]) for n in v["nodes"][:5]]}
            for v in res.response["violations"]]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("name,path,tab", PAGES, ids=[p[0] for p in PAGES])
def test_page_has_no_wcag_violations(browser, workbench: Workbench, theme, name, path, tab):
    ctx = new_context(browser, workbench, os_scheme="light", stored=theme)
    page = ctx.new_page()
    page.goto(workbench.base + path, wait_until="load")
    page.wait_for_timeout(1200)          # client-rendered sections settle
    if tab:
        page.evaluate("t => window._setStudyTab && window._setStudyTab(t)", tab)
        page.wait_for_timeout(600)
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == theme
    found = _violations(page)
    assert not found, f"{theme} {name}: {found}"
    ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme_menu_and_assistant_panel_have_no_wcag_violations(browser, workbench, theme):
    """The new chrome in its open states: the rail theme menu and the
    assistant side panel (header, model bar, composer)."""
    ctx = new_context(browser, workbench, os_scheme="light", stored=theme)
    page = ctx.new_page()
    page.goto(workbench.base + "/#modules", wait_until="load")
    page.locator("#viv-theme-menu-btn").click()
    page.locator("#viv-theme-menu-list").wait_for(state="visible")
    found = _violations(page, context="#viv-theme-menu-list")
    assert not found, f"{theme} theme menu: {found}"
    page.keyboard.press("Escape")
    page.locator("[data-viv-sidepanel-toggle='assistant']").click()
    page.locator("#viv-sidepanel textarea").wait_for(state="visible")
    found = _violations(page, context="#viv-sidepanel")
    assert not found, f"{theme} assistant panel: {found}"
    found = _violations(page)
    assert not found, f"{theme} page with the panel open: {found}"
    ctx.close()
