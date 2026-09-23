// tests/js/test_sidepanel.js — run with: node tests/js/test_sidepanel.js
//
// Width math and keyboard resizing of static/sidepanel.js (the core right-side
// panel host). Pointer dragging, focus restore and the overlay are covered by
// the Playwright E2E suite.
const assert = require('assert');

global.window = {};   // no document: the module exports helpers without wiring the DOM
const sp = require('../../vivarium_workbench/static/sidepanel.js');

assert.strictEqual(sp.MIN_WIDTH, 320);
assert.strictEqual(sp.MAX_WIDTH, 720);

// max = min(50vw, 720), never below the minimum
assert.strictEqual(sp.maxWidth(2000), 720);
assert.strictEqual(sp.maxWidth(1200), 600);
assert.strictEqual(sp.maxWidth(500), 320, 'narrow viewport keeps the minimum');
assert.strictEqual(sp.maxWidth(undefined), 320);

// clamp
assert.strictEqual(sp.clampWidth(100, 2000), 320);
assert.strictEqual(sp.clampWidth(5000, 2000), 720);
assert.strictEqual(sp.clampWidth(450.6, 2000), 451);
assert.strictEqual(sp.clampWidth('abc', 2000), sp.DEFAULT_WIDTH, 'garbage falls back to the default');
assert.strictEqual(sp.clampWidth(-5, 2000), sp.DEFAULT_WIDTH);
assert.strictEqual(sp.clampWidth(700, 1200), 600, 'clamped to 50vw');

// keyboard: the handle is on the LEFT edge, so ArrowLeft widens
assert.strictEqual(sp.keyWidth('ArrowLeft', 400, 2000), 416);
assert.strictEqual(sp.keyWidth('ArrowRight', 400, 2000), 384);
assert.strictEqual(sp.keyWidth('ArrowRight', 320, 2000), 320, 'cannot shrink below min');
assert.strictEqual(sp.keyWidth('ArrowLeft', 720, 2000), 720, 'cannot grow above max');
assert.strictEqual(sp.keyWidth('Home', 500, 2000), 320);
assert.strictEqual(sp.keyWidth('End', 500, 2000), 720);
assert.strictEqual(sp.keyWidth('Enter', 500, 2000), null);

// API surface without a DOM is inert, not throwing
assert.strictEqual(sp.isOpen(), false);
assert.doesNotThrow(() => { sp.open('x'); sp.close(); sp.toggle(); });
const off = sp.onChange(() => {});
assert.strictEqual(typeof off, 'function');
off();

console.log('test_sidepanel.js: all assertions passed');
