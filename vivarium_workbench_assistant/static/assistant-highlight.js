// assistant-highlight.js — a small, dependency-free syntax highlighter.
//
// Lazy-loaded by assistant-markdown.js the first time a fenced code block with
// a language tag appears; served from the extension's own static directory
// (never a CDN). It re-builds a <code> element's children from TEXT NODES and
// <span class="tok-…"> wrappers — the code text is never parsed as HTML.
// Colours come from the --syntax-* design tokens (assistant.css), so both
// themes work.
//
// Languages: python, yaml, json, bash/shell, diff, javascript/typescript.
// Anything else stays plain text.
//
// window.vivAssistantHighlight = { highlight(codeEl, lang), tokenize(src, lang) }
(function (root) {
  'use strict';

  var MAX_CHARS = 100000;

  function words(list) {
    var set = {};
    list.split(/\s+/).forEach(function (w) { if (w) set[w] = true; });
    return set;
  }

  var PY_KW = words('def class return if elif else for while in not and or import from as with try except ' +
    'finally raise pass break continue lambda yield async await global nonlocal assert del is match case');
  var PY_CONST = words('True False None');
  var JS_KW = words('var let const function return if else for while do in of new this class extends super ' +
    'import from export default try catch finally throw switch case break continue typeof instanceof void ' +
    'async await yield delete interface type enum implements readonly public private protected static');
  var JS_CONST = words('true false null undefined NaN Infinity');
  var SH_KW = words('if then else elif fi for in do done case esac while until function export local return ' +
    'source set unset readonly');
  var YAML_CONST = words('true false null yes no on off True False Null ~');

  var STR_DQ = /"(?:\\.|[^"\\\n])*"?/y;
  var STR_SQ = /'(?:\\.|[^'\\\n])*'?/y;
  var NUM = /-?\b\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?\b|0[xX][0-9a-fA-F]+/y;
  var IDENT = /[A-Za-z_$][\w$]*/y;

  function ident(kw, consts) {
    return function (m) {
      if (consts && consts[m]) return 'tok-boolean';
      if (kw && kw[m]) return 'tok-keyword';
      return null;
    };
  }

  var LANGS = {
    python: [
      [/#[^\n]*/y, 'tok-comment'],
      [/(?:[rRbBuUfF]{1,2})?(?:"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$))/y, 'tok-string'],
      [/(?:[rRbBuUfF]{1,2})?"(?:\\.|[^"\\\n])*"?/y, 'tok-string'],
      [/(?:[rRbBuUfF]{1,2})?'(?:\\.|[^'\\\n])*'?/y, 'tok-string'],
      [/@[\w.]+/y, 'tok-keyword'],
      [NUM, 'tok-number'],
      [IDENT, ident(PY_KW, PY_CONST)],
    ],
    javascript: [
      [/\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|$)/y, 'tok-comment'],
      [/`(?:\\.|[^`\\])*`?/y, 'tok-string'],
      [STR_DQ, 'tok-string'],
      [STR_SQ, 'tok-string'],
      [NUM, 'tok-number'],
      [IDENT, ident(JS_KW, JS_CONST)],
    ],
    bash: [
      [/(^|(?<=\s))#[^\n]*/y, 'tok-comment'],
      [STR_DQ, 'tok-string'],
      [STR_SQ, 'tok-string'],
      [/\$\{?[\w@#?*!-]+\}?/y, 'tok-keyword'],
      [NUM, 'tok-number'],
      [IDENT, ident(SH_KW, null)],
    ],
    json: [
      [/"(?:\\.|[^"\\\n])*"(?=\s*:)/y, 'tok-key'],
      [STR_DQ, 'tok-string'],
      [NUM, 'tok-number'],
      [/\b(?:true|false|null)\b/y, 'tok-boolean'],
    ],
    yaml: [
      [/(^|(?<=\s))#[^\n]*/y, 'tok-comment'],
      [/(?<=^[ \t-]*)[\w.\-/"'$]+(?=[ \t]*:(?:\s|$))/my, 'tok-key'],
      [STR_DQ, 'tok-string'],
      [STR_SQ, 'tok-string'],
      [/[&*][\w-]+/y, 'tok-keyword'],
      [NUM, 'tok-number'],
      [/[A-Za-z_~][\w]*/y, ident(null, YAML_CONST)],
    ],
  };
  LANGS.py = LANGS.python;
  LANGS.js = LANGS.ts = LANGS.typescript = LANGS.jsx = LANGS.tsx = LANGS.javascript;
  LANGS.sh = LANGS.shell = LANGS.zsh = LANGS.console = LANGS.bash;
  LANGS.yml = LANGS.yaml;

  function tokenizeWith(src, rules) {
    var out = [];
    var plain = '';
    var i = 0;
    while (i < src.length) {
      var hit = null;
      for (var r = 0; r < rules.length; r++) {
        var re = rules[r][0];
        re.lastIndex = i;
        var m = re.exec(src);
        if (m && m.index === i && m[0].length > 0) { hit = [rules[r][1], m[0]]; break; }
      }
      if (hit) {
        var cls = typeof hit[0] === 'function' ? hit[0](hit[1]) : hit[0];
        if (cls) {
          if (plain) { out.push([null, plain]); plain = ''; }
          out.push([cls, hit[1]]);
        } else {
          plain += hit[1];
        }
        i += hit[1].length;
      } else {
        plain += src.charAt(i);
        i++;
      }
    }
    if (plain) out.push([null, plain]);
    return out;
  }

  function tokenizeDiff(src) {
    return src.split(/(?<=\n)/).map(function (line) {
      if (/^(\+\+\+|---|diff |index |@@)/.test(line)) return ['tok-meta', line];
      if (line.charAt(0) === '+') return ['tok-add', line];
      if (line.charAt(0) === '-') return ['tok-del', line];
      return [null, line];
    });
  }

  function tokenize(src, lang) {
    var l = String(lang || '').toLowerCase();
    if (l === 'diff' || l === 'patch') return tokenizeDiff(src);
    var rules = LANGS[l];
    if (!rules) return [[null, src]];
    return tokenizeWith(src, rules);
  }

  function highlight(codeEl, lang) {
    if (!codeEl || codeEl.getAttribute('data-asst-highlighted')) return;
    var src = codeEl.textContent || '';
    codeEl.setAttribute('data-asst-highlighted', '1');
    if (src.length > MAX_CHARS) return;
    var toks = tokenize(src, lang);
    if (toks.length === 1 && toks[0][0] === null) return;
    var doc = codeEl.ownerDocument;
    while (codeEl.firstChild) codeEl.removeChild(codeEl.firstChild);
    toks.forEach(function (t) {
      if (t[0]) {
        var span = doc.createElement('span');
        span.className = t[0];
        span.textContent = t[1];
        codeEl.appendChild(span);
      } else {
        codeEl.appendChild(doc.createTextNode(t[1]));
      }
    });
  }

  var api = { highlight: highlight, tokenize: tokenize };
  root.vivAssistantHighlight = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
