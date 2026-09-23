// tests/js/test_theme.js — run with: node tests/js/test_theme.js
//
// Exercises static/theme.js (the three-state theme runtime) and the pre-paint
// boot in templates/_theme_boot.html under Node with minimal browser fakes:
// documentElement attributes/classList/style, document.cookie, localStorage,
// matchMedia (with change listeners), window events, rAF, CustomEvent.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const THEME_JS = path.join(__dirname, '../../vivarium_workbench/static/theme.js');
const BOOT_HTML = path.join(__dirname, '../../vivarium_workbench/templates/_theme_boot.html');

function makeStore(initial, opts) {
  const m = Object.assign({}, initial || {});
  const throws = opts && opts.throws;
  return {
    getItem: (k) => { if (throws) throw new Error('SecurityError'); return k in m ? m[k] : null; },
    setItem: (k, v) => { if (throws) throw new Error('SecurityError'); m[k] = String(v); },
    removeItem: (k) => { delete m[k]; },
    _m: m,
  };
}

function makeClassList() {
  const s = new Set();
  return {
    add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c),
    toggle: (c, on) => { if (on === undefined ? !s.has(c) : on) s.add(c); else s.delete(c); },
  };
}

function makeEnv(o) {
  o = o || {};
  const attrs = {};
  const listeners = {};
  const mqlListeners = [];
  const frames = [];
  const cookieJar = { value: o.cookie || '', writes: [] };
  const images = o.images || [];
  const docEl = {
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    classList: makeClassList(),
    style: {},
  };
  const document = {
    documentElement: docEl,
    querySelectorAll: (sel) => (sel.indexOf('data-dark-src') >= 0 ? images : []),
  };
  Object.defineProperty(document, 'cookie', {
    get: () => cookieJar.value,
    set: (v) => { cookieJar.writes.push(v); const pair = v.split(';')[0]; cookieJar.value = pair; },
  });
  const mql = {
    matches: !!o.systemDark,
    addEventListener: (type, fn) => { if (type === 'change') mqlListeners.push(fn); },
  };
  function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; }
  const win = {
    document: document,
    localStorage: o.storage || makeStore(o.stored ? { 'viv.theme': o.stored } : {}),
    location: { protocol: o.protocol || 'http:' },
    matchMedia: o.noMatchMedia ? undefined : () => mql,
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    CustomEvent: CustomEvent,
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    dispatchEvent: (ev) => { (listeners[ev.type] || []).forEach((fn) => fn(ev)); return true; },
    getComputedStyle: () => ({ getPropertyValue: (n) => (o.tokens && o.tokens[n]) || '' }),
  };
  return {
    win, attrs, mql, mqlListeners, frames, cookieJar, listeners,
    flushFrames() { while (frames.length) frames.shift()(); },
    osChange(dark) { mql.matches = dark; mqlListeners.forEach((fn) => fn({ matches: dark })); },
  };
}

function load(o) {
  const env = makeEnv(o);
  global.window = env.win;
  delete require.cache[require.resolve(THEME_JS)];
  const api = require(THEME_JS);
  return { api, env };
}

// ---- pure resolve -----------------------------------------------------------
{
  const { api } = load();
  assert.strictEqual(api.resolve('dark', false), 'dark');
  assert.strictEqual(api.resolve('dark', true), 'dark');
  assert.strictEqual(api.resolve('light', true), 'light');
  assert.strictEqual(api.resolve('system', true), 'dark');
  assert.strictEqual(api.resolve('system', false), 'light');
  assert.strictEqual(api.resolve('blue', true), api.resolve(api.DEFAULT_PREFERENCE, true),
    'invalid preference resolves like the default');
  assert.deepStrictEqual(api.PREFERENCES, ['system', 'light', 'dark']);
}

// ---- first apply with nothing stored = default, attributes + colorScheme -----
{
  const { api, env } = load({ systemDark: true });
  assert.strictEqual(api.getPreference(), api.DEFAULT_PREFERENCE);
  assert.strictEqual(env.attrs['data-theme-pref'], api.DEFAULT_PREFERENCE);
  assert.strictEqual(env.attrs['data-theme'], api.resolve(api.DEFAULT_PREFERENCE, true));
  assert.strictEqual(env.win.document.documentElement.style.colorScheme, env.attrs['data-theme']);
  assert.strictEqual(env.cookieJar.writes.length, 0, 'reading never writes a cookie');
}

