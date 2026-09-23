// tests/js/test_dialog.js — static/dialog.js: modal dialog semantics.
// Run with: node tests/js/test_dialog.js
'use strict';
const assert = require('assert');

// A purpose-built fake DOM: only what dialog.js touches.
function makeDom() {
  const listeners = {};
  const doc = {
    activeElement: null,
    addEventListener(type, fn, capture) { (listeners[type] = listeners[type] || []).push({ fn, capture }); },
    contains(n) { while (n) { if (n === doc.body) return true; n = n.parentNode; } return false; },
  };
  function el(tag, opts) {
    opts = opts || {};
    const n = {
      tagName: tag.toUpperCase(), attrs: {}, children: [], parentNode: null, style: {},
      id: opts.id || '', hidden: false, classList: { contains: (c) => (opts.cls || []).includes(c) },
      focusable: !!opts.focusable, disabled: !!opts.disabled, ownerDocument: doc,
      get offsetParent() { let p = this; while (p) { if (p.style.display === 'none') return null; p = p.parentNode; } return doc.body; },
      getClientRects() { return this.offsetParent ? [1] : []; },
      setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); },
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      hasAttribute(k) { return k in this.attrs; },
      appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
      get firstElementChild() { return this.children[0] || null; },
      contains(x) { while (x) { if (x === this) return true; x = x.parentNode; } return false; },
      focus() { doc.activeElement = this; },
      all() { return this.children.reduce((a, c) => a.concat([c], c.all()), []); },
      querySelector(sel) {
        if (sel === '.modal-box') return this.all().find((c) => c.classList.contains('modal-box')) || null;
        if (sel === 'h1, h2, h3, h4') return this.all().find((c) => /^H[1-4]$/.test(c.tagName)) || null;
        throw new Error('unsupported selector ' + sel);
      },
      querySelectorAll(sel) {
        if (sel === '.modal-close') return this.all().filter((c) => c.classList.contains('modal-close'));
        return this.all().filter((c) => c.focusable && !c.disabled);   // the FOCUSABLE list
      },
    };
    return n;
  }
  doc.body = el('body');
  const press = (key, shiftKey) => {
    const e = { key, shiftKey: !!shiftKey, defaultPrevented: false, stopped: false,
      preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
    (listeners.keydown || []).forEach((l) => l.fn(e));
    return e;
  };
  return { doc, el, press, listeners };
}

const dom = makeDom();
global.window = { document: dom.doc };
global.document = dom.doc;
const dialog = require('../../vivarium_workbench/static/dialog.js');
assert.strictEqual(window.vivDialog, dialog, 'exposed as window.vivDialog');
assert.ok(dom.listeners.keydown.some((l) => l.capture === true), 'Escape/Tab handled in the capture phase');

// page: an opener button + a modal overlay (hidden)
const opener = dom.doc.body.appendChild(dom.el('button', { focusable: true }));
const overlay = dom.doc.body.appendChild(dom.el('div', { id: 'modal-x', cls: ['modal-overlay'] }));
overlay.style.display = 'none';
const box = overlay.appendChild(dom.el('div', { cls: ['modal-box'] }));
const close = box.appendChild(dom.el('button', { cls: ['modal-close'], focusable: true }));
const title = box.appendChild(dom.el('h3'));
const input = box.appendChild(dom.el('input', { focusable: true }));
const disabled = box.appendChild(dom.el('button', { focusable: true, disabled: true }));
const submit = box.appendChild(dom.el('button', { focusable: true }));
void disabled;

opener.focus();
dialog.open(overlay);
assert.strictEqual(overlay.style.display, 'flex', 'shown');
assert.strictEqual(box.getAttribute('role'), 'dialog');
assert.strictEqual(box.getAttribute('aria-modal'), 'true');
assert.strictEqual(title.id, 'modal-x-title', 'heading gets an id');
assert.strictEqual(box.getAttribute('aria-labelledby'), 'modal-x-title', 'named by its heading');
assert.strictEqual(close.getAttribute('aria-label'), 'Close', 'icon-only close button gets a name');
assert.strictEqual(dom.doc.activeElement, input, 'focus moves to the first field (not the × button)');
assert.ok(dialog.isOpen(overlay));

// Tab wraps from the last focusable to the first; Shift+Tab from the first to the last
submit.focus();
let e = dom.press('Tab');
assert.ok(e.defaultPrevented && dom.doc.activeElement === close, 'Tab on the last item wraps to the first');
e = dom.press('Tab', true);
assert.ok(e.defaultPrevented && dom.doc.activeElement === submit, 'Shift+Tab on the first item wraps to the last');
input.focus();
e = dom.press('Tab');
assert.ok(!e.defaultPrevented, 'Tab between inner items is left to the browser');
opener.focus();   // focus escaped somehow
e = dom.press('Tab');
assert.ok(e.defaultPrevented && dom.doc.activeElement === close, 'focus outside the dialog is pulled back in');

// Escape closes the topmost dialog and restores focus to the opener
e = dom.press('Escape');
assert.ok(e.defaultPrevented && e.stopped, 'Escape is consumed');
assert.strictEqual(overlay.style.display, 'none', 'hidden');
assert.ok(!dialog.isOpen(overlay));
assert.strictEqual(dom.doc.activeElement, opener, 'focus restored to the opener');

// Keys do nothing once no dialog is open
e = dom.press('Escape');
assert.ok(!e.defaultPrevented, 'no dialog -> Escape untouched');

// Stacked dialogs: Escape closes only the top one, onClose runs, focus returns step by step
const overlay2 = dom.doc.body.appendChild(dom.el('div', { id: 'modal-y' }));
overlay2.style.display = 'none';
const card2 = overlay2.appendChild(dom.el('div'));
const ok2 = card2.appendChild(dom.el('button', { focusable: true }));
let closed = 0;
opener.focus();
dialog.open(overlay);
dialog.open(overlay2, { label: 'Sign in', onClose: () => { closed++; } });
assert.strictEqual(card2.getAttribute('role'), 'dialog', 'first element child is the box when there is no .modal-box');
assert.strictEqual(card2.getAttribute('aria-label'), 'Sign in');
assert.strictEqual(dom.doc.activeElement, ok2);
dom.press('Escape');
assert.strictEqual(closed, 1, 'onClose cleanup ran');
assert.strictEqual(overlay2.style.display, 'none');
assert.strictEqual(overlay.style.display, 'flex', 'the lower dialog stays open');
assert.strictEqual(dom.doc.activeElement, input, 'focus back in the lower dialog');
dialog.close(overlay);
assert.strictEqual(dom.doc.activeElement, opener);

// An overlay hidden behind the helper's back is dropped from the stack
dialog.open(overlay);
overlay.style.display = 'none';
e = dom.press('Escape');
assert.ok(!e.defaultPrevented, 'stale entries are ignored');

console.log('test_dialog.js: all assertions passed');
