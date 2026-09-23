// assistant-markdown.js — a safe Markdown renderer for UNTRUSTED model output.
//
// Builds DOM nodes with createElement/textContent only. Model text is NEVER
// assigned to innerHTML/outerHTML/insertAdjacentHTML, so raw HTML in a reply
// renders as literal text.
//
// Supported subset: paragraphs (single newlines become line breaks), ATX
// headings, ordered/unordered (nested) lists, blockquotes, fenced code blocks
// (``` / ~~~ with a language tag), GFM tables, horizontal rules, emphasis,
// strong, strikethrough, inline code, links and autolinks.
//
// Links: only http(s), mailto and in-app "#route" links become anchors; they
// show the full URL on hover and external ones open with
// rel="noopener noreferrer". Anything else (javascript:, data:, relative
// paths…) stays text. Images are NEVER loaded — remote image URLs are an
// exfiltration channel for prompt injection — and render as a link
// placeholder instead.
//
// Streaming: createStreamRenderer() freezes completed blocks and re-renders
// only the trailing (still growing) block on each update.
//
// window.vivAssistantMarkdown = { render, parseBlocks, createStreamRenderer, safeHref }
(function (root) {
  'use strict';

  var FENCE_RE = /^ {0,3}(`{3,}|~{3,})\s*([\w+#.-]*)[^\n]*$/;
  var HEADING_RE = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  var HR_RE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
  var LIST_RE = /^( *)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  var QUOTE_RE = /^ {0,3}>\s?(.*)$/;
  var TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function doc() { return root.document; }

  function el(tag, cls) {
    var n = doc().createElement(tag);
    if (cls) n.className = cls;
    return n;
  }

  function text(t) { return doc().createTextNode(t); }

  // ── Links ────────────────────────────────────────────────────────────────
  function safeHref(url) {
    var u = String(url || '').trim();
    if (!u) return null;
    if (/^#[A-Za-z0-9_\-/=&?.:%]*$/.test(u)) return { href: u, inApp: true };
    var parsed;
    try { parsed = new root.URL(u); } catch (e) { return null; }
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:' || parsed.protocol === 'mailto:') {
      return { href: parsed.href, inApp: false };
    }
    return null;
  }

  function makeLink(label, url) {
    var safe = safeHref(url);
    if (!safe) return text(label === url ? label : label + ' (' + url + ')');
    var a = el('a', 'asst-link');
    a.setAttribute('href', safe.href);
    a.setAttribute('title', safe.href);
    if (!safe.inApp) {
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    }
    a.textContent = label;
    return a;
  }

  function imagePlaceholder(alt, url) {
    var span = el('span', 'asst-image-placeholder');
    span.appendChild(text('[image: ' + (alt || 'untitled') + '] '));
    var safe = safeHref(url);
    if (safe && !safe.inApp) span.appendChild(makeLink('open link', url));
    else span.appendChild(text('(' + url + ')'));
    return span;
  }

  // "[text](url "title")" starting at i (text[i] === '['); null if not a link.
  function parseLinkAt(s, i) {
    var depth = 0, j = i;
    for (; j < s.length; j++) {
      var ch = s.charAt(j);
      if (ch === '\\') { j++; continue; }
      if (ch === '[') depth++;
      else if (ch === ']') { depth--; if (depth === 0) break; }
    }
    if (j >= s.length || s.charAt(j + 1) !== '(') return null;
    var k = j + 2, parens = 1;
    for (; k < s.length; k++) {
      var c = s.charAt(k);
      if (c === '\\') { k++; continue; }
      if (c === '(') parens++;
      else if (c === ')') { parens--; if (parens === 0) break; }
      else if (c === '\n') return null;
    }
    if (k >= s.length) return null;
    var dest = s.slice(j + 2, k).trim();
    var m = /^<?([^\s>]*)>?(?:\s+["'(].*["')])?$/.exec(dest);
    return { label: s.slice(i + 1, j), url: m ? m[1] : dest, end: k + 1 };
  }

  // ── Inline ───────────────────────────────────────────────────────────────
  var ESCAPABLE = /[\\`*_{}[\]()#+\-.!|~<>]/;
  var BARE_URL_RE = /^(https?:\/\/[^\s<>"'`]+[^\s<>"'`.,;:!?)\]])/;

  function findClosing(s, from, delim) {
    var idx = from;
    var single = delim.length === 1;
    while (true) {
      idx = s.indexOf(delim, idx);
      if (idx === -1) return -1;
      var before = s.charAt(idx - 1);
      // A single delimiter never closes inside a doubled run ("**").
      var inRun = single && (s.charAt(idx + 1) === delim || before === delim);
      if (!inRun && before !== '\\' && !/\s/.test(before)) return idx;
      idx += inRun ? 2 : delim.length;
    }
  }

  function renderInline(s, parent) {
    var buf = '';
    function flush() { if (buf) { parent.appendChild(text(buf)); buf = ''; } }
    var i = 0;
    while (i < s.length) {
      var c = s.charAt(i);
      if (c === '\\' && i + 1 < s.length && ESCAPABLE.test(s.charAt(i + 1))) {
        buf += s.charAt(i + 1); i += 2; continue;
      }
      if (c === '\n') { flush(); parent.appendChild(el('br')); i++; continue; }
      if (c === '`') {
        var ticks = /^`+/.exec(s.slice(i))[0];
        var end = s.indexOf(ticks, i + ticks.length);
        if (end !== -1) {
          flush();
          var code = el('code', 'asst-inline-code');
          var inner = s.slice(i + ticks.length, end).replace(/\n/g, ' ');
          if (/^ .* $/.test(inner)) inner = inner.slice(1, -1);
          code.textContent = inner;
          parent.appendChild(code);
          i = end + ticks.length;
          continue;
        }
        buf += ticks; i += ticks.length; continue;
      }
      if (c === '!' && s.charAt(i + 1) === '[') {
        var img = parseLinkAt(s, i + 1);
        if (img) { flush(); parent.appendChild(imagePlaceholder(img.label, img.url)); i = img.end; continue; }
      }
      if (c === '[') {
        var link = parseLinkAt(s, i);
        if (link) {
          flush();
          var node = makeLink('', link.url);
          if (node.nodeType === 1) {           // an anchor: render its label inline
            renderInline(link.label, node);
            if (!node.textContent) node.textContent = link.url;
          } else {
            renderInline(link.label, parent);
            node = text(' (' + link.url + ')');
          }
          parent.appendChild(node);
          i = link.end;
          continue;
        }
      }
      if (c === '<') {
        var am = /^<((?:https?:\/\/|mailto:)[^\s<>]+)>/.exec(s.slice(i));
        if (am) { flush(); parent.appendChild(makeLink(am[1], am[1])); i += am[0].length; continue; }
      }
      if ((c === 'h') && (i === 0 || /[\s(]/.test(s.charAt(i - 1)))) {
        var bu = BARE_URL_RE.exec(s.slice(i));
        if (bu) { flush(); parent.appendChild(makeLink(bu[1], bu[1])); i += bu[1].length; continue; }
      }
      if (c === '~' && s.charAt(i + 1) === '~') {
        var dEnd = findClosing(s, i + 2, '~~');
        if (dEnd > i + 2) {
          flush();
          var del = el('del');
          renderInline(s.slice(i + 2, dEnd), del);
          parent.appendChild(del);
          i = dEnd + 2;
          continue;
        }
      }
      if (c === '*' || c === '_') {
        var intraword = c === '_' && i > 0 && /\w/.test(s.charAt(i - 1));
        if (!intraword && !/\s/.test(s.charAt(i + (s.charAt(i + 1) === c ? 2 : 1)) || '')) {
          var dbl = s.charAt(i + 1) === c;
          var delim = dbl ? c + c : c;
          var close = findClosing(s, i + delim.length + 1, delim);
          if (close !== -1 && (c !== '_' || !/\w/.test(s.charAt(close + delim.length) || ''))) {
            flush();
            var em = el(dbl ? 'strong' : 'em');
            renderInline(s.slice(i + delim.length, close), em);
            parent.appendChild(em);
            i = close + delim.length;
            continue;
          }
        }
      }
      buf += c;
      i++;
    }
    flush();
  }

  // ── Blocks ───────────────────────────────────────────────────────────────
  // Each block: { type, raw, ... } — raw is the source slice (for streaming).
  function parseBlocks(src) {
    var lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    var blocks = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (/^\s*$/.test(line)) { i++; continue; }
      var start = i;
      var fm = FENCE_RE.exec(line);
      if (fm) {
        var fence = fm[1];
        var body = [];
        i++;
        var closed = false;
        while (i < lines.length) {
          var close = new RegExp('^ {0,3}' + fence.charAt(0) + '{' + fence.length + ',}\\s*$');
          if (close.test(lines[i])) { closed = true; i++; break; }
          body.push(lines[i]);
          i++;
        }
        blocks.push({ type: 'code', lang: fm[2] || '', text: body.join('\n'), closed: closed,
                      raw: lines.slice(start, i).join('\n') });
        continue;
      }
      var hm = HEADING_RE.exec(line);
      if (hm) {
        blocks.push({ type: 'heading', level: hm[1].length, text: hm[2], raw: line });
        i++;
        continue;
      }
      if (HR_RE.test(line)) { blocks.push({ type: 'hr', raw: line }); i++; continue; }
      if (QUOTE_RE.test(line)) {
        var q = [];
        while (i < lines.length && QUOTE_RE.test(lines[i])) { q.push(QUOTE_RE.exec(lines[i])[1]); i++; }
        blocks.push({ type: 'quote', children: parseBlocks(q.join('\n')), raw: lines.slice(start, i).join('\n') });
        continue;
      }
      if (line.indexOf('|') !== -1 && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
        var header = splitRow(line);
        var aligns = splitRow(lines[i + 1]).map(function (c) {
          var l = c.charAt(0) === ':', r = c.charAt(c.length - 1) === ':';
          return l && r ? 'center' : r ? 'right' : l ? 'left' : '';
        });
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].indexOf('|') !== -1 && !/^\s*$/.test(lines[i])) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        blocks.push({ type: 'table', header: header, aligns: aligns, rows: rows,
                      raw: lines.slice(start, i).join('\n') });
        continue;
      }
      var lm = LIST_RE.exec(line);
      if (lm) {
        var baseIndent = lm[1].length;
        var ordered = /\d/.test(lm[2]);
        var items = [];
        var cur = null;
        while (i < lines.length) {
          var l = lines[i];
          var m = LIST_RE.exec(l);
          if (m && m[1].length === baseIndent && /\d/.test(m[2]) === ordered) {
            cur = { lines: [m[3]], startNum: ordered ? parseInt(m[2], 10) : null };
            items.push(cur);
            i++;
            continue;
          }
          if (/^\s*$/.test(l)) {
            // A blank line continues the list only if the next line is indented or another item.
            var nxt = lines[i + 1];
            if (nxt !== undefined && (/^\s{2,}\S/.test(nxt) || (LIST_RE.exec(nxt) &&
                LIST_RE.exec(nxt)[1].length === baseIndent))) {
              cur.lines.push('');
              i++;
              continue;
            }
            break;
          }
          var indent = /^( *)/.exec(l)[1].length;
          if (indent > baseIndent) {
            cur.lines.push(l.slice(Math.min(indent, baseIndent + 2 + (ordered ? 1 : 0))));
            i++;
            continue;
          }
          if (m) break;                     // a sibling list of another kind / indent
          // Lazy continuation of the item's paragraph.
          if (!FENCE_RE.test(l) && !HEADING_RE.test(l) && !QUOTE_RE.test(l) && !HR_RE.test(l)) {
            cur.lines.push(l);
            i++;
            continue;
          }
          break;
        }
        blocks.push({
          type: 'list', ordered: ordered, start: items.length ? items[0].startNum : null,
          items: items.map(function (it) { return parseBlocks(it.lines.join('\n')); }),
          raw: lines.slice(start, i).join('\n'),
        });
        continue;
      }
      // Paragraph: until a blank line or the start of another block.
      var para = [line];
      i++;
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !FENCE_RE.test(lines[i]) &&
             !HEADING_RE.test(lines[i]) && !QUOTE_RE.test(lines[i]) && !HR_RE.test(lines[i])) {
        if (/^\s{0,3}([-*+]|\d{1,9}[.)])\s/.test(lines[i])) break;     // a list starts
        para.push(lines[i]);
        i++;
      }
      blocks.push({ type: 'paragraph', text: para.join('\n'), raw: para.join('\n') });
    }
    return blocks;
  }

  function splitRow(line) {
    var t = line.trim();
    if (t.charAt(0) === '|') t = t.slice(1);
    if (t.charAt(t.length - 1) === '|' && t.charAt(t.length - 2) !== '\\') t = t.slice(0, -1);
    var cells = [], cur = '';
    for (var i = 0; i < t.length; i++) {
      var ch = t.charAt(i);
      if (ch === '\\' && t.charAt(i + 1) === '|') { cur += '|'; i++; continue; }
      if (ch === '|') { cells.push(cur.trim()); cur = ''; continue; }
      cur += ch;
    }
    cells.push(cur.trim());
    return cells;
  }

  var highlightRequested = false;

  function requestHighlight(codeEl, lang) {
    var H = root.vivAssistantHighlight;
    if (H && typeof H.highlight === 'function') {
      try { H.highlight(codeEl, lang); } catch (e) { /* plain text stays */ }
      return;
    }
    if (highlightRequested || !doc() || typeof doc().createElement !== 'function') return;
    highlightRequested = true;
    var s = doc().createElement('script');
    var base = (root.__BASE_PATH__ || '');
    s.src = base + '/ext/assistant/assets/assistant-highlight.js';
    s.async = true;
    s.onload = function () {
      var pending = doc().querySelectorAll('code[data-asst-lang]:not([data-asst-highlighted])');
      for (var i = 0; i < pending.length; i++) requestHighlight(pending[i], pending[i].getAttribute('data-asst-lang'));
    };
    (doc().head || doc().documentElement).appendChild(s);
  }

  function copyText(textValue, button) {
    function done(ok) {
      var prev = button.textContent;
      button.textContent = ok ? 'Copied' : 'Copy failed';
      setTimeout(function () { button.textContent = prev === 'Copied' || prev === 'Copy failed' ? 'Copy' : prev; }, 1500);
    }
    var nav = root.navigator;
    if (nav && nav.clipboard && typeof nav.clipboard.writeText === 'function' && root.isSecureContext !== false) {
      nav.clipboard.writeText(textValue).then(function () { done(true); }, function () { fallback(); });
    } else {
      fallback();
    }
    function fallback() {
      try {
        var ta = el('textarea', 'asst-copy-buffer');
        ta.value = textValue;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        doc().body.appendChild(ta);
        ta.select();
        var ok = doc().execCommand && doc().execCommand('copy');
        doc().body.removeChild(ta);
        done(!!ok);
      } catch (e) { done(false); }
    }
  }

  function renderCode(block) {
    var wrap = el('div', 'asst-code');
    var head = el('div', 'asst-code-head');
    var label = el('span', 'asst-code-lang');
    label.textContent = block.lang || 'text';
    var btn = el('button', 'asst-code-copy');
    btn.type = 'button';
    btn.textContent = 'Copy';
    btn.setAttribute('aria-label', 'Copy code' + (block.lang ? ' (' + block.lang + ')' : ''));
    btn.addEventListener('click', function () { copyText(block.text, btn); });
    head.appendChild(label);
    head.appendChild(btn);
    var pre = el('pre');
    var code = el('code');
    if (block.lang) {
      code.className = 'language-' + block.lang.replace(/[^\w+#.-]/g, '');
      code.setAttribute('data-asst-lang', block.lang.toLowerCase());
    }
    code.textContent = block.text;
    pre.appendChild(code);
    wrap.appendChild(head);
    wrap.appendChild(pre);
    if (block.lang && block.closed !== false) requestHighlight(code, block.lang.toLowerCase());
    return wrap;
  }

  function renderBlock(block) {
    switch (block.type) {
      case 'code': return renderCode(block);
      case 'heading': {
        var h = el('h' + Math.min(6, block.level + 2), 'asst-h');   // model h1 → h3: stays below the panel title
        renderInline(block.text, h);
        return h;
      }
      case 'hr': return el('hr');
      case 'quote': {
        var bq = el('blockquote');
        block.children.forEach(function (b) { bq.appendChild(renderBlock(b)); });
        return bq;
      }
      case 'table': {
        var wrap = el('div', 'asst-table-wrap');
        var table = el('table');
        var thead = el('thead');
        var tr = el('tr');
        block.header.forEach(function (cell, idx) {
          var th = el('th');
          th.setAttribute('scope', 'col');
          if (block.aligns[idx]) th.style.textAlign = block.aligns[idx];
          renderInline(cell, th);
          tr.appendChild(th);
        });
        thead.appendChild(tr);
        table.appendChild(thead);
        var tbody = el('tbody');
        block.rows.forEach(function (row) {
          var r = el('tr');
          for (var i = 0; i < block.header.length; i++) {
            var td = el('td');
            if (block.aligns[i]) td.style.textAlign = block.aligns[i];
            renderInline(row[i] || '', td);
            r.appendChild(td);
          }
          tbody.appendChild(r);
        });
        table.appendChild(tbody);
        wrap.appendChild(table);
        return wrap;
      }
      case 'list': {
        var list = el(block.ordered ? 'ol' : 'ul');
        if (block.ordered && block.start && block.start !== 1) list.setAttribute('start', String(block.start));
        block.items.forEach(function (children) {
          var li = el('li');
          if (children.length === 1 && children[0].type === 'paragraph') {
            renderInline(children[0].text, li);
          } else {
            children.forEach(function (b) { li.appendChild(renderBlock(b)); });
          }
          list.appendChild(li);
        });
        return list;
      }
      default: {
        var p = el('p');
        renderInline(block.text || '', p);
        return p;
      }
    }
  }

  function render(src, container) {
    while (container.firstChild) container.removeChild(container.firstChild);
    parseBlocks(src).forEach(function (b) { container.appendChild(renderBlock(b)); });
    return container;
  }

  // Incremental renderer for streamed text.
  function createStreamRenderer(container) {
    var frozen = 0;          // number of blocks already rendered for good
    var tail = null;         // container node of the trailing block(s)
    return {
      update: function (full) {
        var blocks = parseBlocks(full);
        // A block is complete once another block follows it.
        while (frozen < blocks.length - 1) {
          if (tail) { container.removeChild(tail); tail = null; }
          container.appendChild(renderBlock(blocks[frozen]));
          frozen++;
        }
        if (tail) { container.removeChild(tail); tail = null; }
        if (blocks.length > frozen) {
          tail = el('div', 'asst-md-tail');
          tail.appendChild(renderBlock(blocks[blocks.length - 1]));
          container.appendChild(tail);
        }
      },
      finish: function (full) { render(full, container); frozen = 0; tail = null; },
    };
  }

  var api = { render: render, parseBlocks: parseBlocks, renderInline: renderInline,
              createStreamRenderer: createStreamRenderer, safeHref: safeHref };
  root.vivAssistantMarkdown = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