// ---- setPreference persists (localStorage + cookie), applies, notifies ------
{
  const { api, env } = load();
  const seen = [];
  const events = [];
  const unsub = api.subscribe((d) => seen.push(d));
  env.win.addEventListener('viv:themechange', (e) => events.push(e.detail));
  api.setPreference('dark');
  assert.strictEqual(env.win.localStorage.getItem('viv.theme'), 'dark');
  const cookie = env.cookieJar.writes[env.cookieJar.writes.length - 1];
  assert.strictEqual(cookie, 'viv_theme=dark; Path=/; Max-Age=31536000; SameSite=Lax');
  assert.ok(!/HttpOnly/i.test(cookie), 'boot script must be able to read the cookie');
  assert.strictEqual(env.attrs['data-theme'], 'dark');
  assert.strictEqual(env.attrs['data-theme-pref'], 'dark');
  assert.deepStrictEqual(seen, [{ preference: 'dark', resolved: 'dark' }]);
  assert.deepStrictEqual(events, [{ preference: 'dark', resolved: 'dark' }]);
  // no-op re-apply does not re-notify
  api.setPreference('dark');
  assert.strictEqual(seen.length, 1, 'unchanged state is not re-announced');
  unsub();
  api.setPreference('light');
  assert.strictEqual(seen.length, 1, 'unsubscribed listener is not called');
  assert.strictEqual(events.length, 2);
  // invalid input is normalized to the default
  api.setPreference('blue');
  assert.strictEqual(env.win.localStorage.getItem('viv.theme'), api.DEFAULT_PREFERENCE);
}

// ---- Secure cookie over https -------------------------------------------------
{
  const { api, env } = load({ protocol: 'https:' });
  api.setPreference('light');
  assert.ok(/; Secure$/.test(env.cookieJar.writes.pop()), 'Secure on https');
}

// ---- cookie is the fallback when localStorage is empty (new port) -----------
{
  const { api, env } = load({ cookie: 'other=1; viv_theme=system', systemDark: true });
  assert.strictEqual(api.getPreference(), 'system');
  assert.strictEqual(env.attrs['data-theme'], 'dark');
}

// ---- localStorage throwing (private mode) falls back to the cookie ----------
{
  const { api, env } = load({ storage: makeStore({}, { throws: true }), cookie: 'viv_theme=dark' });
  assert.strictEqual(api.getPreference(), 'dark');
  assert.doesNotThrow(() => api.setPreference('light'));
  assert.strictEqual(env.attrs['data-theme'], 'light');
}

// ---- invalid stored value is treated as default and NOT rewritten on read ----
{
  const { api, env } = load({ stored: 'blue' });
  assert.strictEqual(api.getPreference(), api.DEFAULT_PREFERENCE);
  assert.strictEqual(env.win.localStorage.getItem('viv.theme'), 'blue');
}

// ---- legacy explicit values are honoured as-is --------------------------------
{
  const { api, env } = load({ stored: 'dark', systemDark: false });
  assert.strictEqual(api.getPreference(), 'dark');
  assert.strictEqual(env.attrs['data-theme'], 'dark');
}

// ---- 'system' follows the OS live; explicit choices ignore it ---------------
{
  const { api, env } = load({ stored: 'system', systemDark: false });
  const seen = [];
  api.subscribe((d) => seen.push(d.resolved));
  assert.strictEqual(env.attrs['data-theme'], 'light');
  env.osChange(true);
  assert.strictEqual(env.attrs['data-theme'], 'dark');
  assert.strictEqual(env.attrs['data-theme-pref'], 'system');
  env.osChange(false);
  assert.strictEqual(env.attrs['data-theme'], 'light');
  assert.deepStrictEqual(seen, ['dark', 'light']);
  api.setPreference('dark');
  env.osChange(false);
  assert.strictEqual(env.attrs['data-theme'], 'dark', 'explicit dark ignores the OS');
}

