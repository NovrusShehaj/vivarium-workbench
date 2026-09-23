// sidepanel.js — the core right-side panel host (<aside id="viv-sidepanel">).
//
// Extension-agnostic: the template renders the host only when an enabled
// extension contributes a panel (lib/extensions.py `panel=True`), and the
// extension mounts its UI into #viv-sidepanel-body. This module owns the
// chrome every panel shares:
//   * open / close / toggle from any [data-viv-sidepanel-toggle] button
//     (aria-expanded kept in sync) or programmatically;
//   * a left-edge resize handle (role="separator") driven by pointer events
//     AND the keyboard (ArrowLeft/ArrowRight ±16px, Home/End = min/max) with
//     aria-valuenow/min/max, mirroring the left rail's resize handle;
//   * width + open state persisted in localStorage (viv.sidepanel.width,
//     viv.sidepanel.open) and exposed as the --sidepanel-w CSS variable;
//   * below 1100px it overlays the content (CSS) over a scrim that closes it;
//   * Escape inside the panel closes it (unless the extension handled the
//     key) and focus returns to whatever opened it.
//
// API: window.vivSidepanel = { open(id?), close(), toggle(id?), isOpen(),
//   body(), activeId(), setLabel(text), onChange(fn) -> unsubscribe }
// Emits `viv:sidepanel` (detail {open, id}) on window.
//
// Also require()-able under Node for tests/js/test_sidepanel.js (pure helpers).
(function (root) {
  'use strict';

  var WIDTH_KEY = 'viv.sidepanel.width';
  var OPEN_KEY = 'viv.sidepanel.open';
  var MIN_W = 320;
  var MAX_W = 720;
  var DEFAULT_W = 400;
  var STEP = 16;
  var EVENT = 'viv:sidepanel';

  // ── Pure helpers (unit-tested) ─────────────────────────────────────────

  function maxWidth(viewportWidth) {
    var vw = Number(viewportWidth) || 0;
    return Math.max(MIN_W, Math.min(Math.floor(vw * 0.5), MAX_W));
  }

  function clampWidth(w, viewportWidth) {
    var n = Math.round(Number(w));
    if (!isFinite(n) || n <= 0) n = DEFAULT_W;
    return Math.min(Math.max(n, MIN_W), maxWidth(viewportWidth));
  }

  // New width for a keyboard key on the (left-edge) handle, or null when the
  // key is not a resize key. Left widens (the handle moves left).
  function keyWidth(key, width, viewportWidth) {
    switch (key) {
      case 'ArrowLeft': return clampWidth(width + STEP, viewportWidth);
      case 'ArrowRight': return clampWidth(width - STEP, viewportWidth);
      case 'Home': return MIN_W;
      case 'End': return maxWidth(viewportWidth);
      default: return null;
    }
  }

  // ── State ───────────────────────────────────────────────────────────────

  var doc = root.document;
  var panel = null;
  var handle = null;
  var bodyEl = null;
  var scrim = null;
  var width = DEFAULT_W;
  var openState = false;
  var currentId = null;
  var opener = null;
  var listeners = [];

  function viewportWidth() { return root.innerWidth || (doc && doc.documentElement && doc.documentElement.clientWidth) || 1280; }

  function readStored(key) {
    try { return root.localStorage ? root.localStorage.getItem(key) : null; } catch (e) { return null; }
  }

  function writeStored(key, value) {
    try { if (root.localStorage) root.localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }

  function applyWidth(w, persist) {
    width = clampWidth(w, viewportWidth());
    if (panel) panel.style.setProperty('--sidepanel-w', width + 'px');
    if (handle) {
      handle.setAttribute('aria-valuenow', String(width));
      handle.setAttribute('aria-valuemin', String(MIN_W));
      handle.setAttribute('aria-valuemax', String(maxWidth(viewportWidth())));
    }
    if (persist) writeStored(WIDTH_KEY, String(width));
  }

  function syncToggles() {
    if (!doc) return;
    var btns = doc.querySelectorAll('[data-viv-sidepanel-toggle]');
    for (var i = 0; i < btns.length; i++) {
      var mine = openState && btns[i].getAttribute('data-viv-sidepanel-toggle') === currentId;
      btns[i].setAttribute('aria-expanded', mine ? 'true' : 'false');
      if (btns[i].classList) btns[i].classList.toggle('active', mine);
    }
  }

  function emit() {
    var detail = { open: openState, id: currentId };
    var list = listeners.slice();
    for (var i = 0; i < list.length; i++) {
      try { list[i](detail); } catch (e) { /* isolate listeners */ }
    }
    try {
      if (typeof root.CustomEvent === 'function') root.dispatchEvent(new root.CustomEvent(EVENT, { detail: detail }));
    } catch (e) { /* old browsers */ }
  }

  function defaultId() {
    if (!doc) return null;
    var b = doc.querySelector('[data-viv-sidepanel-toggle]');
    return b ? b.getAttribute('data-viv-sidepanel-toggle') : null;
  }

  function open(id) {
    if (!panel) return;
    var nextId = id || currentId || defaultId();
    var wasOpen = openState;
    if (!wasOpen) {
      var active = doc.activeElement;
      opener = (active && active !== doc.body && !(panel.contains && panel.contains(active))) ? active : null;
    }
    currentId = nextId;
    openState = true;
    panel.hidden = false;
    panel.setAttribute('data-sidepanel-id', currentId || '');
    if (scrim) scrim.hidden = false;
    if (doc.body && doc.body.classList) doc.body.classList.add('viv-sidepanel-open');
    applyWidth(width, false);
    writeStored(OPEN_KEY, '1');
    syncToggles();
    emit();
    // Move focus into the panel unless the extension already did (on emit).
    if (!(panel.contains && panel.contains(doc.activeElement)) && bodyEl) {
      if (!bodyEl.hasAttribute('tabindex')) bodyEl.setAttribute('tabindex', '-1');
      try { bodyEl.focus({ preventScroll: true }); } catch (e) { bodyEl.focus(); }
    }
  }

  function close() {
    if (!panel || !openState) return;
    var hadFocus = panel.contains && panel.contains(doc.activeElement);
    openState = false;
    panel.hidden = true;
    if (scrim) scrim.hidden = true;
    if (doc.body && doc.body.classList) doc.body.classList.remove('viv-sidepanel-open');
    writeStored(OPEN_KEY, '0');
    syncToggles();
    emit();
    if (hadFocus) {
      var target = opener && doc.contains && doc.contains(opener) ? opener : doc.querySelector('[data-viv-sidepanel-toggle]');
      if (target && typeof target.focus === 'function') target.focus();
    }
    opener = null;
  }

  function toggle(id) {
    if (openState && (!id || id === currentId)) close();
    else open(id);
  }

  function onChange(fn) {
    if (typeof fn !== 'function') return function () {};
    listeners.push(fn);
    return function () {
      var i = listeners.indexOf(fn);
      if (i !== -1) listeners.splice(i, 1);
    };
  }

  function setLabel(text) {
    if (panel && text) panel.setAttribute('aria-label', String(text));
  }

  // ── Resize ──────────────────────────────────────────────────────────────

  function wireResize() {
    if (!handle) return;
    var startX = 0;
    var startW = 0;
    var dragging = false;

    handle.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      dragging = true;
      startX = e.clientX;
      startW = width;
      try { handle.setPointerCapture(e.pointerId); } catch (err) { /* synthetic events */ }
      if (panel.classList) panel.classList.add('viv-sidepanel-resizing');
      if (doc.body && doc.body.classList) doc.body.classList.add('viv-sidepanel-resizing-active');
      e.preventDefault();
    });
    handle.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      applyWidth(startW + (startX - e.clientX), false);
    });
    var end = function (e) {
      if (!dragging) return;
      dragging = false;
      try { handle.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
      if (panel.classList) panel.classList.remove('viv-sidepanel-resizing');
      if (doc.body && doc.body.classList) doc.body.classList.remove('viv-sidepanel-resizing-active');
      applyWidth(width, true);
    };
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
    handle.addEventListener('dblclick', function () { applyWidth(DEFAULT_W, true); });
    handle.addEventListener('keydown', function (e) {
      var w = keyWidth(e.key, width, viewportWidth());
      if (w === null) return;
      e.preventDefault();
      applyWidth(w, true);
    });
  }

  // ── Init ────────────────────────────────────────────────────────────────

  function init() {
    panel = doc.getElementById('viv-sidepanel');
    if (!panel) return;
    handle = doc.getElementById('viv-sidepanel-resize');
    bodyEl = doc.getElementById('viv-sidepanel-body');

    scrim = doc.createElement('div');
    scrim.className = 'viv-sidepanel-scrim';
    scrim.hidden = true;
    scrim.setAttribute('aria-hidden', 'true');
    scrim.addEventListener('click', close);
    panel.parentNode.insertBefore(scrim, panel);

    var stored = parseInt(readStored(WIDTH_KEY) || '', 10);
    applyWidth(isFinite(stored) ? stored : DEFAULT_W, false);
    wireResize();

    doc.addEventListener('click', function (e) {
      var btn = e.target && e.target.closest ? e.target.closest('[data-viv-sidepanel-toggle]') : null;
      if (!btn) return;
      e.preventDefault();
      toggle(btn.getAttribute('data-viv-sidepanel-toggle'));
    });
    panel.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !e.defaultPrevented) {
        e.preventDefault();
        close();
      }
    });
    root.addEventListener('resize', function () { applyWidth(width, false); });

    syncToggles();
    if (readStored(OPEN_KEY) === '1') {
      // Restore without stealing focus from the page on load.
      currentId = defaultId();
      openState = true;
      panel.hidden = false;
      panel.setAttribute('data-sidepanel-id', currentId || '');
      scrim.hidden = false;
      if (doc.body && doc.body.classList) doc.body.classList.add('viv-sidepanel-open');
      syncToggles();
      emit();
    }
  }

  var api = {
    open: open,
    close: close,
    toggle: toggle,
    isOpen: function () { return openState; },
    body: function () { return bodyEl; },
    activeId: function () { return currentId; },
    setLabel: setLabel,
    onChange: onChange,
    // exposed for tests
    MIN_WIDTH: MIN_W,
    MAX_WIDTH: MAX_W,
    DEFAULT_WIDTH: DEFAULT_W,
    clampWidth: clampWidth,
    maxWidth: maxWidth,
    keyWidth: keyWidth,
  };

  root.vivSidepanel = api;

  if (doc && typeof doc.addEventListener === 'function' && typeof doc.getElementById === 'function') {
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
    else init();
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
