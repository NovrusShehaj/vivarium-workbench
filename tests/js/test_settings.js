// tests/js/test_settings.js — run with: node tests/js/test_settings.js
//
// Pure helpers of static/settings.js (theme labels, menu key navigation, the
// optional shortcut's guards) and the extension section registry. Real DOM
// behaviour (menu focus, radio group) is covered by the Playwright E2E suite.
const assert = require('assert');

// ---- minimal DOM for registerSection -----------------------------------------
function el(tag) {
  return {
    tagName: tag.toUpperCase(), children: [], attrs: {}, id: '', className: '', textContent: '',
    parentNode: null,
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c, ref) {
      c.parentNode = this;
      const i = ref ? this.children.indexOf(ref) : -1;
      if (i === -1) this.children.push(c); else this.children.splice(i, 0, c);
      return c;
    },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
}
const host = el('div');
host.id = 'viv-settings-extensions';
global.window = {
  document: {
    readyState: 'complete',
    createElement: el,
    getElementById: (id) => (id === 'viv-settings-extensions' ? host : null),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
  },
  addEventListener() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
};

const settings = require('../../vivarium_workbench/static/settings.js');

// ---- describe -----------------------------------------------------------------
assert.strictEqual(settings.describe('system', 'dark'), 'System (currently Dark)');
assert.strictEqual(settings.describe('system', 'light'), 'System (currently Light)');
assert.strictEqual(settings.describe('dark', 'dark'), 'Dark');
assert.strictEqual(settings.describe('light', 'dark'), 'Light');
assert.strictEqual(settings.describe('bogus', 'dark'), 'Light');

// ---- menu navigation wraps; Home/End jump; other keys are not navigation -----
assert.strictEqual(settings.nextIndex('ArrowDown', 0, 4), 1);
assert.strictEqual(settings.nextIndex('ArrowDown', 3, 4), 0, 'wraps forward');
assert.strictEqual(settings.nextIndex('ArrowUp', 0, 4), 3, 'wraps backward');
assert.strictEqual(settings.nextIndex('ArrowDown', -1, 4), 0);
assert.strictEqual(settings.nextIndex('ArrowUp', -1, 4), 3);
assert.strictEqual(settings.nextIndex('Home', 2, 4), 0);
assert.strictEqual(settings.nextIndex('End', 0, 4), 3);
assert.strictEqual(settings.nextIndex('Enter', 0, 4), -1);
assert.strictEqual(settings.nextIndex('ArrowDown', 0, 0), -1);

// ---- the shortcut never fires while typing ----------------------------------
assert.strictEqual(settings.isTypingTarget({ tagName: 'TEXTAREA' }), true);
assert.strictEqual(settings.isTypingTarget({ tagName: 'input', type: 'text' }), true);
assert.strictEqual(settings.isTypingTarget({ tagName: 'INPUT', type: 'search' }), true);
assert.strictEqual(settings.isTypingTarget({ tagName: 'INPUT' }), true, 'default input type is text');
assert.strictEqual(settings.isTypingTarget({ tagName: 'INPUT', type: 'checkbox' }), false);
assert.strictEqual(settings.isTypingTarget({ tagName: 'SELECT' }), true);
assert.strictEqual(settings.isTypingTarget({ tagName: 'DIV', isContentEditable: true }), true);
assert.strictEqual(settings.isTypingTarget({ tagName: 'BUTTON' }), false);
assert.strictEqual(settings.isTypingTarget(null), false);

assert.strictEqual(settings.isShortcut({ altKey: true, shiftKey: true, code: 'KeyT' }), true);
assert.strictEqual(settings.isShortcut({ altKey: true, shiftKey: true, ctrlKey: true, code: 'KeyT' }), false);
assert.strictEqual(settings.isShortcut({ altKey: true, shiftKey: false, code: 'KeyT' }), false);
assert.strictEqual(settings.isShortcut({ altKey: true, shiftKey: true, metaKey: true, code: 'KeyT' }), false);
assert.strictEqual(settings.isShortcut({ altKey: true, shiftKey: true, code: 'KeyY' }), false);

// ---- extension sections -------------------------------------------------------
assert.throws(() => settings.registerSection({ id: 'Bad Id', title: 'x', mount() {} }), /invalid id/);
assert.throws(() => settings.registerSection({ id: 'ok', title: 'x' }), /mount/);

const mounted = [];
settings.registerSection({ id: 'zeta', title: 'Zeta', order: 200, mount: (b) => mounted.push(['zeta', b.className]) });
settings.registerSection({ id: 'alpha', title: 'Alpha <b>', order: 10, mount: (b) => mounted.push(['alpha', b.className]) });
settings.registerSection({ id: 'broken', title: 'Broken', order: 300, mount: () => { throw new Error('x'); } });
// duplicate registration is ignored
settings.registerSection({ id: 'alpha', title: 'Again', mount: () => mounted.push(['dup']) });

assert.deepStrictEqual(mounted, [['zeta', 'viv-settings-section-body'], ['alpha', 'viv-settings-section-body']]);
assert.deepStrictEqual(host.children.map((c) => c.id), ['settings-alpha', 'settings-zeta', 'settings-broken'],
  'sections are ordered by `order`');
const alpha = host.children[0];
assert.strictEqual(alpha.children[0].textContent, 'Alpha <b>', 'titles are text, never HTML');
assert.strictEqual(alpha.getAttribute('aria-labelledby'), 'settings-alpha-title');
const broken = host.children[2];
assert.strictEqual(broken.children[1].textContent, 'This settings section failed to load.');

console.log('test_settings.js: all assertions passed');