// ---- matchMedia addListener fallback (old Safari) ----------------------------
{
  const env = makeEnv({ stored: 'system' });
  const legacy = [];
  env.win.matchMedia = () => ({ matches: false, addListener: (fn) => legacy.push(fn) });
  global.window = env.win;
  delete require.cache[require.resolve(THEME_JS)];
  require(THEME_JS);
  assert.strictEqual(legacy.length, 1, 'falls back to addListener');
}

// ---- no matchMedia at all: system resolves light, nothing throws -------------
{
  const { api, env } = load({ stored: 'system', noMatchMedia: true });
  assert.strictEqual(api.getResolved(), 'light');
  assert.strictEqual(env.attrs['data-theme'], 'light');
}

// ---- another document changed the preference (storage event) ----------------
{
  const { api, env } = load({ stored: 'light' });
  env.win.localStorage._m['viv.theme'] = 'dark';           // written by the parent/other tab
  env.win.dispatchEvent({ type: 'storage', key: 'viv.theme' });
  assert.strictEqual(env.attrs['data-theme'], 'dark');
  env.win.localStorage._m['viv.theme'] = 'light';
  env.win.dispatchEvent({ type: 'storage', key: 'unrelated' });
  assert.strictEqual(env.attrs['data-theme'], 'dark', 'unrelated keys are ignored');
  env.win.dispatchEvent({ type: 'storage', key: null });   // storage.clear()
  assert.strictEqual(env.attrs['data-theme'], 'light');
  assert.strictEqual(api.getPreference(), 'light');
}

// ---- transitions are suppressed for a frame on change (not on first paint) --
{
  const { api, env } = load({ stored: 'light' });
  const cl = env.win.document.documentElement.classList;
  assert.ok(!cl.contains('viv-theme-switching'), 'initial apply does not add the class');
  api.setPreference('dark');
  assert.ok(cl.contains('viv-theme-switching'));
  env.flushFrames();
  env.flushFrames();
  assert.ok(!cl.contains('viv-theme-switching'), 'removed after the next frames');
}

// ---- a throwing subscriber does not break the others -------------------------
{
  const { api } = load();
  const seen = [];
  api.subscribe(() => { throw new Error('bad'); });
  api.subscribe((d) => seen.push(d.resolved));
  api.setPreference('dark');
  assert.deepStrictEqual(seen, ['dark']);
}

// ---- back-compat globals ------------------------------------------------------
{
  const { env } = load({ stored: 'system', systemDark: true });
  assert.strictEqual(typeof env.win._setTheme, 'function');
  assert.strictEqual(typeof env.win._toggleTheme, 'function');
  env.win._toggleTheme();   // resolved dark -> explicit light
  assert.strictEqual(env.win.localStorage.getItem('viv.theme'), 'light');
  assert.strictEqual(env.attrs['data-theme'], 'light');
  env.win._toggleTheme();
  assert.strictEqual(env.win.localStorage.getItem('viv.theme'), 'dark');
  env.win._setTheme('system');
  assert.strictEqual(env.attrs['data-theme-pref'], 'system');
  assert.strictEqual(env.win.vivTheme.getPreference(), 'system');
}

// ---- themed images follow the resolved theme ---------------------------------
{
  const img = {
    _a: { 'data-light-src': 'l.png', 'data-dark-src': 'd.png', src: 'l.png' },
    getAttribute(k) { return this._a[k] || null; },
    setAttribute(k, v) { this._a[k] = v; },
  };
  const { api } = load({ images: [img] });
  api.setPreference('dark');
  assert.strictEqual(img._a.src, 'd.png');
  api.setPreference('light');
  assert.strictEqual(img._a.src, 'l.png');
}

