// tests/js/test_assistant_markdown.js — run with: node tests/js/test_assistant_markdown.js
//
// The assistant's Markdown renderer treats model output as UNTRUSTED: raw
// HTML stays text, only http(s)/mailto/#route links become anchors, images
// are never loaded, and innerHTML is never used (the test DOM throws on it).
const assert = require('assert');
const { makeDocument, serialize } = require('./_mini_dom');

global.window = { document: makeDocument(), URL: URL, __BASE_PATH__: '' };
const MD = require('../../vivarium_workbench_assistant/static/assistant-markdown.js');
const H = require('../../vivarium_workbench_assistant/static/assistant-highlight.js');

function render(src) {
  const box = window.document.createElement('div');
  MD.render(src, box);
  return box;
}
function all(node, tag) { return node.querySelectorAll(tag); }

// ── XSS: raw HTML is text ──────────────────────────────────────────────────
{
  // [payload, text that must stay visible]
  const payloads = [
    ['<script>alert(1)</script>', '<script>alert(1)</script>'],
    ['<img src=x onerror=alert(1)>', 'onerror=alert(1)'],
    ['<iframe src="javascript:alert(1)"></iframe>', '<iframe'],
    ['<svg onload=alert(1)>', '<svg onload=alert(1)>'],
    ['<a href="javascript:alert(1)">x</a>', 'javascript:alert(1)'],
    ['```\n</code></pre><script>alert(1)</script>\n```', '</code></pre><script>alert(1)</script>'],
    ['<style>body{display:none}</style>', '<style>body{display:none}</style>'],
  ];
  payloads.forEach(([p, visible]) => {
    const box = render('before ' + p + ' after');
    for (const tag of ['script', 'img', 'iframe', 'svg', 'style']) {
      assert.strictEqual(all(box, tag).length, 0, tag + ' element created from: ' + p);
    }
    assert.ok(box.textContent.indexOf(visible) !== -1, 'payload kept as visible text: ' + p);
    all(box, 'a').forEach((a) => {
      assert.ok(!/^javascript:/i.test(a.getAttribute('href') || ''), 'javascript: href from ' + p);
    });
  });
}

// ── Links ────────────────────────────────────────────────────────────────
{
  const box = render('[docs](https://example.com/a?b=1) [bad](javascript:alert(1)) [data](data:text/html,<b>x</b>) ' +
                     '[route](#investigations) <https://auto.example> see https://bare.example/x. [mail](mailto:a@b.c)');
  const links = all(box, 'a');
  const hrefs = links.map((a) => a.getAttribute('href'));
  assert.deepStrictEqual(hrefs, ['https://example.com/a?b=1', '#investigations', 'https://auto.example/',
                                 'https://bare.example/x', 'mailto:a@b.c']);
  const ext = links[0];
  assert.strictEqual(ext.getAttribute('rel'), 'noopener noreferrer');
  assert.strictEqual(ext.getAttribute('target'), '_blank');
  assert.strictEqual(ext.getAttribute('title'), 'https://example.com/a?b=1', 'full URL on hover');
  assert.strictEqual(links[1].getAttribute('target'), null, 'in-app route stays in the page');
  assert.ok(box.textContent.indexOf('javascript:alert(1)') !== -1, 'unsafe link rendered as text');
  assert.ok(box.textContent.indexOf('data:text/html') !== -1);
}

// ── Images are never loaded ────────────────────────────────────────────────
{
  const box = render('![tracking pixel](https://evil.example/p.png?leak=secret)');
  assert.strictEqual(all(box, 'img').length, 0);
  const ph = all(box, 'span.asst-image-placeholder');
  assert.strictEqual(ph.length, 1);
  assert.ok(ph[0].textContent.indexOf('[image: tracking pixel]') === 0);
}

// ── Code blocks keep their text verbatim ────────────────────────────────────
{
  const box = render('```python\nprint("<b>x</b>")\n```');
  const code = all(box, 'code')[0];
  assert.strictEqual(code.textContent, 'print("<b>x</b>")');
  assert.strictEqual(code.getAttribute('data-asst-lang'), 'python');
  assert.strictEqual(all(box, 'button').length, 1, 'copy button');
  assert.strictEqual(all(box, 'b').length, 0);
}

