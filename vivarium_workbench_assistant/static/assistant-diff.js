// assistant-diff.js — dialogs, the diff viewer, proposal review and approvals.
//
// * dialog(): role="dialog" + aria-modal, labelled, focus trapped inside,
//   Escape closes, focus returns to the opener.
// * renderUnifiedDiff(): a table with old/new line numbers and a visible
//   "+"/"−" gutter (never colour alone), plus visually hidden "added"/"removed"
//   text for screen readers.
// * renderProposal(): per-file diff, validation results, an "executable code"
//   warning, Apply / Reject per file, Apply all / Apply & commit / Reject all,
//   Undo and Revert commit. Deletions are always confirmed individually.
// * approve(): the tool-approval dialog — shows the exact arguments, the
//   category, dangerous-pattern warnings and (for commands) the environment
//   policy; "Allow for this conversation" only where the policy permits.
//
// All content is set with textContent; nothing from the model or a file is
// ever parsed as HTML.
//
// window.vivAssistantDiff = { dialog, renderUnifiedDiff, renderProposal, approve, parseUnifiedDiff }
(function (root) {
  'use strict';

  function doc() { return root.document; }
  function el(tag, cls, textContent) {
    var n = doc().createElement(tag);
    if (cls) n.className = cls;
    if (textContent !== undefined && textContent !== null) n.textContent = textContent;
    return n;
  }
  function button(label, cls, onClick) {
    var b = el('button', cls || 'asst-btn', label);
    b.type = 'button';
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }

  function api(method, path, body) {
    return root.fetch(path, {
      method: method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.error || ('HTTP ' + r.status)); e.status = r.status; e.body = j; throw e; }
        return j;
      });
    });
  }

  // ── Dialog ────────────────────────────────────────────────────────────────
  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
                  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  // opts: { title, body: Node, actions: [{label, value, primary, danger}], describedBy }
  // Resolves with the chosen action's value (or null on Escape/close).
  function dialog(opts) {
    return new Promise(function (resolve) {
      var opener = doc().activeElement;
      var overlay = el('div', 'asst-dialog-overlay');
      var box = el('div', 'asst-dialog');
      var titleId = 'asst-dlg-' + Math.random().toString(36).slice(2);
      box.setAttribute('role', 'dialog');
      box.setAttribute('aria-modal', 'true');
      box.setAttribute('aria-labelledby', titleId);
      var h = el('h2', 'asst-dialog-title', opts.title || 'Confirm');
      h.id = titleId;
      box.appendChild(h);
      if (opts.body) {
        var bodyWrap = el('div', 'asst-dialog-body');
        bodyWrap.appendChild(opts.body);
        box.appendChild(bodyWrap);
      }
      var foot = el('div', 'asst-dialog-actions');
      var done = false;
      function close(value) {
        if (done) return;
        done = true;
        doc().removeEventListener('keydown', onKey, true);
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        if (opener && typeof opener.focus === 'function' && doc().contains(opener)) opener.focus();
        resolve(value);
      }
      (opts.actions || [{ label: 'OK', value: true, primary: true }]).forEach(function (a) {
        var b = button(a.label, 'asst-btn' + (a.primary ? ' asst-btn-primary' : '') + (a.danger ? ' asst-btn-danger' : ''),
                       function () { close(a.value); });
        if (a.value === null || a.cancel) b.setAttribute('data-cancel', '');
        foot.appendChild(b);
      });
      box.appendChild(foot);
      overlay.appendChild(box);
      overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) close(null); });
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(null); return; }
        if (e.key !== 'Tab') return;
        var items = Array.prototype.slice.call(box.querySelectorAll(FOCUSABLE));
        if (!items.length) return;
        var first = items[0], last = items[items.length - 1];
        if (e.shiftKey && doc().activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && doc().activeElement === last) { e.preventDefault(); first.focus(); }
      }
      doc().addEventListener('keydown', onKey, true);
      doc().body.appendChild(overlay);
      var initial = box.querySelector(opts.initialFocus || '[data-cancel]') || box.querySelector(FOCUSABLE);
      if (initial) initial.focus();
    });
  }

  // ── Diff ──────────────────────────────────────────────────────────────────
  function parseUnifiedDiff(text) {
    var rows = [];
    var oldN = 0, newN = 0;
    String(text || '').split('\n').forEach(function (line, idx, all) {
      if (idx === all.length - 1 && line === '') return;
      var m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(line);
      if (m) {
        oldN = parseInt(m[1], 10);
        newN = parseInt(m[2], 10);
        rows.push({ kind: 'hunk', text: line });
        return;
      }
      if (/^(---|\+\+\+) /.test(line)) { rows.push({ kind: 'file', text: line }); return; }
      if (line.charAt(0) === '\\') { rows.push({ kind: 'note', text: line }); return; }
      var tag = line.charAt(0);
      if (tag === '+') { rows.push({ kind: 'add', newN: newN++, text: line.slice(1) }); return; }
      if (tag === '-') { rows.push({ kind: 'del', oldN: oldN++, text: line.slice(1) }); return; }
      rows.push({ kind: 'ctx', oldN: oldN++, newN: newN++, text: line.slice(1) });
    });
    return rows;
  }

  function renderUnifiedDiff(text, meta) {
    var wrap = el('div', 'asst-diff-wrap');
    var rows = parseUnifiedDiff(text);
    var adds = 0, dels = 0;
    rows.forEach(function (r) { if (r.kind === 'add') adds++; else if (r.kind === 'del') dels++; });
    if (meta && meta.path) {
      var head = el('div', 'asst-diff-filehead');
      if (meta.op) head.appendChild(el('span', 'asst-op asst-op-' + meta.op, meta.opLabel || meta.op));
      head.appendChild(el('code', 'asst-path', meta.path));
      head.appendChild(el('span', 'asst-counts', '+' + (meta.additions != null ? meta.additions : adds) +
        ' −' + (meta.deletions != null ? meta.deletions : dels)));
      wrap.appendChild(head);
    }
    var table = el('table', 'asst-diff');
    var tbody = el('tbody');
    rows.forEach(function (r) {
      var tr = el('tr', 'asst-diff-' + r.kind);
      if (r.kind === 'hunk' || r.kind === 'file' || r.kind === 'note') {
        var td = el('td', 'asst-diff-meta', r.text);
        td.colSpan = 4;
        tr.appendChild(td);
      } else {
        tr.appendChild(el('td', 'asst-diff-ln', r.oldN !== undefined ? String(r.oldN) : ''));
        tr.appendChild(el('td', 'asst-diff-ln', r.newN !== undefined ? String(r.newN) : ''));
        var sign = el('td', 'asst-diff-sign');
        sign.setAttribute('aria-hidden', 'true');
        sign.textContent = r.kind === 'add' ? '+' : r.kind === 'del' ? '−' : ' ';
        tr.appendChild(sign);
        var code = el('td', 'asst-diff-code');
        if (r.kind !== 'ctx') code.appendChild(el('span', 'viv-sr-only', r.kind === 'add' ? 'added: ' : 'removed: '));
        code.appendChild(doc().createTextNode(r.text));
        tr.appendChild(code);
      }
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  // ── Proposal card ─────────────────────────────────────────────────────────
  var OP_LABEL = { modify: 'Edit', create: 'Create', delete: 'Delete' };

  function validationList(results) {
    var ul = el('ul', 'asst-validation');
    (results || []).forEach(function (v) {
      var li = el('li', v.ok ? 'asst-ok' : 'asst-bad');
      li.appendChild(el('span', 'asst-glyph', v.ok ? '✓ ' : '✗ '));
      li.appendChild(doc().createTextNode(v.message || v.kind));
      ul.appendChild(li);
    });
    return ul;
  }

  // renderProposal(container, proposalId, {onChange, canApply})
  function renderProposal(container, proposalId, opts) {
    opts = opts || {};
    var card = el('section', 'asst-proposal');
    card.setAttribute('aria-label', 'Proposed changes');
    container.appendChild(card);

    function load() {
      return api('GET', '/api/ext/assistant/proposals/' + encodeURIComponent(proposalId)).then(draw, function (e) {
        card.textContent = 'Could not load the proposal: ' + e.message;
      });
    }

    function act(action, paths, commit, source) {
      if (source) { source.disabled = true; source.setAttribute('aria-busy', 'true'); }
      return api('POST', '/api/ext/assistant/proposals/' + encodeURIComponent(proposalId) + '/' + action,
                 action === 'revert' ? {} : { paths: paths || null, commit: !!commit })
        .then(function (res) {
          draw(res.proposal || res);
          var msgs = [];
          (res.results || []).forEach(function (r) {
            if (r.status === 'conflict' || r.status === 'error') msgs.push(r.path + ': ' + r.message);
          });
          if (res.commit) {
            if (res.commit.committed) msgs.push('Committed ' + String(res.commit.commit_sha).slice(0, 10) + '.');
            else msgs.push('Not committed: ' + res.commit.reason + '.');
            if (res.commit.skipped && res.commit.skipped.length) {
              msgs.push('Left uncommitted (had your own changes): ' + res.commit.skipped.join(', '));
            }
          }
          if (res.reverted === false || res.reason) msgs.push(res.reason || 'Revert failed.');
          if (msgs.length) note(msgs.join(' '));
          if (opts.onChange) opts.onChange(res);
        }, function (e) {
          if (source) { source.disabled = false; source.removeAttribute('aria-busy'); }
          note(e.message);
        });
    }

    var noteEl = null;
    function note(t) {
      if (!noteEl) { noteEl = el('p', 'asst-proposal-note'); noteEl.setAttribute('role', 'status'); }
      noteEl.textContent = t;
      card.appendChild(noteEl);
    }

    function confirmDelete(path) {
      return dialog({
        title: 'Delete ' + path + '?',
        body: el('p', null, 'The assistant proposed deleting this file. You can undo it from this card afterwards.'),
        actions: [{ label: 'Cancel', value: false, cancel: true }, { label: 'Delete file', value: true, danger: true }],
      });
    }

    function draw(p) {
      while (card.firstChild) card.removeChild(card.firstChild);
      var head = el('div', 'asst-proposal-head');
      head.appendChild(el('strong', null, 'Proposed changes'));
      head.appendChild(el('span', 'asst-badge', p.status.replace('_', ' ')));
      card.appendChild(head);
      var pending = p.files.filter(function (f) { return f.status === 'pending'; });
      (p.files || []).forEach(function (f) {
        var fileBox = el('div', 'asst-proposal-file');
        var fh = el('div', 'asst-proposal-file-head');
        fh.appendChild(el('span', 'asst-op asst-op-' + f.op, OP_LABEL[f.op] || f.op));
        fh.appendChild(el('code', 'asst-path', f.path));
        fh.appendChild(el('span', 'asst-counts', '+' + f.additions + ' −' + f.deletions));
        fh.appendChild(el('span', 'asst-badge asst-status-' + f.status, f.status));
        fileBox.appendChild(fh);
        if (f.executable_code) {
          var w = el('p', 'asst-warning');
          w.appendChild(el('span', 'asst-glyph', '⚠ '));
          w.appendChild(doc().createTextNode('This is code that runs when simulations or tests execute. Review it carefully.'));
          fileBox.appendChild(w);
        }
        if (f.rationale) fileBox.appendChild(el('p', 'asst-rationale', f.rationale));
        fileBox.appendChild(validationList(f.validation));
        var details = el('details', 'asst-diff-details');
        if (p.files.length === 1 || f.status === 'pending') details.open = true;
        details.appendChild(el('summary', null, 'Diff'));
        details.appendChild(renderUnifiedDiff(f.unified_diff, {
          path: f.path, op: f.op, opLabel: OP_LABEL[f.op] || f.op,
          additions: f.additions, deletions: f.deletions,
        }));
        fileBox.appendChild(details);
        if (f.message) fileBox.appendChild(el('p', 'asst-bad', f.message));
        var actions = el('div', 'asst-actions');
        if (f.status === 'pending' && opts.canApply !== false) {
          actions.appendChild(button(f.op === 'delete' ? 'Delete file' : 'Apply', 'asst-btn asst-btn-primary', function (e) {
            var go = f.op === 'delete' ? confirmDelete(f.path) : Promise.resolve(true);
            go.then(function (ok) { if (ok) act('apply', [f.path], false, e.currentTarget); });
          }));
          actions.appendChild(button('Reject', 'asst-btn', function (e) { act('reject', [f.path], false, e.currentTarget); }));
        }
        if (f.status === 'applied') {
          actions.appendChild(button('Undo changes', 'asst-btn', function (e) { act('undo', [f.path], false, e.currentTarget); }));
        }
        if (actions.childNodes.length) fileBox.appendChild(actions);
        card.appendChild(fileBox);
      });
      var all = el('div', 'asst-actions asst-proposal-actions');
      var nonDelete = pending.filter(function (f) { return f.op !== 'delete'; }).map(function (f) { return f.path; });
      if (pending.length && opts.canApply !== false) {
        if (nonDelete.length) {
          all.appendChild(button('Apply all' + (nonDelete.length < pending.length ? ' edits' : ''), 'asst-btn asst-btn-primary',
                                 function (e) { act('apply', nonDelete, false, e.currentTarget); }));
          all.appendChild(button('Apply & commit', 'asst-btn', function (e) { act('apply', nonDelete, true, e.currentTarget); }));
        }
        all.appendChild(button(pending.length > 1 ? 'Reject all' : 'Reject', 'asst-btn', function (e) {
          act('reject', pending.map(function (f) { return f.path; }), false, e.currentTarget);
        }));
      }
      if (p.can_undo) all.appendChild(button('Undo changes', 'asst-btn', function (e) { act('undo', null, false, e.currentTarget); }));
      if (p.commit_sha) {
        all.appendChild(el('span', 'asst-muted', 'commit ' + String(p.commit_sha).slice(0, 10)));
        all.appendChild(button('Revert commit', 'asst-btn', function (e) { act('revert', null, false, e.currentTarget); }));
      }
      if (all.childNodes.length) card.appendChild(all);
      if (opts.canApply === false && pending.length) {
        card.appendChild(el('p', 'asst-muted', 'Applying edits is disabled on this deployment.'));
      }
    }

    load();
    return { reload: load, element: card };
  }

  // ── Tool approval ─────────────────────────────────────────────────────────
  // info: {name, category, arguments, warnings, reason, grantable}
  // Resolves {decision: 'approve'|'deny', scope: 'once'|'conversation'}.
  function approve(info) {
    var body = el('div', 'asst-approval');
    var p = el('p');
    p.appendChild(doc().createTextNode('The assistant wants to run '));
    p.appendChild(el('code', null, info.name));
    p.appendChild(doc().createTextNode(' (' + info.category + ').'));
    body.appendChild(p);
    if (info.reason) body.appendChild(el('p', 'asst-muted', 'Why approval is needed: ' + info.reason + '.'));
    if (info.category === 'execute' || info.category === 'shell') {
      body.appendChild(el('p', 'asst-muted', 'Runs in the workspace directory with a minimal environment ' +
        '(no API keys or cloud credentials). Network access is not sandboxed.'));
    }
    (info.warnings || []).forEach(function (w) {
      var wp = el('p', 'asst-warning');
      wp.appendChild(el('span', 'asst-glyph', '⚠ '));
      wp.appendChild(doc().createTextNode('Warning: ' + w));
      body.appendChild(wp);
    });
    body.appendChild(el('p', 'asst-label', 'Exact arguments:'));
    var pre = el('pre', 'asst-args');
    var argText = JSON.stringify(info.arguments || {}, null, 2);
    pre.textContent = argText;
    body.appendChild(pre);
    body.appendChild(button('Copy arguments', 'asst-btn asst-btn-quiet', function (e) {
      var b = e.currentTarget;
      var prev = b.textContent;
      var nav = root.navigator;
      var done = function (ok) { b.textContent = ok ? 'Copied' : 'Copy failed'; setTimeout(function () { b.textContent = prev; }, 1500); };
      if (nav && nav.clipboard && root.isSecureContext !== false) nav.clipboard.writeText(argText).then(function () { done(true); }, function () { done(false); });
      else done(false);
    }));
    var actions = [{ label: 'Deny execution', value: { decision: 'deny', scope: 'once' }, danger: true, cancel: true },
                   { label: 'Approve once', value: { decision: 'approve', scope: 'once' }, primary: true }];
    if (info.grantable) {
      actions.push({ label: 'Allow for this conversation', value: { decision: 'approve', scope: 'conversation' } });
    }
    return dialog({ title: 'Allow this tool?', body: body, actions: actions }).then(function (v) {
      return v || { decision: 'deny', scope: 'once' };
    });
  }

  var apiObj = { dialog: dialog, renderUnifiedDiff: renderUnifiedDiff, renderProposal: renderProposal,
                 approve: approve, parseUnifiedDiff: parseUnifiedDiff, request: api };
  root.vivAssistantDiff = apiObj;
  if (typeof module !== 'undefined' && module.exports) module.exports = apiObj;
})(typeof window !== 'undefined' ? window : globalThis);
