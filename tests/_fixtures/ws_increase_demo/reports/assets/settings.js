// settings.js — the core Settings page (#settings) and the rail theme menu.
//
// Core and AI-agnostic. Owns:
//   * the Appearance section: a native radio group (System / Light / Dark)
//     bound to window.vivTheme, with a live "(currently Dark)" note, plus the
//     optional light/dark keyboard shortcut (off by default);
//   * the rail-footer theme menu button (WAI-ARIA menu button pattern:
//     menuitemradio x3 + a Settings link; arrows/Home/End move, Enter/Space
//     select, Escape closes and returns focus);
//   * window.vivSettings.registerSection(), the mount point extensions use to
//     add their own sections (e.g. the assistant's providers).
//
// Nothing here is persisted server-side: the theme preference lives in the
// browser (localStorage + host cookie, see theme.js) and the shortcut toggle
// in localStorage.
//
// Loaded as a classic script at the end of <body>; also require()-able under
// Node for tests/js/test_settings.js (pure helpers only).
(function (root) {
  'use strict';

  var SHORTCUT_KEY = 'viv.theme.shortcut';
  var LABELS = { system: 'System', light: 'Light', dark: 'Dark' };

  var doc = root.document;
  var sections = [];          // { id, title, order, mount, onShow, el }
  var mountedDom = false;

  // ── Pure helpers (unit-tested) ─────────────────────────────────────────

  // Human description of the current theme state for labels and notes.
  function describe(preference, resolved) {
    var p = LABELS[preference] ? preference : 'light';
    var r = resolved === 'dark' ? 'dark' : 'light';
    if (p === 'system') return 'System (currently ' + LABELS[r] + ')';
    return LABELS[p];
  }

  // Index of the next menu item for a navigation key, wrapping; -1 if the key
  // is not a navigation key.
  function nextIndex(key, current, count) {
    if (count <= 0) return -1;
    switch (key) {
      case 'ArrowDown': return current < 0 ? 0 : (current + 1) % count;
      case 'ArrowUp': return current < 0 ? count - 1 : (current - 1 + count) % count;
      case 'Home': return 0;
      case 'End': return count - 1;
      default: return -1;
    }
  }

  // Whether a keydown target is somewhere the user is typing (the optional
  // shortcut must never fire there).
  function isTypingTarget(el) {
    if (!el || !el.tagName) return false;
    var tag = String(el.tagName).toUpperCase();
    if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (tag === 'INPUT') {
      var t = String(el.type || 'text').toLowerCase();
      return ['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file', 'image'].indexOf(t) === -1;
    }
    return !!el.isContentEditable;
  }

  // Alt+Shift+T (layout-independent: event.code), no Ctrl/Meta.
  function isShortcut(e) {
    return !!(e && e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyT');
  }

  // ── Storage ─────────────────────────────────────────────────────────────

  function shortcutEnabled() {
    try { return root.localStorage && root.localStorage.getItem(SHORTCUT_KEY) === '1'; } catch (e) { return false; }
  }

  function setShortcutEnabled(on) {
    try {
      if (!root.localStorage) return;
      if (on) root.localStorage.setItem(SHORTCUT_KEY, '1');
      else root.localStorage.removeItem(SHORTCUT_KEY);
    } catch (e) { /* private mode */ }
  }

  // ── Theme state ─────────────────────────────────────────────────────────

  function theme() { return root.vivTheme || null; }

  function state() {
    var t = theme();
    if (!t) return { preference: 'light', resolved: 'light', systemResolved: 'light' };
    var sysDark = typeof t.systemPrefersDark === 'function' ? t.systemPrefersDark() : false;
    return {
      preference: t.getPreference(),
      resolved: t.getResolved(),
      systemResolved: t.resolve('system', sysDark),
    };
  }

  // ── Appearance section ──────────────────────────────────────────────────

  function syncAppearance() {
    if (!doc) return;
    var s = state();
    var radios = doc.querySelectorAll('input[name="viv-theme-pref"]');
    for (var i = 0; i < radios.length; i++) radios[i].checked = radios[i].value === s.preference;
    var note = doc.querySelector('[data-theme-system-note]');
    if (note) note.textContent = '(currently ' + LABELS[s.systemResolved] + ')';
    var cb = doc.getElementById('viv-theme-shortcut-cb');
    if (cb) cb.checked = shortcutEnabled();
  }

  function wireAppearance() {
    var radios = doc.querySelectorAll('input[name="viv-theme-pref"]');
    for (var i = 0; i < radios.length; i++) {
      radios[i].addEventListener('change', function (e) {
        var t = theme();
        if (t && e.target.checked) t.setPreference(e.target.value);
      });
    }
    var cb = doc.getElementById('viv-theme-shortcut-cb');
    if (cb) cb.addEventListener('change', function () { setShortcutEnabled(cb.checked); });
  }

  // ── Rail theme menu ─────────────────────────────────────────────────────

  var menuBtn = null;
  var menuList = null;

  function menuItems() {
    return menuList ? Array.prototype.slice.call(menuList.querySelectorAll('[role="menuitemradio"], [role="menuitem"]')) : [];
  }

  function syncMenu() {
    if (!menuBtn || !menuList) return;
    var s = state();
    var radios = menuList.querySelectorAll('[role="menuitemradio"]');
    for (var i = 0; i < radios.length; i++) {
      radios[i].setAttribute('aria-checked', radios[i].getAttribute('data-theme-choice') === s.preference ? 'true' : 'false');
    }
    var icons = menuBtn.querySelectorAll('[data-theme-icon]');
    for (var j = 0; j < icons.length; j++) {
      icons[j].hidden = icons[j].getAttribute('data-theme-icon') !== s.preference;
    }
    var label = 'Theme: ' + describe(s.preference, s.resolved);
    menuBtn.setAttribute('aria-label', label);
    menuBtn.setAttribute('title', label);
  }

  function isMenuOpen() { return !!(menuList && !menuList.hidden); }

  function positionMenu() {
    if (!menuBtn || !menuList || typeof menuBtn.getBoundingClientRect !== 'function') return;
    var r = menuBtn.getBoundingClientRect();
    menuList.style.left = Math.max(8, Math.round(r.left)) + 'px';
    menuList.style.bottom = Math.max(8, Math.round((root.innerHeight || 0) - r.top + 6)) + 'px';
  }

  function focusItem(idx) {
    var items = menuItems();
    if (idx < 0 || idx >= items.length) return;
    items[idx].focus();
  }

  function openMenu(focusLast) {
    if (!menuList) return;
    syncMenu();
    menuList.hidden = false;
    menuBtn.setAttribute('aria-expanded', 'true');
    positionMenu();
    var items = menuItems();
    var checked = -1;
    for (var i = 0; i < items.length; i++) {
      if (items[i].getAttribute('aria-checked') === 'true') { checked = i; break; }
    }
    focusItem(focusLast ? items.length - 1 : (checked >= 0 ? checked : 0));
  }

  function closeMenu(restoreFocus) {
    if (!menuList || menuList.hidden) return;
    menuList.hidden = true;
    menuBtn.setAttribute('aria-expanded', 'false');
    if (restoreFocus) menuBtn.focus();
  }

  function activate(item) {
    if (!item) return;
    var choice = item.getAttribute('data-theme-choice');
    if (choice) {
      var t = theme();
      if (t) t.setPreference(choice);
      closeMenu(true);
      return;
    }
    if (item.hasAttribute('data-theme-settings-link')) {
      closeMenu(false);
      openSettings();
    }
  }

  function wireMenu() {
    menuBtn = doc.getElementById('viv-theme-menu-btn');
    menuList = doc.getElementById('viv-theme-menu-list');
    if (!menuBtn || !menuList) return;
    syncMenu();

    menuBtn.addEventListener('click', function () {
      if (isMenuOpen()) closeMenu(true); else openMenu(false);
    });
    menuBtn.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault(); openMenu(false);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault(); openMenu(true);
      }
    });
    menuList.addEventListener('keydown', function (e) {
      var items = menuItems();
      var cur = items.indexOf(doc.activeElement);
      var n = nextIndex(e.key, cur, items.length);
      if (n >= 0) { e.preventDefault(); focusItem(n); return; }
      if (e.key === 'Escape') { e.preventDefault(); closeMenu(true); return; }
      if (e.key === 'Tab') { closeMenu(false); return; }
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault(); activate(items[cur]);
      }
    });
    menuList.addEventListener('click', function (e) {
      var item = e.target && e.target.closest ? e.target.closest('[role="menuitemradio"], [role="menuitem"]') : null;
      if (!item) return;
      e.preventDefault();
      activate(item);
    });
    doc.addEventListener('mousedown', function (e) {
      if (!isMenuOpen()) return;
      var inside = (menuList.contains && menuList.contains(e.target)) || (menuBtn.contains && menuBtn.contains(e.target));
      if (!inside) closeMenu(false);
    });
    root.addEventListener('resize', function () { if (isMenuOpen()) positionMenu(); });
  }

  // ── Keyboard shortcut (opt-in) ──────────────────────────────────────────

  function onKeydown(e) {
    if (!isShortcut(e) || !shortcutEnabled() || isTypingTarget(e.target)) return;
    var t = theme();
    if (!t) return;
    e.preventDefault();
    t.setPreference(t.getResolved() === 'dark' ? 'light' : 'dark');
  }

  // ── Extension sections ──────────────────────────────────────────────────

  function mountSection(sec) {
    if (sec.el || !doc) return;
    var host = doc.getElementById('viv-settings-extensions');
    if (!host) return;
    var el = doc.createElement('section');
    el.className = 'panel viv-settings-section';
    el.id = 'settings-' + sec.id;
    var h = doc.createElement('h2');
    h.id = 'settings-' + sec.id + '-title';
    h.textContent = sec.title;
    el.setAttribute('aria-labelledby', h.id);
    el.appendChild(h);
    var body = doc.createElement('div');
    body.className = 'viv-settings-section-body';
    el.appendChild(body);
    // Keep sections ordered by `order`, then registration order.
    var after = null;
    for (var i = 0; i < sections.length; i++) {
      var other = sections[i];
      if (other !== sec && other.el && other.order > sec.order) { after = other.el; break; }
    }
    host.insertBefore(el, after);
    sec.el = el;
    try { sec.mount(body); } catch (err) {
      body.textContent = 'This settings section failed to load.';
      if (root.console) root.console.error('[settings] section ' + sec.id + ' failed:', err);
    }
  }

  // def: { id, title, mount(bodyEl), onShow?(bodyEl), order? }
  function registerSection(def) {
    if (!def || typeof def.id !== 'string' || !/^[a-z][a-z0-9-]{0,40}$/.test(def.id)) {
      throw new Error('vivSettings.registerSection: invalid id');
    }
    if (typeof def.mount !== 'function') throw new Error('vivSettings.registerSection: mount() required');
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].id === def.id) return sections[i].el;
    }
    var sec = {
      id: def.id,
      title: String(def.title || def.id),
      order: typeof def.order === 'number' ? def.order : 100,
      mount: def.mount,
      onShow: typeof def.onShow === 'function' ? def.onShow : null,
      el: null,
    };
    sections.push(sec);
    sections.sort(function (a, b) { return a.order - b.order; });
    if (mountedDom) mountSection(sec);
    return sec.el;
  }

  function openSettings(sectionId) {
    if (root.location) root.location.hash = '#settings';
    if (typeof root._switchPage === 'function') root._switchPage('settings');
    if (sectionId && doc) {
      var el = doc.getElementById('settings-' + sectionId);
      if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'start' });
    }
  }

  // Called by walkthrough.js _switchPage('settings').
  function render() {
    syncAppearance();
    for (var i = 0; i < sections.length; i++) {
      var s = sections[i];
      if (s.el && s.onShow) {
        try { s.onShow(s.el.querySelector('.viv-settings-section-body')); } catch (e) { /* keep page usable */ }
      }
    }
  }

  function init() {
    if (!doc || mountedDom) return;
    mountedDom = true;
    wireAppearance();
    wireMenu();
    syncAppearance();
    for (var i = 0; i < sections.length; i++) mountSection(sections[i]);
    var t = theme();
    if (t) t.subscribe(function () { syncAppearance(); syncMenu(); });
    try {
      var mql = root.matchMedia ? root.matchMedia('(prefers-color-scheme: dark)') : null;
      var onOs = function () { syncAppearance(); syncMenu(); };
      if (mql && typeof mql.addEventListener === 'function') mql.addEventListener('change', onOs);
      else if (mql && typeof mql.addListener === 'function') mql.addListener(onOs);
    } catch (e) { /* no matchMedia */ }
    doc.addEventListener('keydown', onKeydown);
  }

  var api = {
    registerSection: registerSection,
    render: render,
    open: openSettings,
    // exposed for tests
    describe: describe,
    nextIndex: nextIndex,
    isTypingTarget: isTypingTarget,
    isShortcut: isShortcut,
  };

  root.vivSettings = api;

  if (doc && typeof doc.addEventListener === 'function') {
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
    else init();
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