// ── Structure: headings, lists, quotes, tables, emphasis ──────────────────────
{
  const box = render('# Title\n\nSome *em* and **strong** and ~~del~~ and `code`.\n\n' +
                     '- one\n- two\n  - nested\n\n1. first\n2. second\n\n> quoted\n\n' +
                     '| a | b |\n|---|--:|\n| 1 | 2 |\n\n---\n\nline one\nline two');
  assert.strictEqual(all(box, 'h3').length, 1, 'model h1 renders below the panel title');
  assert.strictEqual(all(box, 'em').length, 1);
  assert.strictEqual(all(box, 'strong').length, 1);
  assert.strictEqual(all(box, 'del').length, 1);
  assert.strictEqual(all(box, 'ul').length, 2, 'nested list');
  assert.strictEqual(all(box, 'ol').length, 1);
  assert.strictEqual(all(box, 'blockquote').length, 1);
  assert.strictEqual(all(box, 'table').length, 1);
  assert.strictEqual(all(box, 'th')[1].style.textAlign, 'right');
  assert.strictEqual(all(box, 'hr').length, 1);
  assert.strictEqual(all(box, 'br').length, 1, 'single newline is a line break');
}

{
  const box = render('*a **b** c*');
  const em = all(box, 'em');
  assert.strictEqual(em.length, 1);
  assert.strictEqual(em[0].textContent, 'a b c');
  assert.strictEqual(all(em[0], 'strong').length, 1);
  assert.strictEqual(render('snake_case_name stays').textContent, 'snake_case_name stays');
  assert.strictEqual(all(render('snake_case_name'), 'em').length, 0);
  assert.strictEqual(render('\\*not emphasis\\*').textContent, '*not emphasis*');
}

// ── Streaming renderer matches a full render, block by block ─────────────────
{
  const text = '# Plan\n\nFirst paragraph with **bold**.\n\n```yaml\na: 1\nb: 2\n```\n\n- x\n- y\n\nDone.';
  const streamed = window.document.createElement('div');
  const sr = MD.createStreamRenderer(streamed);
  for (let i = 1; i <= text.length; i += 3) sr.update(text.slice(0, i));
  sr.update(text);
  const full = render(text);
  // While streaming, only the trailing block sits in a .asst-md-tail wrapper;
  // unwrapped, the incrementally built DOM equals a full render.
  const unwrapped = streamed.childNodes
    .map((n) => (n.nodeType === 1 && n.className === 'asst-md-tail') ? n.childNodes.map(serialize).join('') : serialize(n))
    .join('');
  assert.strictEqual(unwrapped, full.childNodes.map(serialize).join(''));
  assert.ok(streamed.childNodes.length >= 4, 'completed blocks were frozen as they finished');
  sr.finish(text);
  assert.strictEqual(serialize(streamed), serialize(full));
}

// ── Highlighter: text nodes + token spans only ──────────────────────────────
{
  const toks = H.tokenize('def f(x):\n    return "hi" # note\n', 'python');
  const classes = toks.filter((t) => t[0]).map((t) => t[0] + ':' + t[1]);
  assert.ok(classes.indexOf('tok-keyword:def') !== -1);
  assert.ok(classes.indexOf('tok-keyword:return') !== -1);
  assert.ok(classes.indexOf('tok-string:"hi"') !== -1);
  assert.ok(classes.indexOf('tok-comment:# note') !== -1);
  assert.strictEqual(toks.map((t) => t[1]).join(''), 'def f(x):\n    return "hi" # note\n', 'lossless');
  const json = H.tokenize('{"k": true, "n": 1}', 'json').filter((t) => t[0]).map((t) => t[0]);
  assert.deepStrictEqual(json, ['tok-key', 'tok-boolean', 'tok-key', 'tok-number']);
  const diff = H.tokenize('--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n ctx\n', 'diff').map((t) => t[0]);
  assert.deepStrictEqual(diff, ['tok-meta', 'tok-meta', 'tok-meta', 'tok-del', 'tok-add', null]);
  assert.deepStrictEqual(H.tokenize('<b>x</b>', 'unknownlang'), [[null, '<b>x</b>']]);

  const code = window.document.createElement('code');
  code.textContent = 'x = "<script>"';
  H.highlight(code, 'python');
  assert.strictEqual(code.textContent, 'x = "<script>"', 'highlighting is lossless');
  assert.strictEqual(all(code, 'script').length, 0);
  assert.strictEqual(code.getAttribute('data-asst-highlighted'), '1');
}

console.log('test_assistant_markdown.js: all assertions passed');
