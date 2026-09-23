// tests/js/_mini_dom.js — a tiny DOM for Node tests of the assistant frontend.
//
// Implements only what assistant-markdown.js / assistant-highlight.js use.
// Setting innerHTML/outerHTML THROWS, so a test fails if the code under test
// ever parses a string as HTML.
'use strict';

function Text(data) {
  this.nodeType = 3;
  this.data = String(data);
  this.parentNode = null;
}
Object.defineProperty(Text.prototype, 'textContent', {
  get: function () { return this.data; },
  set: function (v) { this.data = String(v); },
});

function Element(tag, doc) {
  this.nodeType = 1;
  this.tagName = String(tag).toUpperCase();
  this.childNodes = [];
  this.attributes = {};
  this.className = '';
  this.style = {};
  this.parentNode = null;
  this.ownerDocument = doc;
  this.listeners = {};
}
Element.prototype.appendChild = function (c) {
  if (c.parentNode) c.parentNode.removeChild(c);
  c.parentNode = this;
  this.childNodes.push(c);
  return c;
};
Element.prototype.removeChild = function (c) {
  var i = this.childNodes.indexOf(c);
  if (i !== -1) this.childNodes.splice(i, 1);
  c.parentNode = null;
  return c;
};
Element.prototype.insertBefore = function (c, ref) {
  if (!ref) return this.appendChild(c);
  if (c.parentNode) c.parentNode.removeChild(c);
  c.parentNode = this;
  this.childNodes.splice(this.childNodes.indexOf(ref), 0, c);
  return c;
};
Element.prototype.replaceChild = function (n, old) {
  this.insertBefore(n, old);
  return this.removeChild(old);
};
Object.defineProperty(Element.prototype, 'firstChild', { get: function () { return this.childNodes[0] || null; } });
Object.defineProperty(Element.prototype, 'children', {
  get: function () { return this.childNodes.filter(function (n) { return n.nodeType === 1; }); },
});
Object.defineProperty(Element.prototype, 'textContent', {
  get: function () { return this.childNodes.map(function (n) { return n.textContent; }).join(''); },
  set: function (v) {
    this.childNodes.forEach(function (n) { n.parentNode = null; });
    this.childNodes = [];
    if (v !== '' && v !== null && v !== undefined) this.appendChild(new Text(v));
  },
});
['innerHTML', 'outerHTML'].forEach(function (prop) {
  Object.defineProperty(Element.prototype, prop, {
    get: function () { throw new Error(prop + ' read in test DOM'); },
    set: function () { throw new Error(prop + ' is forbidden: untrusted text must never be parsed as HTML'); },
  });
});
Element.prototype.insertAdjacentHTML = function () { throw new Error('insertAdjacentHTML is forbidden'); };
Element.prototype.setAttribute = function (k, v) { this.attributes[k] = String(v); if (k === 'class') this.className = String(v); };
Element.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; };
Element.prototype.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attributes, k); };
Element.prototype.removeAttribute = function (k) { delete this.attributes[k]; };
Element.prototype.addEventListener = function (t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); };
Object.defineProperty(Element.prototype, 'classList', {
  get: function () {
    var el = this;
    return {
      add: function (c) { if ((' ' + el.className + ' ').indexOf(' ' + c + ' ') === -1) el.className = (el.className + ' ' + c).trim(); },
      contains: function (c) { return (' ' + el.className + ' ').indexOf(' ' + c + ' ') !== -1; },
      remove: function (c) { el.className = (' ' + el.className + ' ').replace(' ' + c + ' ', ' ').trim(); },
    };
  },
});
// Attribute-free selectors only: "tag", "tag.class"; enough for assertions.
Element.prototype.querySelectorAll = function (sel) {
  var out = [];
  var m = /^([a-z0-9]*)(?:\.([\w-]+))?/i.exec(sel) || [];
  var tag = (m[1] || '').toUpperCase();
  var cls = m[2];
  (function walk(n) {
    n.childNodes.forEach(function (c) {
      if (c.nodeType !== 1) return;
      if ((!tag || c.tagName === tag) && (!cls || c.classList.contains(cls))) out.push(c);
      walk(c);
    });
  })(this);
  return out;
};
Element.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };

function makeDocument() {
  var doc = {};
  doc.createElement = function (t) { return new Element(t, doc); };
  doc.createTextNode = function (t) { return new Text(t); };
  doc.createElementNS = function (_ns, t) { return new Element(t, doc); };
  doc.documentElement = new Element('html', doc);
  doc.head = new Element('head', doc);
  doc.body = new Element('body', doc);
  doc.documentElement.appendChild(doc.head);
  doc.documentElement.appendChild(doc.body);
  doc.querySelectorAll = function (s) { return doc.documentElement.querySelectorAll(s); };
  doc.querySelector = function (s) { return doc.documentElement.querySelector(s); };
  doc.readyState = 'complete';
  doc.addEventListener = function () {};
  return doc;
}

// Serialise a subtree for structural comparisons.
function serialize(n) {
  if (n.nodeType === 3) return JSON.stringify(n.data);
  var attrs = Object.keys(n.attributes).sort().map(function (k) { return k + '=' + n.attributes[k]; }).join(',');
  return '<' + n.tagName + (n.className ? '.' + n.className : '') + (attrs ? '[' + attrs + ']' : '') + '>' +
    n.childNodes.map(serialize).join('') + '</' + n.tagName + '>';
}

module.exports = { makeDocument: makeDocument, serialize: serialize, Element: Element, Text: Text };
