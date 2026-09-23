// theme.js — the workbench theme runtime (three-state preference).
//
// Preference (what the user chose, persisted):  'system' | 'light' | 'dark'
// Resolved   (what is painted right now):        'light' | 'dark'
//
// The pre-paint boot (templates/_theme_boot.html, included first in every
// shell's <head> and injected into the loom viewer) has already set
//   <html data-theme="<resolved>" data-theme-pref="<preference>">
// before this file runs. This module is the single runtime authority after
// that: it follows the OS while the preference is 'system', propagates changes
// made in other documents (other tabs, the same-origin study iframe, the loom
// iframe, pop-out windows) through the `storage` event, persists choices in
// localStorage AND a host-scoped cookie (the server binds a random port per
// launch, so origin-scoped localStorage alone would forget the choice on every
// restart), and announces every effective change as a `viv:themechange` event.
//
// Public API: window.vivTheme = { getPreference, getResolved, setPreference,
//   subscribe, resolve, token, plotlyLayout, applyToPlotly, syncThemedImages }
// Back-compat globals kept for existing callers: window._setTheme(t),
// window._toggleTheme() (toggle = set the explicit opposite of the resolved
// theme).
//
// Loaded as a classic blocking <script> in <head>; also require()-able under
// Node for tests/js/test_theme.js (dual export like static/session.js).
(function (root) {
  'use strict';

  var KEY = 'viv.theme';
  var COOKIE = 'viv_theme';
  var PREFS = ['system', 'light', 'dark'];
  // MUST equal D in templates/_theme_boot.html (tests/test_theme_boot.py).
  // 'system' since the Phase 2a dark-surface audit: users who never chose a
  // theme follow the OS; an explicit Light/Dark (incl. the legacy toggle's
  // stored values) is always honoured.
  var DEFAULT_PREFERENCE = 'system';
  var EVENT = 'viv:themechange';
  var COOKIE_MAX_AGE = 31536000;  // one year
  var COOKIE_RE = /(?:^|;\s*)viv_theme=(system|light|dark)(?:;|$)/;

  var doc = root.document;
  var subscribers = [];
  var last = null;  // { preference, resolved } most recently applied

  var mql = null;
  try {
    mql = typeof root.matchMedia === 'function'
      ? root.matchMedia('(prefers-color-scheme: dark)') : null;
  } catch (e) { mql = null; }

  function isPreference(v) { return PREFS.indexOf(v) !== -1; }

  function normalize(v) { return isPreference(v) ? v : DEFAULT_PREFERENCE; }

  // Pure: (preference, system prefers dark?) -> resolved theme.
  function resolve(preference, systemPrefersDark) {
    var p = normalize(preference);
    return (p === 'dark' || (p === 'system' && !!systemPrefersDark)) ? 'dark' : 'light';
  }

  function systemPrefersDark() { return !!(mql && mql.matches); }

  function readCookie() {
    try {
      var m = COOKIE_RE.exec((doc && doc.cookie) || '');
      return m ? m[1] : null;
    } catch (e) { return null; }
  }

  // The stored preference (localStorage first, then the host cookie), or null.
  function readStored() {
    var v = null;
    try { v = root.localStorage ? root.localStorage.getItem(KEY) : null; } catch (e) { v = null; }
    if (isPreference(v)) return v;
    var c = readCookie();
    return isPreference(c) ? c : null;
  }

  function writeStored(p) {
    try { if (root.localStorage) root.localStorage.setItem(KEY, p); } catch (e) { /* private mode */ }
    try {
      var secure = (root.location && root.location.protocol === 'https:') ? '; Secure' : '';
      doc.cookie = COOKIE + '=' + p + '; Path=/; Max-Age=' + COOKIE_MAX_AGE + '; SameSite=Lax' + secure;
    } catch (e) { /* cookies disabled */ }
  }

  function getPreference() { return readStored() || DEFAULT_PREFERENCE; }

  function getResolved() { return resolve(getPreference(), systemPrefersDark()); }

  function nextFrame(fn) {
    if (typeof root.requestAnimationFrame === 'function') {
      root.requestAnimationFrame(function () { root.requestAnimationFrame(fn); });
    } else {
      setTimeout(fn, 0);
    }
  }

  // Swap <img data-light-src data-dark-src> sources (the rail logo and any
  // other theme-specific image).
  function syncThemedImages(resolved) {
    if (!doc || typeof doc.querySelectorAll !== 'function') return;
    var r = resolved || getResolved();
    var imgs = doc.querySelectorAll('img[data-light-src][data-dark-src]');
    for (var i = 0; i < imgs.length; i++) {
      var img = imgs[i];
      var next = r === 'dark' ? img.getAttribute('data-dark-src') : img.getAttribute('data-light-src');
      if (next && img.getAttribute('src') !== next) img.setAttribute('src', next);
    }
  }

  function notify(detail) {
    var list = subscribers.slice();
    for (var i = 0; i < list.length; i++) {
      try { list[i](detail); } catch (e) { /* one bad subscriber must not break the rest */ }
    }
    try {
      if (typeof root.dispatchEvent === 'function' && typeof root.CustomEvent === 'function') {
        root.dispatchEvent(new root.CustomEvent(EVENT, { detail: detail }));
      }
    } catch (e) { /* old browsers */ }
  }

  function apply(preference) {
    var p = normalize(preference);
    var resolved = resolve(p, systemPrefersDark());
    var el = doc && doc.documentElement;
    if (!el) return;
    var changed = !last || last.preference !== p || last.resolved !== resolved;
    if (changed && last && el.classList) {
      // Suppress transitions for a frame so the swap does not "crawl".
      el.classList.add('viv-theme-switching');
      nextFrame(function () { el.classList.remove('viv-theme-switching'); });
    }
    el.setAttribute('data-theme', resolved);
    el.setAttribute('data-theme-pref', p);
    if (el.style) el.style.colorScheme = resolved;
    last = { preference: p, resolved: resolved };
    syncThemedImages(resolved);
    if (changed) notify({ preference: p, resolved: resolved });
  }

  function setPreference(preference) {
    var p = normalize(preference);
    writeStored(p);
    apply(p);
  }

  function subscribe(fn) {
    if (typeof fn !== 'function') return function () {};
    subscribers.push(fn);
    return function unsubscribe() {
      var i = subscribers.indexOf(fn);
      if (i !== -1) subscribers.splice(i, 1);
    };
  }

  // Current value of a CSS custom property on <html> (e.g. token('--chart-text')).
  function token(name) {
    try {
      if (!doc || !root.getComputedStyle) return '';
      return String(root.getComputedStyle(doc.documentElement).getPropertyValue(name) || '').trim();
    } catch (e) { return ''; }
  }

  // Plotly layout overrides for the resolved theme: transparent backgrounds so
  // the surrounding card surface shows through, and token-driven text/grid.
  // Without tokens.css on the page the colour keys are omitted, so Plotly keeps
  // its own defaults (no literal colours live here: tokens.css is the source).
  function plotlyLayout() {
    var text = token('--chart-text');
    var grid = token('--chart-grid');
    var axis = token('--chart-axis') || text;
    var layout = { paper_bgcolor: 'transparent', plot_bgcolor: 'transparent' };
    if (text) layout.font = { color: text };
    if (grid) layout.gridcolor = grid;
    if (axis) layout.axiscolor = axis;
    return layout;
  }

  // Re-theme one rendered Plotly graph div in place (flat dot-keys so axis
  // titles, ranges and other axis settings are preserved).
  function applyToPlotly(gd) {
    var P = root.Plotly;
    if (!P || typeof P.relayout !== 'function' || !gd || !gd.layout) return false;
    var t = plotlyLayout();
    var update = { paper_bgcolor: t.paper_bgcolor, plot_bgcolor: t.plot_bgcolor };
    var text = t.font && t.font.color;
    if (text) { update['font.color'] = text; update['legend.font.color'] = text; }
    var keys = Object.keys(gd.layout);
    var axes = keys.filter(function (k) { return /^[xy]axis\d*$/.test(k); });
    if (axes.length === 0) axes = ['xaxis', 'yaxis'];
    axes.forEach(function (ax) {
      if (t.gridcolor) { update[ax + '.gridcolor'] = t.gridcolor; update[ax + '.zerolinecolor'] = t.gridcolor; }
      if (t.axiscolor) { update[ax + '.linecolor'] = t.axiscolor; update[ax + '.tickcolor'] = t.axiscolor; }
      if (text) { update[ax + '.tickfont.color'] = text; update[ax + '.title.font.color'] = text; }
    });
    try { P.relayout(gd, update); return true; } catch (e) { return false; }
  }

  // Follow the OS while the preference is 'system'.
  if (mql) {
    var onOsChange = function () { if (getPreference() === 'system') apply('system'); };
    if (typeof mql.addEventListener === 'function') mql.addEventListener('change', onOsChange);
    else if (typeof mql.addListener === 'function') mql.addListener(onOsChange);
  }

  // Another same-origin document (tab, iframe, pop-out) changed the preference.
  if (typeof root.addEventListener === 'function') {
    root.addEventListener('storage', function (e) {
      if (!e || e.key === KEY || e.key === null) apply(getPreference());
    });
  }

  // Align with what the boot painted (and cover documents without the boot).
  apply(getPreference());

  var api = {
    KEY: KEY,
    COOKIE: COOKIE,
    EVENT: EVENT,
    DEFAULT_PREFERENCE: DEFAULT_PREFERENCE,
    PREFERENCES: PREFS.slice(),
    getPreference: getPreference,
    getResolved: getResolved,
    systemPrefersDark: systemPrefersDark,
    setPreference: setPreference,
    subscribe: subscribe,
    resolve: resolve,
    token: token,
    plotlyLayout: plotlyLayout,
    applyToPlotly: applyToPlotly,
    syncThemedImages: syncThemedImages,
  };

  root.vivTheme = api;
  // Back-compat: existing callers (and bookmarklets) use these globals.
  root._setTheme = function (t) { setPreference(t); };
  root._toggleTheme = function () { setPreference(getResolved() === 'dark' ? 'light' : 'dark'); };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
