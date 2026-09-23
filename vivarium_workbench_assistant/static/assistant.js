// assistant.js — the assistant panel (mounted into the core's #viv-sidepanel).
//
// State machine per conversation: idle → composing → streaming → done/error/cancelled.
// Everything the model says is rendered by assistant-markdown.js (DOM only).
// Streaming text is batched per animation frame; only the trailing Markdown
// block is re-rendered while it grows.
//
// Accessibility: the host <aside> is role=complementary ("Assistant"); the
// streamed text is NOT a live region — a separate visually hidden role=status
// announces "Assistant is responding" / "Response complete", and errors use
// role=alert. Enter sends, Shift+Enter inserts a newline, Escape stops a
// streaming reply (or returns focus to the page). Ctrl/⌘+Shift+. toggles the
// panel (configurable in Settings).
//
// Privacy: the header always shows which provider/model receives a message and
// whether it is Local or Cloud; the footer shows the estimated tokens that
// will be sent; before the first cloud send in a conversation the full
// context preview is shown (a preference, on by default).
(function (root) {
  'use strict';

  var doc = root.document;
  var BASE = '/api/ext/assistant';
  var SELECTION_KEY = 'viv.assistant.selection';     // {instance, model}: a per-browser convenience, no secrets
  var LOADING_HINT_MS = 5000;
  var SECRET_SHAPES = [
    /\bsk-ant-[A-Za-z0-9_-]{8,}/, /\bsk-or-[A-Za-z0-9_-]{8,}/, /\bsk-[A-Za-z0-9_-]{16,}/,
    /\bAIza[0-9A-Za-z_-]{30,}/, /\bya29\.[0-9A-Za-z_.-]{10,}/, /\bgh[opusr]_[A-Za-z0-9_]{20,}/,
    /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/, /\bAKIA[0-9A-Z]{16}\b/,
  ];

  var MD = root.vivAssistantMarkdown;
  var ST = root.vivAssistantStream;
  var DF = root.vivAssistantDiff;

  var S = {
    status: null, providers: [], prefs: null, models: {},
    instance: null, model: null, agent: false,
    conversations: [], convId: null, conv: null, activeLeaf: null,
    tray: [], autoOff: false, cloudConfirmed: {},
    stream: null, streamMsg: null, lastError: null, previewTimer: null, lastPreview: null,
  };
  var E = {};          // DOM references

  // ── Utilities ─────────────────────────────────────────────────────────────
  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function btn(label, onClick, cls, aria) {
    var b = el('button', cls || 'asst-btn', label);
    b.type = 'button';
    if (aria) b.setAttribute('aria-label', aria);
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }
  function icon(pathD) {
    var ns = 'http://www.w3.org/2000/svg';
    var svg = doc.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    var p = doc.createElementNS(ns, 'path');
    p.setAttribute('d', pathD);
    svg.appendChild(p);
    return svg;
  }
  function iconBtn(pathD, label, onClick) {
    var b = btn('', onClick, 'asst-icon-btn', label);
    b.title = label;
    b.appendChild(icon(pathD));
    return b;
  }
  function api(method, path, body) {
    return root.fetch(BASE + path, {
      method: method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.error || ('HTTP ' + r.status)); e.status = r.status; e.body = j; throw e; }
        return j;
      });
    });
  }
  function readSel() {
    try { return JSON.parse(root.localStorage.getItem(SELECTION_KEY) || 'null'); } catch (e) { return null; }
  }
  function writeSel() {
    try { root.localStorage.setItem(SELECTION_KEY, JSON.stringify({ instance: S.instance, model: S.model })); } catch (e) { /* private mode */ }
  }
  function announce(text) { if (E.live) { E.live.textContent = ''; E.live.textContent = text; } }
  function showAlert(text) {
    if (!E.alert) return;
    E.alert.hidden = !text;
    E.alert.textContent = text || '';
  }
  function fmtTokens(n) { return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n); }
  function provider() { return S.providers.filter(function (p) { return p.id === S.instance; })[0] || null; }

  // ── Page context adapter ──────────────────────────────────────────────────
  var SLUG = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
  function currentStudy() {
    var s = root._studyDetailCurrent;
    if (typeof s === 'string' && SLUG.test(s)) return s;
    var frames = doc.querySelectorAll('iframe');
    for (var i = 0; i < frames.length; i++) {
      var f = frames[i];
      if (!f.offsetParent) continue;
      try {
        var n = f.contentDocument && f.contentDocument.getElementById('study-name');
        var slug = n && n.getAttribute('data-slug');
        if (slug && SLUG.test(slug)) return slug;
      } catch (e) { /* cross-origin frame */ }
    }
    return null;
  }
  function currentInvestigation() {
    var v = root._currentIsetSlug || root._currentInvestigation;
    return typeof v === 'string' && SLUG.test(v) ? v : null;
  }
  function currentComposite() {
    var page = (root.location.hash || '').replace(/^#/, '');
    if (page !== 'composite-explore') return null;
    var n = doc.getElementById('ce-id');
    var id = n && n.textContent && n.textContent.trim();
    return id && /^[A-Za-z_][A-Za-z0-9_.]{0,199}$/.test(id) ? id : null;
  }
  function pageSummarySpec() {
    var page = (root.location.hash || '').replace(/^#/, '').split(/[?&/]/)[0];
    var spec = { kind: 'page_summary' };
    if (/^[a-z][a-z0-9-]{0,40}$/.test(page)) spec.page = page;
    var inv = currentInvestigation();
    if (inv) spec.investigation = inv;
    var st = currentStudy();
    if (st) spec.study = st;
    var comp = currentComposite();
    if (comp) spec.composite = comp;
    return spec;
  }
  function contextSpecs() {
    var specs = [];
    var mode = (S.prefs && S.prefs.auto_context) || 'page_summary';
    if (!S.autoOff && mode !== 'off') {
      specs.push(pageSummarySpec());
      if (mode === 'page_and_selection') {
        var st = currentStudy();
        if (st && !S.tray.some(function (t) { return t.kind === 'study' && t.slug === st; })) {
          specs.push({ kind: 'study', slug: st });
        }
      }
    }
    return specs.concat(S.tray.map(function (t) { var c = {}; Object.keys(t).forEach(function (k) { if (k !== '_label') c[k] = t[k]; }); return c; }));
  }

  // ── Layout ────────────────────────────────────────────────────────────────
  function build(host) {
    var wrap = el('div', 'asst');
    // Header
    var head = el('div', 'asst-head');
    var title = el('h2', 'asst-title', 'Assistant');
    title.id = 'asst-title';
    head.appendChild(title);
    var hActions = el('div', 'asst-head-actions');
    E.historyBtn = iconBtn('M3 12a9 9 0 1 0 3-6.7M3 4v5h5M12 7v5l3 3', 'Conversation history', toggleHistory);
    E.historyBtn.setAttribute('aria-expanded', 'false');
    E.historyBtn.setAttribute('aria-controls', 'asst-history');
    hActions.appendChild(E.historyBtn);
    hActions.appendChild(iconBtn('M12 5v14M5 12h14', 'New conversation', newConversation));
    hActions.appendChild(iconBtn('M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
                                 'Assistant settings', openSettings));
    hActions.appendChild(iconBtn('M18 6 6 18M6 6l12 12', 'Close assistant', function () {
      if (root.vivSidepanel) root.vivSidepanel.close();
    }));
    head.appendChild(hActions);
    wrap.appendChild(head);

    // Provider / model bar
    var bar = el('div', 'asst-modelbar');
    E.chip = btn('', toggleModelMenu, 'asst-chip');
    E.chip.setAttribute('aria-haspopup', 'dialog');
    E.chip.setAttribute('aria-expanded', 'false');
    bar.appendChild(E.chip);
    E.statusText = el('span', 'asst-status-text');
    bar.appendChild(E.statusText);
    var agentLabel = el('label', 'asst-agent-toggle');
    E.agent = el('input');
    E.agent.type = 'checkbox';
    E.agent.addEventListener('change', function () { S.agent = E.agent.checked; updateEgress(); });
    agentLabel.appendChild(E.agent);
    agentLabel.appendChild(doc.createTextNode(' Agent'));
    agentLabel.title = 'Let the assistant read files, propose edits and (with your approval) run checks.';
    bar.appendChild(agentLabel);
    wrap.appendChild(bar);
    E.modelMenu = el('div', 'asst-model-menu');
    E.modelMenu.hidden = true;
    E.modelMenu.setAttribute('role', 'dialog');
    E.modelMenu.setAttribute('aria-label', 'Choose provider and model');
    wrap.appendChild(E.modelMenu);

    // History drawer
    E.history = el('div', 'asst-history');
    E.history.id = 'asst-history';
    E.history.hidden = true;
    wrap.appendChild(E.history);

    // Messages
    E.messages = el('div', 'asst-messages');
    E.messages.setAttribute('tabindex', '-1');
    E.messages.setAttribute('aria-label', 'Conversation');
    wrap.appendChild(E.messages);

    E.live = el('div', 'viv-sr-only');
    E.live.setAttribute('role', 'status');
    E.live.setAttribute('aria-live', 'polite');
    wrap.appendChild(E.live);
    E.alert = el('div', 'asst-alert');
    E.alert.setAttribute('role', 'alert');
    E.alert.hidden = true;
    wrap.appendChild(E.alert);

    // Context tray
    E.tray = el('div', 'asst-tray');
    E.tray.setAttribute('aria-label', 'Context sent with the next message');
    wrap.appendChild(E.tray);

    // Composer
    var form = el('form', 'asst-composer');
    E.input = el('textarea', 'asst-input');
    E.input.rows = 3;
    E.input.placeholder = 'Ask about this workspace…';
    E.input.setAttribute('aria-label', 'Message the assistant');
    E.input.addEventListener('keydown', onComposerKey);
    E.input.addEventListener('input', onComposerInput);
    form.appendChild(E.input);
    E.secretWarn = el('p', 'asst-warning');
    E.secretWarn.hidden = true;
    form.appendChild(E.secretWarn);
    var foot = el('div', 'asst-composer-foot');
    E.egress = el('span', 'asst-egress');
    foot.appendChild(E.egress);
    foot.appendChild(btn('Preview', function () { showPreview(false); }, 'asst-btn asst-btn-quiet',
                         'Preview what will be sent'));
    E.send = el('button', 'asst-btn asst-btn-primary', 'Send');
    E.send.type = 'submit';
    foot.appendChild(E.send);
    E.stop = btn('Stop', stopStreaming, 'asst-btn asst-btn-danger');
    E.stop.hidden = true;
    foot.appendChild(E.stop);
    form.appendChild(foot);
    form.addEventListener('submit', function (e) { e.preventDefault(); send(); });
    wrap.appendChild(form);

    host.appendChild(wrap);
  }

  // ── Header / status ───────────────────────────────────────────────────────
  function setStatus(text) { if (E.statusText) E.statusText.textContent = text; }

  function renderChip() {
    var p = provider();
    while (E.chip.firstChild) E.chip.removeChild(E.chip.firstChild);
    if (!p) {
      E.chip.appendChild(doc.createTextNode(S.providers.length ? 'Choose a provider ▾' : 'No provider configured'));
      return;
    }
    var dot = el('span', 'asst-dot ' + (p.credential_status && p.credential_status.configured ? 'asst-dot-ok' : 'asst-dot-bad'));
    dot.setAttribute('aria-hidden', 'true');
    E.chip.appendChild(dot);
    E.chip.appendChild(doc.createTextNode(p.display_name + ' · ' + (S.model || 'choose a model') + ' '));
    E.chip.appendChild(el('span', 'asst-badge asst-badge-' + p.locality, p.locality === 'local' ? 'Local' : 'Cloud'));
    E.chip.appendChild(doc.createTextNode(' ▾'));
    E.chip.setAttribute('aria-label', 'Provider ' + p.display_name + ', model ' + (S.model || 'not chosen') +
                        ', ' + (p.locality === 'local' ? 'local' : 'cloud') + '. Change');
  }

  function statusFor() {
    var p = provider();
    if (!p) return 'Not configured';
    if (!(p.credential_status && p.credential_status.configured)) return 'Not configured';
    if (S.stream) return 'Responding…';
    var err = S.lastError;
    if (err) {
      if (err.kind === 'auth' || err.kind === 'permission') return 'Auth error';
      if (err.kind === 'network' || err.kind === 'timeout') return 'Unreachable';
      if (err.kind === 'rate_limited') return 'Rate-limited';
    }
    return S.model ? 'Ready' : 'Choose a model';
  }

  function toggleModelMenu() {
    var open = E.modelMenu.hidden;
    E.modelMenu.hidden = !open;
    E.chip.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) renderModelMenu();
  }

  function renderModelMenu() {
    var m = E.modelMenu;
    while (m.firstChild) m.removeChild(m.firstChild);
    var search = el('input', 'asst-input-small');
    search.type = 'search';
    search.placeholder = 'Filter models…';
    search.setAttribute('aria-label', 'Filter models');
    m.appendChild(search);
    var list = el('div', 'asst-model-list');
    m.appendChild(list);
    function draw() {
      while (list.firstChild) list.removeChild(list.firstChild);
      var q = search.value.toLowerCase();
      S.providers.filter(function (p) { return p.enabled; }).forEach(function (p) {
        var group = el('div', 'asst-model-group');
        var gh = el('div', 'asst-model-group-head');
        var dot = el('span', 'asst-dot ' + (p.credential_status && p.credential_status.configured ? 'asst-dot-ok' : 'asst-dot-bad'));
        dot.setAttribute('aria-hidden', 'true');
        gh.appendChild(dot);
        gh.appendChild(el('strong', null, p.display_name + ' '));
        gh.appendChild(el('span', 'asst-badge asst-badge-' + p.locality, p.locality === 'local' ? 'Local' : 'Cloud'));
        gh.appendChild(el('span', 'asst-muted', ' ' + ((p.credential_status || {}).source || '')));
        group.appendChild(gh);
        var models = ((S.models[p.id] || {}).models || []);
        models.filter(function (x) { return !q || x.id.toLowerCase().indexOf(q) !== -1 ||
                                          (x.display_name || '').toLowerCase().indexOf(q) !== -1; })
          .slice(0, 100).forEach(function (x) {
            var b = btn((x.display_name || x.id) + (x.context_window ? ' · ' + fmtTokens(x.context_window) : ''),
                        function () {
                          S.instance = p.id; S.model = x.id; writeSel();
                          toggleModelMenu(); refreshUi(); updateEgress(); E.chip.focus();
                        }, 'asst-model-option' + (S.instance === p.id && S.model === x.id ? ' asst-selected' : ''));
            b.setAttribute('aria-pressed', S.instance === p.id && S.model === x.id ? 'true' : 'false');
            group.appendChild(b);
          });
        if (!models.length) group.appendChild(el('p', 'asst-muted', 'No models — open Settings to discover or add them.'));
        list.appendChild(group);
      });
      list.appendChild(btn('Manage providers…', openSettings, 'asst-btn asst-btn-quiet'));
    }
    search.addEventListener('input', draw);
    search.addEventListener('keydown', function (e) { if (e.key === 'Escape') { toggleModelMenu(); E.chip.focus(); } });
    draw();
    search.focus();
  }

  function refreshUi() {
    renderChip();
    setStatus(statusFor());
    var info = modelInfo();
    var tools = info && info.caps && info.caps.indexOf('tools') !== -1;
    E.agent.disabled = !tools;
    E.agent.parentNode.title = tools ? 'Let the assistant read files, propose edits and (with your approval) run checks.'
      : 'This model is not marked as supporting tools (Settings → AI Assistant → model capabilities).';
    if (!tools) { E.agent.checked = false; S.agent = false; }
    E.send.disabled = !!S.stream;
    E.send.hidden = !!S.stream;
    E.stop.hidden = !S.stream;
    renderTray();
  }

  function modelInfo() {
    var list = (S.models[S.instance] || {}).models || [];
    return list.filter(function (m) { return m.id === S.model; })[0] || null;
  }

  // ── Context tray ──────────────────────────────────────────────────────────
  function chipLabel(spec) {
    switch (spec.kind) {
      case 'study': return 'study: ' + spec.slug;
      case 'investigation': return 'investigation: ' + spec.slug;
      case 'composite': return 'composite: ' + spec.id;
      case 'run_log': return 'run log: ' + spec.run_id;
      case 'git_diff': return 'git diff';
      case 'manifest': return 'workspace manifest';
      case 'file': return spec.path + (spec.include_ignored ? ' (gitignored)' : '');
      case 'search': return 'search: ' + spec.query;
      case 'paste': return spec.label || 'pasted text';
      default: return spec.kind;
    }
  }

  function renderTray() {
    var t = E.tray;
    while (t.firstChild) t.removeChild(t.firstChild);
    t.appendChild(el('span', 'asst-tray-label', 'Context:'));
    var mode = (S.prefs && S.prefs.auto_context) || 'page_summary';
    if (mode !== 'off' && !S.autoOff) {
      var auto = el('span', 'asst-ctx-chip asst-ctx-auto');
      auto.appendChild(doc.createTextNode(mode === 'page_and_selection' ? 'page + open study' : 'page summary'));
      auto.appendChild(btn('×', function () { S.autoOff = true; renderTray(); updateEgress(); },
                           'asst-chip-x', 'Remove automatic page context'));
      t.appendChild(auto);
    } else if (mode !== 'off') {
      t.appendChild(btn('+ page summary', function () { S.autoOff = false; renderTray(); updateEgress(); }, 'asst-ctx-add'));
    }
    S.tray.forEach(function (spec, idx) {
      var c = el('span', 'asst-ctx-chip');
      c.appendChild(doc.createTextNode(chipLabel(spec)));
      c.appendChild(btn('×', function () { S.tray.splice(idx, 1); renderTray(); updateEgress(); },
                        'asst-chip-x', 'Remove ' + chipLabel(spec)));
      t.appendChild(c);
    });
    var add = btn('+ Add context', openAddContext, 'asst-ctx-add');
    add.setAttribute('aria-haspopup', 'menu');
    t.appendChild(add);
  }

  function addSpec(spec) {
    S.tray.push(spec);
    renderTray();
    updateEgress();
  }

  function openAddContext() {
    var items = [];
    var st = currentStudy();
    if (st) items.push(['Current study (' + st + ')', function () { addSpec({ kind: 'study', slug: st }); }]);
    var inv = currentInvestigation();
    if (inv) items.push(['Current investigation (' + inv + ')', function () { addSpec({ kind: 'investigation', slug: inv }); }]);
    var comp = currentComposite();
    if (comp) items.push(['Open composite (' + comp + ')', function () { addSpec({ kind: 'composite', id: comp }); }]);
    items.push(['Git diff of the working tree', function () { addSpec({ kind: 'git_diff' }); }]);
    items.push(['Workspace manifest', function () { addSpec({ kind: 'manifest' }); }]);
    items.push(['A file…', pickFile]);
    items.push(['A run log…', function () { askText('Run log', 'Run id', '', function (v) { addSpec({ kind: 'run_log', run_id: v }); }); }]);
    items.push(['Search results…', function () { askText('Search the workspace', 'Text to find', '', function (v) { addSpec({ kind: 'search', query: v }); }); }]);
    items.push(['Paste text…', function () { askText('Paste text', 'Text (for example terminal output)', '', function (v) {
      addSpec({ kind: 'paste', label: 'pasted text', text: v });
    }, true); }]);
    var body = el('div', 'asst-menu-list');
    body.setAttribute('role', 'menu');
    var chosen = null;
    items.forEach(function (it) {
      var b = btn(it[0], function () { chosen = it[1]; closeBtn.click(); }, 'asst-menu-item');
      b.setAttribute('role', 'menuitem');
      body.appendChild(b);
    });
    var closeBtn = null;
    var p = DF.dialog({ title: 'Add context', body: body, actions: [{ label: 'Cancel', value: null, cancel: true }] });
    closeBtn = doc.querySelector('.asst-dialog [data-cancel]');
    p.then(function () { if (chosen) chosen(); });
    var first = body.querySelector('button');
    if (first) first.focus();
  }

  function askText(title, label, initial, done, multiline) {
    var wrap = el('label', 'asst-field');
    wrap.appendChild(el('span', 'asst-field-label', label));
    var inp = multiline ? el('textarea') : el('input');
    if (!multiline) inp.type = 'text';
    else inp.rows = 8;
    inp.value = initial || '';
    wrap.appendChild(inp);
    var p = DF.dialog({ title: title, body: wrap, initialFocus: multiline ? 'textarea' : 'input',
                        actions: [{ label: 'Cancel', value: null, cancel: true }, { label: 'Add', value: true, primary: true }] });
    setTimeout(function () { inp.focus(); }, 0);
    p.then(function (ok) { if (ok && inp.value.trim()) done(inp.value.trim()); });
  }

  function pickFile() {
    var box = el('div', 'asst-file-picker');
    var crumb = el('p', 'asst-muted');
    var list = el('div', 'asst-file-list');
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', 'Workspace files');
    box.appendChild(crumb);
    box.appendChild(list);
    var picked = null;
    var dlg = DF.dialog({ title: 'Attach a file', body: box, actions: [{ label: 'Cancel', value: null, cancel: true }] });
    function load(path) {
      crumb.textContent = '/' + path;
      api('GET', '/files/list?path=' + encodeURIComponent(path)).then(function (r) {
        while (list.firstChild) list.removeChild(list.firstChild);
        if (path) {
          list.appendChild(btn('..', function () { load(path.split('/').slice(0, -1).join('/')); }, 'asst-file-item'));
        }
        r.entries.forEach(function (e) {
          var b = btn((e.type === 'dir' ? '📁 ' : '📄 ') + e.name, function () {
            if (e.type === 'dir') { load(e.path); return; }
            picked = e.path;
            var cancel = doc.querySelector('.asst-dialog [data-cancel]');
            if (cancel) cancel.click();
          }, 'asst-file-item');
          b.setAttribute('role', 'option');
          list.appendChild(b);
        });
        var f = list.querySelector('button');
        if (f) f.focus();
      }, function (e) { crumb.textContent = e.message; });
    }
    load('');
    dlg.then(function () { if (picked) addSpec({ kind: 'file', path: picked }); });
  }

  // ── Egress disclosure and preview ─────────────────────────────────────────
  function updateEgress() {
    clearTimeout(S.previewTimer);
    S.previewTimer = setTimeout(function () {
      var p = provider();
      if (!p || !S.model) { E.egress.textContent = p ? 'Choose a model to start.' : ''; return; }
      api('POST', '/context/preview', { context: contextSpecs(), provider_instance: p.id, model: S.model,
                                        message: E.input.value || '' })
        .then(function (r) {
          S.lastPreview = r;
          var tokens = r.total_tokens + Math.ceil((E.input.value || '').length / 4);
          E.egress.textContent = 'Sends ~' + fmtTokens(tokens) + ' tokens to ' + p.display_name + ' (' +
            (p.locality === 'local' ? 'local' : 'cloud') + ').';
          var errs = r.items.filter(function (i) { return i.error || i.needs_confirmation; });
          if (errs.length) E.egress.textContent += ' ' + errs.length + ' context item(s) need attention — Preview.';
        }, function () { E.egress.textContent = 'Sends to ' + p.display_name + ' (' + (p.locality === 'local' ? 'local' : 'cloud') + ').'; });
    }, 300);
  }

  function previewBody(r) {
    var p = provider();
    var box = el('div', 'asst-preview');
    var dest = el('p');
    dest.appendChild(el('strong', null, 'Destination: '));
    dest.appendChild(doc.createTextNode((p ? p.display_name : '?') + ' · ' + (S.model || '?') + ' · ' +
      (p && p.locality === 'local' ? 'Local' : 'Cloud — leaves this computer')));
    box.appendChild(dest);
    var ul = el('ul', 'asst-preview-list');
    (r.items || []).forEach(function (it) {
      var li = el('li');
      li.appendChild(el('strong', null, it.label));
      if (it.path) li.appendChild(el('code', 'asst-path', ' ' + it.path));
      li.appendChild(doc.createTextNode(' — ~' + fmtTokens(it.tokens || 0) + ' tokens' + (it.truncated ? ', truncated' : '') +
        (it.dropped ? ', left out (does not fit)' : '')));
      (it.flags || []).forEach(function (f) { li.appendChild(el('span', 'asst-badge', f)); });
      if (it.error) li.appendChild(el('p', 'asst-bad', it.error));
      if (it.needs_confirmation) li.appendChild(el('p', 'asst-warning', it.needs_confirmation));
      ul.appendChild(li);
    });
    if (!(r.items || []).length) ul.appendChild(el('li', 'asst-muted', 'No context — only your message.'));
    box.appendChild(ul);
    (r.warnings || []).forEach(function (w) { box.appendChild(el('p', 'asst-warning', w)); });
    if (r.message_secret_findings && r.message_secret_findings.length) {
      box.appendChild(el('p', 'asst-warning', 'Your message looks like it contains ' + r.message_secret_findings.join(', ') +
                                             '. It would be sent as typed.'));
    }
    box.appendChild(el('p', 'asst-muted', 'Estimated total: ~' + fmtTokens(r.total_tokens || 0) + ' tokens of context (budget ' +
                                          fmtTokens(r.budget_tokens || 0) + ').'));
    return box;
  }

  function showPreview(forSend) {
    var p = provider();
    return api('POST', '/context/preview', { context: contextSpecs(), provider_instance: p ? p.id : null,
                                             model: S.model, message: E.input.value || '' })
      .then(function (r) {
        return DF.dialog({
          title: forSend ? 'Send to ' + (p ? p.display_name : 'the provider') + '?' : 'What will be sent',
          body: previewBody(r),
          actions: forSend ? [{ label: 'Cancel', value: false, cancel: true }, { label: 'Send', value: true, primary: true }]
                           : [{ label: 'Close', value: null, cancel: true }],
        });
      }, function (e) { showAlert(e.message); return false; });
  }

  // ── Conversations ─────────────────────────────────────────────────────────
  function loadConversations() {
    return api('GET', '/conversations').then(function (r) {
      S.conversations = r.conversations;
      S.workspaceName = r.workspace;
      if (!E.history.hidden) renderHistory();
      return r;
    });
  }

  function openConversation(cid) {
    return api('GET', '/conversations/' + encodeURIComponent(cid)).then(function (c) {
      S.convId = cid;
      S.conv = c;
      S.activeLeaf = latestLeaf(c.messages);
      renderConversation();
    }, function (e) { showAlert(e.message); });
  }

  function newConversation() {
    if (S.stream) return;
    S.convId = null;
    S.conv = null;
    S.activeLeaf = null;
    S.autoOff = false;
    renderConversation();
    E.input.focus();
  }

  function toggleHistory() {
    E.history.hidden = !E.history.hidden;
    E.historyBtn.setAttribute('aria-expanded', E.history.hidden ? 'false' : 'true');
    if (!E.history.hidden) loadConversations().then(function () {
      var s = E.history.querySelector('input');
      if (s) s.focus();
    });
  }

  function renderHistory() {
    var h = E.history;
    while (h.firstChild) h.removeChild(h.firstChild);
    var search = el('input', 'asst-input-small');
    search.type = 'search';
    search.placeholder = 'Search conversations…';
    search.setAttribute('aria-label', 'Search conversations by title');
    h.appendChild(search);
    var list = el('ul', 'asst-history-list');
    h.appendChild(list);
    function draw() {
      while (list.firstChild) list.removeChild(list.firstChild);
      var q = search.value.toLowerCase();
      S.conversations.filter(function (c) { return !q || (c.title || '').toLowerCase().indexOf(q) !== -1; })
        .forEach(function (c) {
          var li = el('li', 'asst-history-item' + (c.id === S.convId ? ' asst-selected' : ''));
          li.appendChild(btn(c.title || 'Untitled', function () {
            openConversation(c.id);
            toggleHistory();
          }, 'asst-history-open'));
          li.appendChild(btn('Rename', function () {
            askText('Rename conversation', 'Title', c.title, function (v) {
              api('PATCH', '/conversations/' + c.id, { title: v }).then(loadConversations);
            });
          }, 'asst-btn asst-btn-quiet'));
          li.appendChild(btn('Delete', function () {
            DF.dialog({ title: 'Delete this conversation?', body: el('p', null, c.title || 'Untitled'),
                        actions: [{ label: 'Cancel', value: false, cancel: true }, { label: 'Delete', value: true, danger: true }] })
              .then(function (ok) {
                if (!ok) return;
                api('DELETE', '/conversations/' + c.id).then(function () {
                  if (S.convId === c.id) newConversation();
                  return loadConversations();
                });
              });
          }, 'asst-btn asst-btn-quiet'));
          list.appendChild(li);
        });
      if (!list.firstChild) list.appendChild(el('li', 'asst-muted', 'No conversations yet.'));
    }
    search.addEventListener('input', draw);
    draw();
    h.appendChild(btn('Delete all conversations for this workspace…', function () {
      askText('Delete all conversations', 'Type the workspace name (' + (S.workspaceName || '') + ') to confirm', '', function (v) {
        api('POST', '/conversations/delete-all', { confirm: v }).then(function () { newConversation(); return loadConversations(); },
                                                                       function (e) { showAlert(e.message); });
      });
    }, 'asst-btn asst-btn-danger'));
  }

  // ── Message rendering ─────────────────────────────────────────────────────
  function byId(list) { var m = {}; list.forEach(function (x) { m[x.id] = x; }); return m; }
  function latestLeaf(messages) {
    var parents = {};
    messages.forEach(function (m) { if (m.parent_id) parents[m.parent_id] = true; });
    var leaves = messages.filter(function (m) { return !parents[m.id]; });
    leaves.sort(function (a, b) { return (a.created_at || 0) - (b.created_at || 0); });
    return leaves.length ? leaves[leaves.length - 1].id : null;
  }
  function chainTo(messages, leaf) {
    var map = byId(messages);
    var out = [];
    var cur = map[leaf];
    var seen = {};
    while (cur && !seen[cur.id]) { seen[cur.id] = true; out.push(cur); cur = map[cur.parent_id]; }
    return out.reverse();
  }
  function textOf(m) {
    return (m.parts || []).filter(function (p) { return p.type === 'text'; }).map(function (p) { return p.text; }).join('');
  }

  function renderEmpty() {
    var box = el('div', 'asst-empty');
    if (!S.providers.length) {
      box.appendChild(el('p', null, 'Connect a model provider to start. Keys stay on the server — never in the browser.'));
      box.appendChild(btn('Connect a provider', openSettings, 'asst-btn asst-btn-primary'));
    } else {
      box.appendChild(el('p', null, 'Ask about this workspace — for example “Why is this study blocked?” or ' +
                                    '“Explain this composite’s wiring.”'));
      box.appendChild(el('p', 'asst-muted', 'Only a page summary is attached automatically. Add files or objects with “+ Add context”.'));
    }
    return box;
  }

  function contextRefs(manifest) {
    var wrap = el('div', 'asst-used-context');
    if (!manifest || !manifest.length) return wrap;
    wrap.appendChild(el('span', 'asst-muted', 'context: '));
    manifest.forEach(function (it) {
      var label = it.label + (it.truncated ? ' (truncated)' : '') + (it.dropped ? ' (left out)' : '');
      var b = btn(label, function () { navigateRef(it); }, 'asst-ref');
      b.title = (it.path || it.kind) + ' · ~' + fmtTokens(it.tokens || 0) + ' tokens';
      if (it.error) b.classList.add('asst-bad');
      wrap.appendChild(b);
    });
    return wrap;
  }

  function navigateRef(it) {
    if (it.kind === 'study' && it.label) {
      var slug = it.label.replace(/^study: /, '');
      if (typeof root._openStudyEmbeddedNewTab === 'function') { root.location.hash = '#investigations'; root._openStudyEmbeddedNewTab(slug); return; }
    }
    if (it.kind === 'investigation' && it.label) {
      var inv = it.label.replace(/^investigation: /, '');
      if (typeof root._showInvestigationWorkspace === 'function') { root.location.hash = '#investigations'; root._showInvestigationWorkspace(inv); return; }
    }
    if (it.path) viewFile(it.path);
  }

  function viewFile(path) {
    api('GET', '/files?path=' + encodeURIComponent(path)).then(function (r) {
      var box = el('div', 'asst-file-view');
      box.appendChild(el('p', 'asst-muted', r.size + ' bytes · sha256 ' + String(r.sha256).slice(0, 12) +
                                              (r.truncated ? ' · truncated' : '') + (r.gitignored ? ' · gitignored' : '')));
      var pre = el('pre');
      pre.textContent = r.text;
      box.appendChild(pre);
      DF.dialog({ title: r.path, body: box, actions: [{ label: 'Close', value: null, cancel: true }] });
    }, function (e) { showAlert(e.message); });
  }

  function renderConversation() {
    var m = E.messages;
    while (m.firstChild) m.removeChild(m.firstChild);
    showAlert('');
    if (!S.conv || !S.conv.messages.length) {
      m.appendChild(renderEmpty());
      refreshUi();
      return;
    }
    var all = S.conv.messages;
    var chain = chainTo(all, S.activeLeaf || latestLeaf(all));
    chain.forEach(function (msg) { m.appendChild(renderMessage(msg, all)); });
    m.scrollTop = m.scrollHeight;
    refreshUi();
  }

  function siblingsOf(msg, all) {
    return all.filter(function (x) { return x.role === msg.role && x.parent_id === msg.parent_id; })
      .sort(function (a, b) { return (a.created_at || 0) - (b.created_at || 0); });
  }

  function renderMessage(msg, all) {
    var box = el('article', 'asst-msg asst-msg-' + msg.role);
    box.setAttribute('data-message-id', msg.id);
    var who = el('div', 'asst-msg-who', msg.role === 'user' ? 'You' : 'Assistant');
    if (msg.role === 'assistant' && msg.provider_instance) {
      who.appendChild(el('span', 'asst-muted', ' · ' + msg.provider_instance + '/' + (msg.model || '')));
    }
    box.appendChild(who);
    if (msg.role === 'user') {
      var ut = el('div', 'asst-msg-text asst-user-text');
      ut.textContent = textOf(msg);
      box.appendChild(ut);
      box.appendChild(contextRefs(msg.context_manifest));
      return box;
    }
    var body = el('div', 'asst-msg-text asst-md');
    MD.render(textOf(msg), body);
    box.appendChild(body);
    if (msg.tool_events && msg.tool_events.length) box.appendChild(toolSummary(msg.tool_events));
    if (msg.proposal_id) DF.renderProposal(box, msg.proposal_id, { canApply: canApply(), onChange: function () {} });
    if (msg.status && msg.status !== 'complete') {
      var label = { cancelled: 'Stopped', interrupted: 'Interrupted', error: 'Error', streaming: 'Incomplete' }[msg.status] || msg.status;
      box.appendChild(el('p', 'asst-msg-state asst-' + msg.status, label + (msg.error ? ': ' + msg.error.message : '')));
    }
    var actions = el('div', 'asst-msg-actions');
    actions.appendChild(btn('Copy', function (e) { copy(textOf(msg), e.currentTarget); }, 'asst-btn asst-btn-quiet',
                            'Copy this reply'));
    if (msg.status === 'error' || msg.status === 'interrupted' || msg.status === 'cancelled') {
      actions.appendChild(btn('Retry', function () { rerun('retry', msg.parent_id); }, 'asst-btn asst-btn-quiet'));
    }
    actions.appendChild(btn('Regenerate', function () { rerun('regenerate', msg.parent_id); }, 'asst-btn asst-btn-quiet'));
    var sibs = siblingsOf(msg, all);
    if (sibs.length > 1) {
      var idx = sibs.map(function (s) { return s.id; }).indexOf(msg.id);
      var nav = el('span', 'asst-alt-nav');
      var prev = btn('‹', function () { S.activeLeaf = sibs[idx - 1].id; renderConversation(); }, 'asst-btn asst-btn-quiet', 'Previous version');
      prev.disabled = idx <= 0;
      var next = btn('›', function () { S.activeLeaf = sibs[idx + 1].id; renderConversation(); }, 'asst-btn asst-btn-quiet', 'Next version');
      next.disabled = idx >= sibs.length - 1;
      nav.appendChild(prev);
      nav.appendChild(el('span', 'asst-muted', (idx + 1) + '/' + sibs.length));
      nav.appendChild(next);
      actions.appendChild(nav);
    }
    box.appendChild(actions);
    return box;
  }

  function toolSummary(events) {
    var d = el('details', 'asst-tools');
    d.appendChild(el('summary', null, events.length + ' tool call' + (events.length === 1 ? '' : 's')));
    var ol = el('ol', 'asst-timeline');
    events.forEach(function (ev) {
      var li = el('li', ev.ok ? 'asst-ok' : 'asst-bad');
      li.appendChild(el('span', 'asst-glyph', ev.ok ? '✓ ' : '✗ '));
      li.appendChild(el('code', null, ev.name));
      li.appendChild(doc.createTextNode(' — ' + (ev.summary || '') + (ev.decision && ev.decision !== 'allow' ? ' (' + ev.decision + ')' : '')));
      ol.appendChild(li);
    });
    d.appendChild(ol);
    return d;
  }

  function canApply() { return !!(S.status && S.status.capabilities && S.status.capabilities.apply_edits); }

  function copy(text, button) {
    function done(ok) { var prev = button.textContent; button.textContent = ok ? 'Copied' : 'Copy failed'; setTimeout(function () { button.textContent = 'Copy'; }, 1500); }
    if (root.navigator && root.navigator.clipboard && root.isSecureContext !== false) {
      root.navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    } else {
      done(false);
    }
  }

  // ── Streaming ─────────────────────────────────────────────────────────────
  function beginStreamingView(userText, contextManifest) {
    if (!S.conv || !S.conv.messages.length) {
      while (E.messages.firstChild) E.messages.removeChild(E.messages.firstChild);
    }
    if (userText !== null) {
      var u = el('article', 'asst-msg asst-msg-user');
      u.appendChild(el('div', 'asst-msg-who', 'You'));
      var ut = el('div', 'asst-msg-text asst-user-text');
      ut.textContent = userText;
      u.appendChild(ut);
      S.pendingUserRefs = el('div', 'asst-used-context');
      u.appendChild(S.pendingUserRefs);
      E.messages.appendChild(u);
    }
    var a = el('article', 'asst-msg asst-msg-assistant asst-streaming');
    a.appendChild(el('div', 'asst-msg-who', 'Assistant'));
    var body = el('div', 'asst-msg-text asst-md');
    a.appendChild(body);
    var timeline = el('ol', 'asst-timeline');
    a.appendChild(timeline);
    var hint = el('p', 'asst-muted asst-loading', 'Waiting for the model…');
    a.appendChild(hint);
    E.messages.appendChild(a);
    E.messages.scrollTop = E.messages.scrollHeight;
    return { box: a, body: body, timeline: timeline, hint: hint, text: '', pending: '', frame: 0,
             renderer: MD.createStreamRenderer(body), tools: {}, gotToken: false };
  }

  function flush(sm) {
    sm.frame = 0;
    if (!sm.pending) return;
    var nearBottom = E.messages.scrollHeight - E.messages.scrollTop - E.messages.clientHeight < 80;
    sm.text += sm.pending;
    sm.pending = '';
    sm.renderer.update(sm.text);
    if (nearBottom) E.messages.scrollTop = E.messages.scrollHeight;
  }

  function onEvent(sm, name, data) {
    if (name === 'run.start') {
      if (S.pendingUserRefs) { S.pendingUserRefs.parentNode.replaceChild(contextRefs(data.context_manifest), S.pendingUserRefs); S.pendingUserRefs = null; }
      (data.warnings || []).forEach(function (w) { sm.box.appendChild(el('p', 'asst-warning', w)); });
      announce('Assistant is responding');
      return;
    }
    if (name === 'text.delta') {
      if (!sm.gotToken) { sm.gotToken = true; sm.hint.hidden = true; }
      sm.pending += data.text || '';
      if (!sm.frame) sm.frame = (root.requestAnimationFrame || setTimeout)(function () { flush(sm); });
      return;
    }
    if (name === 'notice') { sm.box.appendChild(el('p', 'asst-muted', data.message)); return; }
    if (name === 'tool.call') {
      sm.hint.hidden = true;
      var li = el('li', 'asst-tool-pending');
      li.appendChild(el('code', null, data.name));
      var summary = el('span', null, ' — ' + (data.requires_approval ? 'waiting for your approval' : 'running…'));
      li.appendChild(summary);
      sm.timeline.appendChild(li);
      sm.tools[data.id] = { li: li, summary: summary };
      if (data.requires_approval && data.approval_id) {
        setStatus('Waiting for approval');
        announce('The assistant is asking for approval to run ' + data.name);
        DF.approve(data).then(function (choice) {
          return api('POST', '/runs/' + encodeURIComponent(S.stream.runId()) + '/approvals/' + encodeURIComponent(data.approval_id),
                     { decision: choice.decision, scope: choice.scope, args_hash: data.args_hash });
        }).then(function () { setStatus('Responding…'); }, function (e) { showAlert(e.message); });
      }
      return;
    }
    if (name === 'tool.result') {
      var t = sm.tools[data.id];
      if (t) {
        t.li.className = data.ok ? 'asst-ok' : 'asst-bad';
        t.summary.textContent = ' — ' + (data.ok ? '✓ ' : '✗ ') + (data.summary || '') + (data.truncated ? ' (truncated)' : '');
      }
      return;
    }
    if (name === 'proposal') {
      DF.renderProposal(sm.box, data.proposal_id, { canApply: canApply() });
      return;
    }
    if (name === 'usage') return;
    if (name === 'error') {
      S.lastError = data;
      var msg = data.message || 'The request failed.';
      if (data.kind === 'auth' || data.kind === 'config' || data.kind === 'permission') msg += ' Open Settings → AI Assistant.';
      showAlert(msg);
      return;
    }
    if (name === 'run.end') flush(sm);
  }

  function startStream(body, userText) {
    showAlert('');
    S.lastError = null;
    var sm = beginStreamingView(userText, null);
    var loadTimer = setTimeout(function () {
      if (!sm.gotToken) sm.hint.textContent = (provider() && provider().locality === 'local')
        ? 'Loading model… (local models can take a while to start)' : 'Still waiting for the first token…';
    }, LOADING_HINT_MS);
    S.stream = ST.startRun(BASE + '/conversations/' + encodeURIComponent(S.convId) + '/runs', body, {
      onEvent: function (name, data) { onEvent(sm, name, data); },
      onError: function (err) {
        S.lastError = err.body && err.body.provider_error ? err.body.provider_error : { kind: 'unknown', message: err.message };
        if (err.status === 409 && err.body && /gitignored/.test(err.message || '')) {
          handleIgnoredConfirmation(err.message, body, userText);
          return;
        }
        showAlert(err.message || 'The request failed.');
      },
      onDone: function (info) {
        clearTimeout(loadTimer);
        flush(sm);
        S.stream = null;
        announce(info && info.error ? 'Response failed' : 'Response complete');
        refreshUi();
        if (S.convId) {
          openConversation(S.convId).then(loadConversations);
        }
      },
    });
    refreshUi();
  }

  function handleIgnoredConfirmation(message, body, userText) {
    DF.dialog({ title: 'Include a gitignored file?', body: el('p', null, message),
                actions: [{ label: 'Cancel', value: false, cancel: true }, { label: 'Include anyway', value: true }] })
      .then(function (ok) {
        if (!ok) return;
        S.tray.forEach(function (t) { if (t.kind === 'file') t.include_ignored = true; });
        renderTray();
      });
  }

  function ensureConversation() {
    if (S.convId) return Promise.resolve(S.convId);
    return api('POST', '/conversations', {}).then(function (c) {
      S.convId = c.id;
      S.conv = { id: c.id, title: c.title, messages: [] };
      return c.id;
    });
  }

  function send() {
    if (S.stream) return;
    var text = (E.input.value || '').trim();
    if (!text) return;
    var p = provider();
    if (!p) { openSettings(); return; }
    if (!S.model) { toggleModelMenu(); return; }
    var needConfirm = p.locality === 'cloud' && S.prefs && S.prefs.confirm_first_cloud_send !== false &&
                      !S.cloudConfirmed[S.convId || '_new'];
    var gate = needConfirm ? showPreview(true) : Promise.resolve(true);
    gate.then(function (ok) {
      if (!ok) return;
      return ensureConversation().then(function (cid) {
        S.cloudConfirmed[cid] = true;
        if (S.cloudConfirmed._new) delete S.cloudConfirmed._new;
        var body = { action: 'send', message: text, context: contextSpecs(), provider_instance: p.id,
                     model: S.model, agent: !!S.agent };
        if (S.activeLeaf) body.parent_id = S.activeLeaf;
        E.input.value = '';
        E.secretWarn.hidden = true;
        startStream(body, text);
      });
    }).catch(function (e) { showAlert(e.message); });
  }

  function rerun(action, userMessageId) {
    if (S.stream || !S.convId || !userMessageId) return;
    var p = provider();
    if (!p || !S.model) { showAlert('Choose a provider and model first.'); return; }
    // Show the answered user message, then stream the new reply beneath it.
    S.activeLeaf = userMessageId;
    renderConversation();
    startStream({ action: action, parent_id: userMessageId, provider_instance: p.id, model: S.model,
                  agent: !!S.agent }, null);
  }

  function stopStreaming() { if (S.stream) S.stream.stop(); }

  // ── Composer ──────────────────────────────────────────────────────────────
  function onComposerKey(e) {
    if (e.isComposing) return;
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); return; }
    if (e.key === 'Escape') {
      if (S.stream) { e.preventDefault(); stopStreaming(); return; }
      e.preventDefault();
      E.input.blur();
      var main = doc.getElementById('viv-content');
      if (main) { if (!main.hasAttribute('tabindex')) main.setAttribute('tabindex', '-1'); main.focus(); }
    }
  }

  function onComposerInput() {
    var v = E.input.value || '';
    var hit = SECRET_SHAPES.some(function (re) { return re.test(v); });
    var p = provider();
    E.secretWarn.hidden = !hit;
    E.secretWarn.textContent = hit ? 'This looks like an API key or other secret — it will be sent to ' +
      (p ? p.display_name : 'the provider') + ' as typed. Remove it unless you mean to share it.' : '';
    updateEgress();
  }

  // ── Settings / shortcut / suggest ─────────────────────────────────────────
  function openSettings() {
    if (root.vivSettings && root.vivSettings.open) root.vivSettings.open('assistant');
    else root.location.hash = '#settings';
  }

  function shortcutMatches(e) {
    var spec = (S.prefs && S.prefs.panel_shortcut) || 'Mod+Shift+.';
    var parts = spec.split('+');
    var key = parts.pop();
    var want = { mod: false, shift: false, alt: false };
    parts.forEach(function (p) { want[p.toLowerCase() === 'ctrl' || p.toLowerCase() === 'cmd' ? 'mod' : p.toLowerCase()] = true; });
    var mod = e.ctrlKey || e.metaKey;
    var k = e.key === '>' && key === '.' ? '.' : e.key;     // Shift+. reports ">" on US layouts
    return mod === !!want.mod && e.shiftKey === !!want.shift && e.altKey === !!want.alt &&
           (k || '').toLowerCase() === key.toLowerCase();
  }

  function onGlobalKey(e) {
    if (e.isComposing || !shortcutMatches(e)) return;
    e.preventDefault();
    if (!root.vivSidepanel) return;
    var wasOpen = root.vivSidepanel.isOpen();
    root.vivSidepanel.toggle('assistant');
    if (!wasOpen) setTimeout(function () { E.input.focus(); }, 0);
  }

  function registerSuggest() {
    root.vivSuggest = root.vivSuggest || { providers: [], register: function (p) { this.providers.push(p); } };
    if (root.vivSuggest.providers.some(function (p) { return p.id === 'assistant'; })) return;
    root.vivSuggest.register({
      id: 'assistant', label: 'AI Assistant',
      available: function () { return !!(provider() && S.model); },
      suggest: function (kind) {
        var p = provider();
        return api('POST', '/suggest', { kind: kind, provider_instance: p.id, model: S.model });
      },
    });
  }

  // ── Config loading ────────────────────────────────────────────────────────
  function reloadConfig() {
    return Promise.all([api('GET', '/status'), api('GET', '/providers'), api('GET', '/preferences')]).then(function (r) {
      S.status = r[0];
      S.providers = r[1].providers;
      S.prefs = r[2];
      var saved = readSel();
      var enabled = S.providers.filter(function (p) { return p.enabled; });
      var inst = saved && enabled.some(function (p) { return p.id === saved.instance; }) ? saved.instance
               : (S.prefs.default_instance && enabled.some(function (p) { return p.id === S.prefs.default_instance; })
                 ? S.prefs.default_instance : (enabled[0] && enabled[0].id));
      S.instance = inst || null;
      var p = provider();
      S.model = saved && saved.instance === S.instance && saved.model ? saved.model : (p && p.default_model) || null;
      return Promise.all(enabled.map(function (pp) {
        return api('GET', '/providers/' + pp.id + '/models').then(function (m) { S.models[pp.id] = m; }, function () {});
      }));
    }).then(function () {
      if (!S.model) {
        var list = (S.models[S.instance] || {}).models || [];
        if (list.length === 1) S.model = list[0].id;
      }
      refreshUi();
      updateEgress();
      if (!S.conv) renderConversation();
    }, function (e) {
      showAlert('The assistant could not load its settings: ' + e.message);
    });
  }

  function init() {
    var sp = root.vivSidepanel;
    var host = sp && sp.body && sp.body();
    if (!host || !MD || !ST || !DF) return;
    if (sp.setLabel) sp.setLabel('Assistant');
    build(host);
    renderTray();
    reloadConfig().then(loadConversations);
    registerSuggest();
    doc.addEventListener('keydown', onGlobalKey);
    if (sp.onChange) sp.onChange(function (d) {
      if (d.open && d.id === 'assistant') { updateEgress(); setTimeout(function () { E.input.focus(); }, 0); }
    });
    root.addEventListener('hashchange', function () { updateEgress(); });
  }

  root.vivAssistant = { reloadConfig: reloadConfig, open: function () { if (root.vivSidepanel) root.vivSidepanel.open('assistant'); } };

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
  else init();
})(typeof window !== 'undefined' ? window : globalThis);
