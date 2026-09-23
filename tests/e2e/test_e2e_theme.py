"""Theme acceptance in a real browser (plan §17.3, §23.1 T1-T7, T12, T13)."""
from __future__ import annotations

import re

import pytest

from .e2e_support import COMPOSITE, Workbench, hermetic, new_context

# preference stored -> OS scheme -> expected resolved theme
MATRIX = [
    ("light", "light", "light"), ("light", "dark", "light"),
    ("dark", "light", "dark"), ("dark", "dark", "dark"),
    ("system", "light", "light"), ("system", "dark", "dark"),
    (None, "light", "light"), (None, "dark", "dark"),          # no choice -> default 'system'
    ("blue", "dark", "dark"),                                   # invalid -> default 'system'
]
PAGES = ["/", "/studies/growth-partial", "/bigraph-loom/index.html"]


def _wait_theme(page, expected: str, timeout: int = 5000) -> None:
    page.wait_for_function("t => document.documentElement.getAttribute('data-theme') === t",
                           arg=expected, timeout=timeout)


@pytest.mark.parametrize("path", PAGES)
@pytest.mark.parametrize("stored,os_scheme,expected", MATRIX)
def test_theme_is_right_before_first_paint(browser, workbench: Workbench, path, stored, os_scheme, expected):
    """T1-T3, T7, T13: data-theme is already correct when <body>/the first
    stylesheet is inserted, on every HTML entry point."""
    ctx = new_context(browser, workbench, os_scheme=os_scheme, stored=stored)
    page = ctx.new_page()
    page.goto(workbench.base + path, wait_until="domcontentloaded")
    first = page.evaluate("window.__vivFirstPaint")
    assert first is not None, "probe did not fire"
    assert first["theme"] == expected, (path, stored, os_scheme, first)
    want_pref = stored if stored in ("light", "dark", "system") else "system"
    assert first["pref"] == want_pref
    color_scheme = page.evaluate("document.documentElement.style.colorScheme")
    assert color_scheme == expected
    ctx.close()


@pytest.mark.parametrize("path", PAGES)
def test_system_preference_follows_os_live(browser, workbench, path):
    """T4: with 'system', an OS switch re-themes the open document without a reload."""
    ctx = new_context(browser, workbench, os_scheme="light", stored="system")
    page = ctx.new_page()
    page.goto(workbench.base + path, wait_until="domcontentloaded")
    page.evaluate("window.__e2eNoReload = 1")
    _wait_theme(page, "light")
    page.emulate_media(color_scheme="dark")
    _wait_theme(page, "dark")
    page.emulate_media(color_scheme="light")
    _wait_theme(page, "light")
    assert page.evaluate("window.__e2eNoReload") == 1, "the page reloaded"
    ctx.close()


def test_explicit_choice_ignores_os(browser, workbench):
    """T5."""
    ctx = new_context(browser, workbench, os_scheme="light", stored="dark")
    page = ctx.new_page()
    page.goto(workbench.base + "/", wait_until="domcontentloaded")
    _wait_theme(page, "dark")
    page.emulate_media(color_scheme="dark")
    page.emulate_media(color_scheme="light")
    page.wait_for_timeout(300)
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
    ctx.close()


def test_choice_persists_across_reload_tabs_and_documents(browser, workbench):
    """T6 (same origin): the choice is stored in localStorage + cookie and
    propagates live to other tabs and to the loom document."""
    ctx = new_context(browser, workbench, os_scheme="light")
    a = ctx.new_page()
    a.goto(workbench.base + "/", wait_until="domcontentloaded")
    b = ctx.new_page()
    b.goto(workbench.base + "/studies/growth-accepted", wait_until="domcontentloaded")
    c = ctx.new_page()
    c.goto(workbench.base + "/bigraph-loom/index.html", wait_until="domcontentloaded")
    for p in (a, b, c):
        _wait_theme(p, "light")
    a.evaluate("window.vivTheme.setPreference('dark')")
    for p in (a, b, c):
        _wait_theme(p, "dark")
    assert a.evaluate("localStorage.getItem('viv.theme')") == "dark"
    cookies = {ck["name"]: ck for ck in ctx.cookies(workbench.base)}
    assert cookies["viv_theme"]["value"] == "dark"
    assert cookies["viv_theme"]["sameSite"] == "Lax"
    assert not cookies["viv_theme"]["secure"], "Secure only on HTTPS"
    a.reload(wait_until="domcontentloaded")
    assert a.evaluate("window.__vivFirstPaint.theme") == "dark"
    ctx.close()


