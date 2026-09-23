// static/dialog.js — modal dialog semantics shared by every workbench modal.
//
//   vivDialog.open(overlayEl[, { label, initialFocus }])
//   vivDialog.close(overlayEl)
//   vivDialog.isOpen(overlayEl)
//
// `overlayEl` is the full-screen backdrop (`.modal-overlay`, the GitHub
// device-flow backdrop, …). Its dialog box (`.modal-box`, or the first
// element child) gets role="dialog", aria-modal="true" and an accessible
// name from its first heading. Opening remembers what had focus and moves
// focus into the dialog; Tab and Shift+Tab stay inside it; Escape closes the
// topmost dialog; closing restores focus to where it was. Callers keep
// deciding HOW an overlay is shown: open/close only toggle `display`.
(function (root) {
  'use strict';

  var FOCUSABLE = [
    'a[href]', 'area[href]', 'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])', 'select:not([disabled])',
    'textarea:not([disabled])', 'iframe', '[tabindex]:not([tabindex="-1"])',
    '[contenteditable="true"]',
  ].join(',');

  var stack = [];   // [{ el, opener, onClose }] — last is topmost

  function boxOf(el) {
    return (el && (el.querySelector('.modal-box') || el.firstElementChild)) || el;
  }

  function visible(node) {
    if (!node) return false;
    if (node.hidden) return false;
    // offsetParent is null for display:none subtrees (and position:fixed, so
    // fall back to getClientRects for those).
    return node.offsetParent !== null || (node.getClientRects && node.getClientRects().length > 0);
  }

  function focusables(el) {
    var box = boxOf(el);
    if (!box || !box.querySelectorAll) return [];
    return Array.prototype.filter.call(box.querySelectorAll(FOCUSABLE), visible);
  }

  function indexOf(el) {
    for (var i = stack.length - 1; i >= 0; i--) if (stack[i].el === el) return i;
    return -1;
  }

  function isOpen(el) { return indexOf(el) !== -1; }

  function label(box, el, opts) {
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    if (opts && opts.label) {
      box.setAttribute('aria-label', opts.label);
    } else if (!box.hasAttribute('aria-labelledby') && !box.hasAttribute('aria-label')) {
      var h = box.querySelector('h1, h2, h3, h4');
      if (h) {
        if (!h.id) h.id = (el.id || 'viv-dialog') + '-title';
        box.setAttribute('aria-labelledby', h.id);
      }
    }
    if (!box.hasAttribute('tabindex')) box.setAttribute('tabindex', '-1');
    // Icon-only close buttons ("×") need a name.
    var closers = box.querySelectorAll('.modal-close');
    for (var i = 0; i < closers.length; i++) {
      if (!closers[i].getAttribute('aria-label')) closers[i].setAttribute('aria-label', 'Close');
    }
  }

  function open(el, opts) {
    if (!el) return;
    opts = opts || {};
    var box = boxOf(el);
    label(box, el, opts);
    var doc = el.ownerDocument || root.document;
    var i = indexOf(el);
    var opener = i === -1 ? doc.activeElement : stack[i].opener;
    if (i !== -1) stack.splice(i, 1);
    stack.push({ el: el, opener: opener, onClose: opts.onClose || null });
    el.style.display = opts.display || 'flex';
    var target = opts.initialFocus || focusables(el).filter(function (n) {
      return !(n.classList && n.classList.contains('modal-close'));
    })[0] || box;
    try { target.focus(); } catch (e) { /* not focusable */ }
  }

  function close(el) {
    if (!el) return;
    el.style.display = 'none';
    var i = indexOf(el);
    if (i === -1) return;
    var entry = stack.splice(i, 1)[0];
    if (typeof entry.onClose === 'function') {
      try { entry.onClose(); } catch (e) { /* caller cleanup must not block focus restore */ }
    }
    var o = entry.opener;
    var doc = el.ownerDocument || root.document;
    if (o && typeof o.focus === 'function' && doc.contains(o)) {
      try { o.focus(); } catch (e) { /* gone */ }
    }
  }

  function top() {
    // Drop entries whose overlay was hidden or removed behind our back.
    while (stack.length) {
      var t = stack[stack.length - 1];
      var doc = t.el.ownerDocument || root.document;
      if (doc.contains(t.el) && t.el.style.display !== 'none') return t;
      stack.pop();
    }
    return null;
  }

  function onKeydown(e) {
    var t = top();
    if (!t) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      if (typeof e.stopImmediatePropagation === 'function') e.stopImmediatePropagation();
      close(t.el);
      return;
    }
    if (e.key !== 'Tab') return;
    var items = focusables(t.el);
    var doc = t.el.ownerDocument || root.document;
    var active = doc.activeElement;
    if (!items.length) { e.preventDefault(); return; }
    var first = items[0];
    var last = items[items.length - 1];
    var inside = t.el.contains(active);
    if (e.shiftKey && (active === first || !inside)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (active === last || !inside)) { e.preventDefault(); first.focus(); }
  }

  if (root.document && typeof root.document.addEventListener === 'function') {
    // Capture phase so the topmost dialog handles Escape before page-level
    // shortcuts (the side panel, menus) see it.
    root.document.addEventListener('keydown', onKeydown, true);
  }

  var api = { open: open, close: close, isOpen: isOpen, focusables: focusables,
              _onKeydown: onKeydown, _stack: stack, FOCUSABLE: FOCUSABLE };
  root.vivDialog = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
