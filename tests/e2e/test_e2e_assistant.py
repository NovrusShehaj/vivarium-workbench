"""The assistant panel in a real browser against a fake local model (plan §17.3).

The model is ``tests/assistant/fake_llm.py``: a real HTTP server on 127.0.0.1
that the WORKBENCH SERVER calls (the browser never talks to a provider; the
page's hermetic router would abort it if it tried). No live provider is used.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .e2e_support import Workbench, free_port, new_context

from assistant.fake_llm import FakeLLM, Scripted, openai_text_events

CHUNKS = [f"word{i} " for i in range(14)]


@pytest.fixture(scope="module")
def fake_llm():
    fake = FakeLLM().start()
    yield fake
    fake.stop()


def _api(wb: Workbench, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(wb.base + "/api/ext/assistant" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Origin": wb.base})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


@pytest.fixture(scope="module")
def local_provider(workbench, fake_llm):
    existing = _api(workbench, "GET", "/providers").get("providers", [])
    for p in existing:
        if p.get("id") == "e2e-local":
            return p
    return _api(workbench, "POST", "/providers", {
        "id": "e2e-local", "type": "openai_compatible", "preset": "custom", "display_name": "E2E local",
        "base_url": fake_llm.base + "/v1", "credential": {"source": "none"}, "default_model": "fake-model"})["provider"]


def _open_panel(page):
    toggle = page.locator("[data-viv-sidepanel-toggle='assistant']")
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    page.locator("#viv-sidepanel textarea.asst-input").wait_for(state="visible")


def _choose_model(page, provider_name: str, model: str) -> None:
    chip = page.locator("#viv-sidepanel .asst-chip")
    chip.wait_for()
    page.wait_for_function("() => !/No provider configured/.test(document.querySelector('#viv-sidepanel .asst-chip').textContent)")
    chip.click()
    menu = page.locator("#viv-sidepanel .asst-model-menu")
    menu.wait_for(state="visible")
    group = menu.locator(".asst-model-group", has_text=provider_name)
    group.locator(".asst-model-option", has_text=model).first.click()
    page.wait_for_function("m => document.querySelector('#viv-sidepanel .asst-chip').textContent.indexOf(m) !== -1",
                           arg=model)


def test_settings_add_provider_test_connection_and_choose_model(browser, workbench, fake_llm):
    ctx = new_context(browser, workbench, os_scheme="light", stored="light")
    page = ctx.new_page()
    page.goto(workbench.base + "/#settings", wait_until="load")
    section = page.locator("#page-settings")
    section.get_by_text("Add a provider").click()
    section.get_by_label("Type").select_option("openai_compatible")
    section.get_by_label("Display name (optional)").fill("UI local")
    section.get_by_label("Preset").select_option("custom")
    section.get_by_label("Base URL").fill(fake_llm.base + "/v1")
    section.get_by_label("Credential").select_option("none")
    section.get_by_role("button", name="Add provider").click()
    card = section.locator(".asst-provider-card", has_text="UI local")
    card.wait_for()
    card.get_by_role("button", name="Test connection").click()
    out = card.locator(".asst-provider-out")
    out.wait_for()
    page.wait_for_function("el => /Connected/.test(el.textContent)", arg=out.element_handle(), timeout=15000)
    assert "2 models" in out.inner_text()
    # no secret-bearing field was ever needed for a local model; nothing in storage
    stored = page.evaluate("JSON.stringify(Object.assign({}, localStorage)) + JSON.stringify(Object.assign({}, sessionStorage))")
    assert "sk-" not in stored and "api_key" not in stored.lower()
    _open_panel(page)
    _choose_model(page, "UI local", "fake-model")
    assert re.search(r"Ready|Choose", page.locator("#viv-sidepanel .asst-status-text").inner_text())
    assert all(r["path"].startswith("/v1/") for r in fake_llm.requests), "only the server talks to the model"
    ctx.close()


def test_panel_open_close_shortcut_and_resize(browser, workbench, local_provider):
    ctx = new_context(browser, workbench, os_scheme="dark", stored="system", viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    page.goto(workbench.base + "/#modules", wait_until="load")
    panel = page.locator("#viv-sidepanel")
    toggle = page.locator("[data-viv-sidepanel-toggle='assistant']")
    assert panel.get_attribute("hidden") is not None
    toggle.click()
    panel.wait_for(state="visible")
    assert toggle.get_attribute("aria-expanded") == "true"
    toggle.click()
    panel.wait_for(state="hidden")
    # keyboard shortcut (Mod+Shift+.) opens it and focuses the composer
    page.locator("body").click()
    page.keyboard.press("Control+Shift+Period")
    panel.wait_for(state="visible")
    page.wait_for_function("() => document.activeElement && document.activeElement.classList.contains('asst-input')")
    # keyboard resize (±16px per arrow), persisted
    handle = page.locator("#viv-sidepanel-resize")
    assert handle.get_attribute("role") == "separator"
    w0 = int(handle.get_attribute("aria-valuenow"))
    handle.focus()
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowLeft")
    w1 = int(handle.get_attribute("aria-valuenow"))
    assert w1 == w0 + 32
    assert page.evaluate("localStorage.getItem('viv.sidepanel.width')") == str(w1)
    # pointer drag
    box = handle.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
    page.mouse.down()
    page.mouse.move(box["x"] - 40, box["y"] + 200, steps=4)
    page.mouse.up()
    w2 = int(handle.get_attribute("aria-valuenow"))
    assert w2 > w1
    page.reload(wait_until="load")
    page.locator("#viv-sidepanel").wait_for(state="visible")          # open state persisted too
    assert int(page.locator("#viv-sidepanel-resize").get_attribute("aria-valuenow")) == w2
    # the panel's own close button; focus goes back to the rail toggle
    page.locator("[data-viv-sidepanel-toggle='assistant']").focus()
    page.keyboard.press("Enter")
    page.locator("#viv-sidepanel").wait_for(state="hidden")
    page.keyboard.press("Enter")
    page.locator("#viv-sidepanel").wait_for(state="visible")
    page.locator("#viv-sidepanel").get_by_role("button", name="Close assistant").click()
    page.locator("#viv-sidepanel").wait_for(state="hidden")
    assert page.evaluate("document.activeElement.getAttribute('data-viv-sidepanel-toggle')") == "assistant"
    ctx.close()


def test_stream_stop_retry_regenerate(browser, workbench, fake_llm, local_provider):
    ctx = new_context(browser, workbench, os_scheme="light", stored="light")
    page = ctx.new_page()
    page.goto(workbench.base + "/#modules", wait_until="load")
    _open_panel(page)
    _choose_model(page, "E2E local", "fake-model")

    # 1. Streaming: text appears incrementally
    fake_llm.queue(Scripted(events=openai_text_events(*CHUNKS), delay_s=0.12))
    page.locator("#viv-sidepanel .asst-input").fill("Say fourteen words.")
    page.locator("#viv-sidepanel .asst-input").press("Enter")
    streaming = page.locator("#viv-sidepanel article.asst-msg-assistant").last
    lengths = page.evaluate("""() => new Promise(resolve => {
        const seen = []; const t0 = Date.now();
        const tick = () => {
          const a = [...document.querySelectorAll('#viv-sidepanel article.asst-msg-assistant')].pop();
          const n = a ? a.querySelector('.asst-msg-text').textContent.length : 0;
          if (!seen.length || seen[seen.length - 1] !== n) seen.push(n);
          if ((a && !a.classList.contains('asst-streaming') && n > 0) || Date.now() - t0 > 15000) resolve(seen);
          else setTimeout(tick, 40);
        };
        tick();
    })""")
    assert len([n for n in lengths if n > 0]) >= 3, f"text did not arrive incrementally: {lengths}"
    assert lengths == sorted(lengths)
    page.wait_for_function("() => ![...document.querySelectorAll('#viv-sidepanel article')].some(a => a.classList.contains('asst-streaming'))")
    assert "word13" in streaming.inner_text()

    # 2. Stop: partial text kept, marked stopped, upstream cancelled
    before = fake_llm.disconnects
    fake_llm.queue(Scripted(events=openai_text_events(*CHUNKS), delay_s=0.4))
    page.locator("#viv-sidepanel .asst-input").fill("Say it again, slowly.")
    page.locator("#viv-sidepanel .asst-input").press("Enter")
    page.wait_for_function("""() => { const a = [...document.querySelectorAll('#viv-sidepanel article.asst-msg-assistant')].pop();
                                      return a && a.classList.contains('asst-streaming') && /word1/.test(a.textContent); }""",
                           timeout=15000)
    page.locator("#viv-sidepanel").get_by_role("button", name="Stop").click()
    last = page.locator("#viv-sidepanel article.asst-msg-assistant").last
    last.locator(".asst-msg-state").wait_for()
    assert "Stopped" in last.locator(".asst-msg-state").inner_text()
    partial_text = last.locator(".asst-msg-text").inner_text()
    assert "word0" in partial_text and "word13" not in partial_text
    for _ in range(50):
        if fake_llm.disconnects > before:
            break
        page.wait_for_timeout(100)
    assert fake_llm.disconnects > before, "the upstream stream was not cancelled"

    # 3. Retry the stopped answer
    fake_llm.queue(Scripted(events=openai_text_events("retried ", "answer")))
    last.get_by_role("button", name="Retry").click()
    page.wait_for_function("""() => { const a = [...document.querySelectorAll('#viv-sidepanel article.asst-msg-assistant')].pop();
                                      return a && !a.classList.contains('asst-streaming') && /retried answer/.test(a.textContent); }""",
                           timeout=15000)

    # 4. Regenerate the latest answer
    fake_llm.queue(Scripted(events=openai_text_events("regenerated ", "answer")))
    page.locator("#viv-sidepanel article.asst-msg-assistant").last.get_by_role("button", name="Regenerate").click()
    page.wait_for_function("""() => { const a = [...document.querySelectorAll('#viv-sidepanel article.asst-msg-assistant')].pop();
                                      return a && !a.classList.contains('asst-streaming') && /regenerated answer/.test(a.textContent); }""",
                           timeout=15000)
    # the prompt the model received never contained browser-side secrets or a key header
    for r in fake_llm.posts():
        assert "authorization" not in r["headers"]
    ctx.close()


def test_snapshot_bundle_has_no_assistant_entry_points(browser, e2e_ws, e2e_root):
    out = e2e_root / "bundle"
    wb = Workbench(ws=e2e_ws, home=e2e_root / "home-publish")
    subprocess.run([sys.executable, "-m", "vivarium_workbench.publish", "--workspace", str(e2e_ws), "--out", str(out)],
                   check=True, env={**wb.env(), "VIVARIUM_WORKBENCH_EXTENSIONS": "assistant"},
                   capture_output=True, timeout=600)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "data-viv-sidepanel-toggle" not in html and 'id="viv-sidepanel"' not in html
    assert "/ext/assistant/" not in html and "data-viv-extension" not in html
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(SimpleHTTPRequestHandler, directory=str(out)))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        ctx = browser.new_context()
        from .e2e_support import hermetic
        hermetic(ctx, [base])
        page = ctx.new_page()
        page.goto(base + "/index.html", wait_until="load")
        assert page.locator("[data-viv-sidepanel-toggle]").count() == 0
        assert page.locator("#viv-sidepanel").count() == 0
        assert page.evaluate("typeof window.vivSidepanel") == "undefined"
        # the theme still works in the static bundle
        page.evaluate("window.vivTheme.setPreference('dark')")
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
        ctx.close()
    finally:
        server.shutdown()
        server.server_close()