def test_choice_survives_a_restart_on_a_new_port(browser, workbench, e2e_ws, e2e_root):
    """T6: a new port is a new origin (empty localStorage); the host cookie
    carries the preference to the first paint."""
    ctx = new_context(browser, workbench, os_scheme="light")
    page = ctx.new_page()
    page.goto(workbench.base + "/", wait_until="domcontentloaded")
    page.evaluate("window.vivTheme.setPreference('dark')")
    other = Workbench(ws=e2e_ws, home=e2e_root / "home-restart").start()
    try:
        assert other.port != workbench.port
        # the second server is another origin: allow it through the hermetic router
        ctx.unroute("**/*")
        hermetic(ctx, [workbench.base, other.base])
        p2 = ctx.new_page()
        p2.goto(other.base + "/", wait_until="domcontentloaded")
        assert p2.evaluate("localStorage.getItem('viv.theme')") is None, "fresh origin"
        first = p2.evaluate("window.__vivFirstPaint")
        assert first["theme"] == "dark" and first["pref"] == "dark", first
    finally:
        other.stop()
        ctx.close()


def test_back_compat_globals(browser, workbench):
    """T13: window._setTheme / window._toggleTheme keep working."""
    ctx = new_context(browser, workbench, os_scheme="light")
    page = ctx.new_page()
    page.goto(workbench.base + "/", wait_until="domcontentloaded")
    page.evaluate("window._setTheme('dark')")
    _wait_theme(page, "dark")
    assert page.evaluate("localStorage.getItem('viv.theme')") == "dark"
    page.evaluate("window._toggleTheme()")
    _wait_theme(page, "light")
    assert page.evaluate("localStorage.getItem('viv.theme')") == "light"
    ctx.close()


def test_rail_theme_menu_by_keyboard(browser, workbench):
    """Menu button with three menuitemradio entries: arrows move, Enter
    selects, Escape closes and returns focus (plan §4.10, §17.4)."""
    ctx = new_context(browser, workbench, os_scheme="light")
    page = ctx.new_page()
    page.goto(workbench.base + "/", wait_until="load")
    btn = page.locator("#viv-theme-menu-btn")
    assert btn.get_attribute("aria-haspopup") == "menu"
    btn.focus()
    page.keyboard.press("Enter")
    menu = page.locator("#viv-theme-menu-list")
    menu.wait_for(state="visible")
    assert btn.get_attribute("aria-expanded") == "true"
    items = menu.locator("[role=menuitemradio]")
    assert items.count() == 3
    checked = [items.nth(i).get_attribute("aria-checked") for i in range(3)]
    assert checked.count("true") == 1
    # focus lands on the checked item (System by default); move to Dark
    labels = [items.nth(i).inner_text().strip().lower() for i in range(3)]
    dark_index = next(i for i, t in enumerate(labels) if t.startswith("dark"))
    active = page.evaluate("document.activeElement.getAttribute('role')")
    assert active == "menuitemradio"
    for _ in range(3):
        if page.evaluate("document.activeElement.textContent.trim().toLowerCase()").startswith("dark"):
            break
        page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    _wait_theme(page, "dark")
    assert items.nth(dark_index).get_attribute("aria-checked") == "true"
    assert page.evaluate("document.activeElement.id") == "viv-theme-menu-btn", "focus returns to the button"
    # Escape closes without changing anything
    page.keyboard.press("Enter")
    menu.wait_for(state="visible")
    page.keyboard.press("Escape")
    menu.wait_for(state="hidden")
    assert page.evaluate("document.activeElement.id") == "viv-theme-menu-btn"
    _wait_theme(page, "dark")
    ctx.close()