// ---- Plotly adapter: transparent backgrounds, token colors, flat relayout ----
{
  const { api, env } = load({ tokens: { '--chart-text': '#93a3b6', '--chart-grid': '#26324c', '--chart-axis': '#93a3b6' } });
  const layout = api.plotlyLayout();
  assert.strictEqual(layout.paper_bgcolor, 'transparent');
  assert.strictEqual(layout.plot_bgcolor, 'transparent');
  assert.strictEqual(layout.font.color, '#93a3b6');
  let call = null;
  env.win.Plotly = { relayout: (gd, upd) => { call = { gd, upd }; } };
  const gd = { layout: { xaxis: {}, yaxis: {}, xaxis2: {}, title: { text: 'keep' } } };
  assert.strictEqual(api.applyToPlotly(gd), true);
  assert.strictEqual(call.gd, gd);
  assert.strictEqual(call.upd['xaxis2.gridcolor'], '#26324c');
  assert.strictEqual(call.upd['yaxis.tickfont.color'], '#93a3b6');
  assert.strictEqual(call.upd['paper_bgcolor'], 'transparent');
  assert.ok(!('title' in call.upd), 'unrelated layout keys are not touched');
  assert.strictEqual(api.applyToPlotly(null), false);
  delete env.win.Plotly;
  assert.strictEqual(api.applyToPlotly(gd), false, 'no Plotly -> no-op');
}

// ---- Plotly adapter without tokens.css: backgrounds only, Plotly keeps its colours ----
{
  const { api, env } = load({ tokens: {} });
  const layout = api.plotlyLayout();
  assert.deepStrictEqual(Object.keys(layout).sort(), ['paper_bgcolor', 'plot_bgcolor']);
  let call = null;
  env.win.Plotly = { relayout: (gd, upd) => { call = upd; } };
  assert.strictEqual(api.applyToPlotly({ layout: { xaxis: {} } }), true);
  assert.deepStrictEqual(Object.keys(call).sort(), ['paper_bgcolor', 'plot_bgcolor']);
}

// ---- the pre-paint boot agrees with the runtime on every input --------------
{
  const html = fs.readFileSync(BOOT_HTML, 'utf8');
  const m = /<script>([\s\S]*?)<\/script>/.exec(html);
  assert.ok(m, 'boot partial contains an inline script');
  assert.ok(/<meta name="color-scheme" content="light dark">/.test(html), 'color-scheme meta');
  const bootCode = m[1];
  assert.ok(Buffer.byteLength(html, 'utf8') < 1024, 'boot partial stays small');
  const defaultMatch = /var K='viv\.theme',D='(system|light|dark)'/.exec(bootCode);
  assert.ok(defaultMatch, 'boot declares its default');
  const { api: runtime } = load();
  assert.strictEqual(defaultMatch[1], runtime.DEFAULT_PREFERENCE, 'boot D === theme.js DEFAULT_PREFERENCE');

  const storedCases = [null, 'system', 'light', 'dark', 'blue', ''];
  const cookieCases = ['', 'viv_theme=dark', 'a=1; viv_theme=system', 'viv_theme=bogus', 'xviv_theme=dark'];
  for (const stored of storedCases) {
    for (const cookie of cookieCases) {
      for (const sysDark of [false, true]) {
        for (const throwing of [false, true]) {
          const attrs = {};
          const docEl = { setAttribute: (k, v) => { attrs[k] = v; }, style: {} };
          const store = makeStore(stored === null ? {} : { 'viv.theme': stored }, { throws: throwing });
          const sandbox = {
            document: { documentElement: docEl, cookie: cookie },
            localStorage: store,
            window: { matchMedia: () => ({ matches: sysDark }) },
          };
          vm.runInNewContext(bootCode, sandbox);

          const { api, env } = load({ storage: makeStore(stored === null ? {} : { 'viv.theme': stored }, { throws: throwing }),
                                      cookie: cookie, systemDark: sysDark });
          const label = JSON.stringify({ stored, cookie, sysDark, throwing });
          assert.strictEqual(attrs['data-theme'], env.attrs['data-theme'], 'resolved ' + label);
          assert.strictEqual(attrs['data-theme-pref'], env.attrs['data-theme-pref'], 'preference ' + label);
          assert.strictEqual(docEl.style.colorScheme, attrs['data-theme'], 'colorScheme ' + label);
          assert.strictEqual(attrs['data-theme-pref'], api.getPreference(), 'getPreference ' + label);
        }
      }
    }
  }
}

console.log('test_theme.js: all assertions passed');