def test_settings_radio_group(browser, workbench):
    """The authoritative control: a native radio group with a live
    '(currently …)' note; changes apply instantly (plan §4.10)."""
    ctx = new_context(browser, workbench, os_scheme="dark")
    page = ctx.new_page()
    page.goto(workbench.base + "/#settings", wait_until="load")
    system = page.get_by_role("radio", name=re.compile(r"^System"))
    system.wait_for()
    assert system.is_checked()
    assert page.locator("#page-settings fieldset legend").first.inner_text().strip() == "Theme"
    assert "currently Dark" in page.locator("#page-settings").inner_text()
    light = page.get_by_role("radio", name="Light", exact=True)
    dark = page.get_by_role("radio", name="Dark", exact=True)
    light.check()
    _wait_theme(page, "light")
    assert page.evaluate("localStorage.getItem('viv.theme')") == "light"
    # keyboard: arrow keys move the selection within the native group
    light.focus()
    page.keyboard.press("ArrowDown")
    _wait_theme(page, "dark")
    assert dark.is_checked()
    ctx.close()


def test_loom_color_mode_follows_theme(browser, workbench):
    """T12: the embedded loom gives React Flow the resolved theme and follows
    a change made in another document."""
    ctx = new_context(browser, workbench, os_scheme="light", stored="dark")
    loom = ctx.new_page()
    loom.goto(f"{workbench.base}/bigraph-loom/index.html?id={COMPOSITE}", wait_until="load")
    loom.wait_for_selector(".react-flow", timeout=30000)
    loom.wait_for_function("document.querySelector('.react-flow').classList.contains('dark')")
    shell = ctx.new_page()
    shell.goto(workbench.base + "/", wait_until="domcontentloaded")
    shell.evaluate("window.vivTheme.setPreference('light')")
    loom.wait_for_function("!document.querySelector('.react-flow').classList.contains('dark')")
    assert "light" in loom.evaluate("document.querySelector('.react-flow').className")
    ctx.close()


def test_generated_viz_document_follows_theme(browser, workbench, e2e_ws):
    """T12: a generated comparative viz document (shown in an iframe in the
    app) uses the theme tokens and re-themes live."""
    from vivarium_workbench.lib.comparative_viz import render_comparative_time_series
    from vivarium_workbench.lib.theme_embed import token_values

    render_comparative_time_series(runs=[{"label": "a"}], observable_path="agents.x", title="viz",
                                   y_label="y", output_path=e2e_ws / "reports" / "e2e-viz.html")
    light, dark = token_values(["--surface"])
    ctx = new_context(browser, workbench, os_scheme="light", stored="dark")
    page = ctx.new_page()
    resp = page.goto(workbench.base + "/reports/e2e-viz.html", wait_until="domcontentloaded")
    assert resp is not None and resp.ok, resp and resp.status

    def bg() -> str:
        return page.evaluate("getComputedStyle(document.body).backgroundColor")

    def rgb(hexv: str) -> str:
        h = hexv.lstrip("#")
        return "rgb(%d, %d, %d)" % (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    assert bg() == rgb(dark["--surface"])
    other = ctx.new_page()
    other.goto(workbench.base + "/", wait_until="domcontentloaded")
    other.evaluate("window.vivTheme.setPreference('light')")
    page.wait_for_function("t => document.documentElement.getAttribute('data-theme') === t", arg="light")
    assert bg() == rgb(light["--surface"])
    ctx.close()


def test_modal_dialog_semantics(browser, workbench):
    """Shared modal helper: role=dialog + aria-modal, focus moves in, Tab is
    trapped, Escape closes and focus returns to the opener (plan §5, 2d)."""
    ctx = new_context(browser, workbench, os_scheme="light")
    page = ctx.new_page()
    page.goto(workbench.base + "/#investigations", wait_until="load")
    page.evaluate("""() => {
        const b = document.createElement('button'); b.id = 'e2e-opener'; b.textContent = 'open';
        b.onclick = () => window.openModal('modal-investigation-create');
        document.body.appendChild(b);
    }""")
    page.locator("#e2e-opener").focus()
    page.keyboard.press("Enter")
    box = page.locator("#modal-investigation-create .modal-box")
    box.wait_for(state="visible")
    assert box.get_attribute("role") == "dialog"
    assert box.get_attribute("aria-modal") == "true"
    assert box.get_attribute("aria-labelledby")
    assert page.evaluate("document.querySelector('#modal-investigation-create').contains(document.activeElement)")
    for _ in range(12):
        page.keyboard.press("Tab")
        assert page.evaluate("document.querySelector('#modal-investigation-create').contains(document.activeElement)"), \
            "focus escaped the dialog"
    page.keyboard.press("Escape")
    page.locator("#modal-investigation-create").wait_for(state="hidden")
    assert page.evaluate("document.activeElement.id") == "e2e-opener"
    ctx.close()
