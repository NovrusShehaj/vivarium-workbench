// assistant-settings.js — Settings → AI Assistant (mounted via vivSettings).
//
// Provider instances as cards: type, Local/Cloud badge, "Where is my key
// stored?", a write-only credential form, Test connection, Discover models,
// default model, manual models + capability overrides, enable/disable, remove.
// Plus preferences (automatic context, first-cloud-send confirmation, history,
// retention, tool permissions, extra instructions) and the data locations.
//
// Credentials: typed into a password field, POSTed once, and the field is
// cleared immediately — this page keeps no copy, and the server never sends a
// key back (only {configured, source, hint}).
//
// window.vivAssistantSettings = { refresh }
(function (root) {
  'use strict';

  var doc = root.document;
  var BASE = '/api/ext/assistant';
  var state = { status: null, types: [], presets: [], providers: [], prefs: null, models: {}, body: null };

  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function btn(label, onClick, cls) {
    var b = el('button', cls || 'asst-btn', label);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }
  function field(labelText, input, help) {
    var wrap = el('label', 'asst-field');
    wrap.appendChild(el('span', 'asst-field-label', labelText));
    wrap.appendChild(input);
    if (help) wrap.appendChild(el('span', 'asst-field-help', help));
    return wrap;
  }
  function input(type, value, attrs) {
    var i = el('input');
    i.type = type;
    if (value !== undefined && value !== null) i.value = value;
    Object.keys(attrs || {}).forEach(function (k) { i.setAttribute(k, attrs[k]); });
    return i;
  }
  function select(options, value) {
    var s = el('select');
    options.forEach(function (o) {
      var opt = el('option', null, o[1]);
      opt.value = o[0];
      if (o[0] === value) opt.selected = true;
      s.appendChild(opt);
    });
    return s;
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
  function flash(host, text, bad) {
    var p = host.querySelector('.asst-flash') || el('p', 'asst-flash');
    p.setAttribute('role', bad ? 'alert' : 'status');
    p.className = 'asst-flash' + (bad ? ' asst-bad' : '');
    p.textContent = text;
    host.appendChild(p);
  }

  var SOURCE_LABEL = {
    keyring: 'OS keychain', env: 'Environment variable', session: 'This server session only',
    adc: 'Google Application Default Credentials', key_file: 'Service-account key file', none: 'No key needed',
  };

  // ── Rendering ─────────────────────────────────────────────────────────────
  function render() {
    var body = state.body;
    if (!body) return;
    while (body.firstChild) body.removeChild(body.firstChild);
    var st = state.status;
    if (!st) { body.appendChild(el('p', 'asst-muted', 'Loading…')); return; }
    if (!st.available) {
      body.appendChild(el('p', 'asst-muted', st.reason || 'The assistant is not available on this deployment.'));
      return;
    }
    var caps = st.capabilities || {};
    var intro = el('p', 'asst-muted');
    intro.textContent = caps.mode === 'local'
      ? 'Bring your own key. Requests go from this workbench server to the provider — never from the browser. ' +
        'Keys are stored in your OS keychain (or only for this server session) and are never shown again.'
      : 'This shared deployment uses provider settings managed by its operator.';
    body.appendChild(intro);
    if (st.config_read_only) body.appendChild(el('p', 'asst-warning', st.config_read_only));

    body.appendChild(el('h3', 'asst-subhead', 'Providers'));
    if (!state.providers.length) body.appendChild(el('p', 'asst-muted', 'No providers yet.'));
    state.providers.forEach(function (p) { body.appendChild(providerCard(p, caps)); });
    if (caps.config_editable) body.appendChild(addProviderForm());

    body.appendChild(el('h3', 'asst-subhead', 'Preferences'));
    body.appendChild(prefsForm(caps));

    if (st.storage && st.storage.data) {
      body.appendChild(el('h3', 'asst-subhead', 'Where things are stored'));
      var dl = el('dl', 'asst-storage');
      dl.appendChild(el('dt', null, 'Settings (no keys)'));
      dl.appendChild(el('dd', null, st.storage.config));
      dl.appendChild(el('dt', null, 'Conversations, proposals, audit log'));
      dl.appendChild(el('dd', null, st.storage.data));
      dl.appendChild(el('dt', null, 'API keys'));
      dl.appendChild(el('dd', null, st.keyring_available ? 'OS keychain (service "vivarium-workbench-assistant")'
                                                          : 'No OS keychain found: keys are kept for this server session only'));
      body.appendChild(dl);
    }
  }

  function providerCard(p, caps) {
    var card = el('div', 'asst-provider-card');
    card.setAttribute('data-provider', p.id);
    var head = el('div', 'asst-provider-head');
    head.appendChild(el('strong', null, p.display_name));
    head.appendChild(el('span', 'asst-badge asst-badge-' + p.locality, p.locality === 'local' ? 'Local' : 'Cloud'));
    head.appendChild(el('span', 'asst-muted', p.profile.display_name));
    if (!p.enabled) head.appendChild(el('span', 'asst-badge', 'disabled'));
    if (state.prefs && state.prefs.default_instance === p.id) head.appendChild(el('span', 'asst-badge', 'default'));
    card.appendChild(head);
    if (p.base_url) card.appendChild(el('p', 'asst-muted', p.base_url));
    if (p.profile.notes) card.appendChild(el('p', 'asst-muted', p.profile.notes));

    var cs = p.credential_status || {};
    var where = el('p', 'asst-where');
    where.appendChild(el('strong', null, 'Where is my key stored? '));
    var desc = SOURCE_LABEL[cs.source] || cs.source;
    if (cs.source === 'env' && p.credential && p.credential.env_var) desc += ' ' + p.credential.env_var;
    where.appendChild(doc.createTextNode(desc + (cs.configured ? ' — configured' : ' — not configured') +
      (cs.hint ? ' (' + cs.hint + ')' : '') + (cs.notice && cs.source === 'session' ? '. ' + cs.notice : '')));
    card.appendChild(where);

    var local = caps.mode === 'local';
    var canSetKey = local ? ['keyring', 'session', 'env', 'none'].indexOf(p.credential.source) !== -1 ||
                            p.type === 'vertex'
                          : (caps.user_credentials && p.credential.source === 'session');
    if (canSetKey && p.type !== 'vertex') card.appendChild(credentialForm(p, 'api_key', 'API key', local));
    if (canSetKey && p.type === 'vertex' && (p.credential.source === 'session' || local)) {
      card.appendChild(credentialForm(p, 'access_token', 'Access token (this session only)', local, true));
    }

    var actions = el('div', 'asst-actions');
    var out = el('div', 'asst-provider-out');
    actions.appendChild(btn('Test connection', function () {
      out.textContent = 'Testing…';
      api('POST', '/providers/' + p.id + '/test', {}).then(function (r) {
        out.textContent = r.ok ? ('✓ Connected' + (r.models_count !== undefined ? ' — ' + r.models_count + ' models' : '') +
                                  (r.latency_ms !== undefined ? ' (' + r.latency_ms + ' ms)' : '') +
                                  (r.notes && r.notes.length ? '. ' + r.notes.join(' ') : ''))
                             : '✗ ' + ((r.error && r.error.message) || 'Failed');
        out.className = 'asst-provider-out ' + (r.ok ? 'asst-ok' : 'asst-bad');
        loadModels(p.id, true).then(function () { renderModels(p, modelsBox, caps); });
      }, function (e) { out.textContent = '✗ ' + e.message; out.className = 'asst-provider-out asst-bad'; });
    }));
    actions.appendChild(btn('Discover models', function () {
      out.textContent = 'Discovering…';
      loadModels(p.id, true).then(function (r) {
        out.textContent = r.error ? '✗ ' + r.error.message : r.models.length + ' models';
        renderModels(p, modelsBox, caps);
      });
    }));
    if (caps.config_editable) {
      actions.appendChild(btn(p.enabled ? 'Disable' : 'Enable', function () {
        api('PATCH', '/providers/' + p.id, { enabled: !p.enabled }).then(refresh, function (e) { flash(card, e.message, true); });
      }));
      if (!state.prefs || state.prefs.default_instance !== p.id) {
        actions.appendChild(btn('Make default', function () {
          api('PATCH', '/providers/' + p.id, { make_default: true }).then(refresh, function (e) { flash(card, e.message, true); });
        }));
      }
      actions.appendChild(btn('Remove', function () {
        var D = root.vivAssistantDiff;
        var go = D ? D.dialog({ title: 'Remove ' + p.display_name + '?',
                                body: el('p', null, 'This removes the provider and deletes its stored key from the keychain.'),
                                actions: [{ label: 'Cancel', value: false, cancel: true },
                                          { label: 'Remove', value: true, danger: true }] })
                   : Promise.resolve(root.confirm('Remove ' + p.display_name + '?'));
        go.then(function (ok) {
          if (ok) api('DELETE', '/providers/' + p.id).then(refresh, function (e) { flash(card, e.message, true); });
        });
      }, 'asst-btn asst-btn-danger'));
    }
    card.appendChild(actions);
    card.appendChild(out);
    var modelsBox = el('div', 'asst-models');
    card.appendChild(modelsBox);
    renderModels(p, modelsBox, caps);
    if (p.profile.data_usage_url) {
      var link = el('a', 'asst-link', 'How ' + p.profile.display_name + ' handles API data ↗');
      link.href = p.profile.data_usage_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      var lp = el('p', 'asst-muted');
      lp.appendChild(link);
      card.appendChild(lp);
    }
    return card;
  }

  function credentialForm(p, fieldName, label, local, tokenOnly) {
    var form = el('form', 'asst-cred-form');
    form.setAttribute('autocomplete', 'off');
    var pw = input('password', '', { autocomplete: 'new-password', spellcheck: 'false', 'aria-label': label + ' for ' + p.display_name });
    form.appendChild(field(label, pw, 'Stored server-side; never shown again.'));
    var sessionOnly = input('checkbox', null);
    if (local && !tokenOnly) {
      var l = el('label', 'asst-check');
      l.appendChild(sessionOnly);
      l.appendChild(doc.createTextNode(' Keep for this server session only (do not save to the keychain)'));
      form.appendChild(l);
    }
    var save = el('button', 'asst-btn asst-btn-primary', 'Save key');
    save.type = 'submit';
    form.appendChild(save);
    var status = el('span', 'asst-muted');
    form.appendChild(status);
    if (p.credential_status && p.credential_status.configured && p.credential.source !== 'none') {
      form.appendChild(btn('Remove key', function () {
        api('DELETE', '/providers/' + p.id + '/credential').then(refresh, function (e) { status.textContent = e.message; });
      }));
    }
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var value = pw.value;
      pw.value = '';                         // clear immediately; no copy is kept
      if (!value) { status.textContent = 'Enter a key first.'; return; }
      var body = { session_only: !!sessionOnly.checked };
      body[fieldName] = value;
      value = null;
      status.textContent = 'Saving…';
      api('POST', '/providers/' + p.id + '/credential', body).then(function (r) {
        body = null;
        status.textContent = r.notice || 'Saved.';
        refresh();
      }, function (err) { body = null; status.textContent = err.message; });
    });
    return form;
  }

  function loadModels(iid, refreshFlag) {
    return api('GET', '/providers/' + iid + '/models' + (refreshFlag ? '?refresh=1' : '')).then(function (r) {
      state.models[iid] = r;
      return r;
    }, function (e) { state.models[iid] = { models: [], error: { message: e.message } }; return state.models[iid]; });
  }

  function renderModels(p, box, caps) {
    while (box.firstChild) box.removeChild(box.firstChild);
    var info = state.models[p.id];
    var models = (info && info.models) || [];
    var row = el('div', 'asst-row');
    var opts = [['', models.length ? 'Choose a default model…' : 'No models yet']].concat(models.map(function (m) {
      return [m.id, (m.display_name || m.id) + (m.context_window ? ' (' + Math.round(m.context_window / 1000) + 'k)' : '')];
    }));
    var sel = select(opts, p.default_model || '');
    sel.setAttribute('aria-label', 'Default model for ' + p.display_name);
    sel.disabled = !caps.config_editable;
    sel.addEventListener('change', function () {
      api('PATCH', '/providers/' + p.id, { default_model: sel.value || null }).then(refresh, function (e) { flash(box, e.message, true); });
    });
    row.appendChild(field('Default model', sel));
    box.appendChild(row);
    if (caps.config_editable) {
      var add = input('text', '', { placeholder: 'model id', spellcheck: 'false' });
      var addRow = el('div', 'asst-row');
      addRow.appendChild(field('Add a model id manually', add,
        p.type === 'vertex' ? 'Vertex offers no model list here: add the ids your project can use.' : null));
      addRow.appendChild(btn('Add', function () {
        var v = add.value.trim();
        if (!v) return;
        var list = (p.manual_models || []).concat([v]);
        api('PATCH', '/providers/' + p.id, { manual_models: list }).then(function () {
          add.value = '';
          return loadModels(p.id, false);
        }).then(refresh, function (e) { flash(box, e.message, true); });
      }));
      box.appendChild(addRow);
      if (models.length) {
        var details = el('details', 'asst-caps');
        details.appendChild(el('summary', null, 'Model capabilities (for agent mode)'));
        details.appendChild(el('p', 'asst-muted', 'Agent mode needs a model that can call tools. Mark local models ' +
                                                   'you know support tool calling.'));
        models.slice(0, 200).forEach(function (m) {
          var l = el('label', 'asst-check');
          var cb = input('checkbox', null);
          cb.checked = !!(m.caps && m.caps.indexOf('tools') !== -1);
          cb.addEventListener('change', function () {
            api('POST', '/providers/' + p.id + '/models/capabilities', { model: m.id, tools: cb.checked })
              .then(function () { return loadModels(p.id, false); }, function (e) { flash(box, e.message, true); });
          });
          l.appendChild(cb);
          l.appendChild(doc.createTextNode(' ' + (m.display_name || m.id) + (m.caps === null ? ' (capabilities unknown)' : '')));
          details.appendChild(l);
        });
        box.appendChild(details);
      }
    }
    if (info && info.error) box.appendChild(el('p', 'asst-bad', info.error.message));
  }

  function addProviderForm() {
    var box = el('details', 'asst-add-provider');
    box.appendChild(el('summary', null, 'Add a provider'));
    var form = el('form', 'asst-form');
    var typeSel = select(state.types.map(function (t) { return [t.type, t.display_name]; }), state.types.length ? state.types[0].type : '');
    form.appendChild(field('Type', typeSel));
    var dyn = el('div');
    form.appendChild(dyn);
    var controls = {};
    function build() {
      while (dyn.firstChild) dyn.removeChild(dyn.firstChild);
      controls = {};
      var t = state.types.filter(function (x) { return x.type === typeSel.value; })[0];
      if (!t) return;
      controls.name = input('text', '', { placeholder: t.display_name, maxlength: '80' });
      dyn.appendChild(field('Display name (optional)', controls.name));
      if (t.type === 'openai_compatible') {
        controls.preset = select(state.presets.map(function (p) { return [p.id, p.display_name]; }), 'ollama');
        dyn.appendChild(field('Preset', controls.preset));
        controls.baseUrl = input('url', 'http://127.0.0.1:11434/v1', { spellcheck: 'false' });
        controls.preset.addEventListener('change', function () {
          var pr = state.presets.filter(function (x) { return x.id === controls.preset.value; })[0];
          controls.baseUrl.value = (pr && pr.base_url) || '';
        });
        dyn.appendChild(field('Base URL', controls.baseUrl, 'The workbench server calls this URL, so browser CORS settings do not matter.'));
      }
      if (t.type === 'vertex') {
        controls.project = input('text', '', { placeholder: 'my-gcp-project', spellcheck: 'false' });
        controls.location = input('text', 'global', { spellcheck: 'false' });
        dyn.appendChild(field('GCP project id', controls.project));
        dyn.appendChild(field('Location', controls.location, '"global" or a region such as us-central1'));
      }
      controls.source = select(t.credential_sources.map(function (s) { return [s, SOURCE_LABEL[s] || s]; }), t.credential_sources[0]);
      dyn.appendChild(field('Credential', controls.source));
      controls.env = input('text', t.default_env_var || '', { spellcheck: 'false', placeholder: 'ENV_VAR_NAME' });
      controls.keyFile = input('text', '', { spellcheck: 'false', placeholder: '/path/to/service-account.json' });
      var envField = field('Environment variable name', controls.env, 'Only the name is saved; the value is read when needed.');
      var fileField = field('Key file path', controls.keyFile, 'Only the path is saved. Local mode only.');
      dyn.appendChild(envField);
      dyn.appendChild(fileField);
      function sync() {
        envField.hidden = controls.source.value !== 'env';
        fileField.hidden = controls.source.value !== 'key_file';
      }
      controls.source.addEventListener('change', sync);
      sync();
      if (t.data_usage_url) {
        var a = el('a', 'asst-link', 'How ' + t.display_name + ' handles API data ↗');
        a.href = t.data_usage_url; a.target = '_blank'; a.rel = 'noopener noreferrer';
        dyn.appendChild(a);
      }
    }
    typeSel.addEventListener('change', build);
    build();
    var submit = el('button', 'asst-btn asst-btn-primary', 'Add provider');
    submit.type = 'submit';
    form.appendChild(submit);
    var status = el('span', 'asst-muted');
    form.appendChild(status);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var body = { type: typeSel.value };
      if (controls.name && controls.name.value.trim()) body.display_name = controls.name.value.trim();
      if (controls.preset) body.preset = controls.preset.value;
      if (controls.baseUrl && controls.baseUrl.value.trim()) body.base_url = controls.baseUrl.value.trim();
      if (controls.project) body.project = controls.project.value.trim();
      if (controls.location) body.location = controls.location.value.trim();
      var cred = { source: controls.source.value };
      if (cred.source === 'env') cred.env_var = controls.env.value.trim();
      if (cred.source === 'key_file') cred.key_file_path = controls.keyFile.value.trim();
      body.credential = cred;
      status.textContent = 'Adding…';
      api('POST', '/providers', body).then(function () { status.textContent = ''; box.open = false; refresh(); },
                                           function (err) { status.textContent = err.message; });
    });
    box.appendChild(form);
    return box;
  }

  function prefsForm(caps) {
    var pr = state.prefs || {};
    var form = el('form', 'asst-form');
    var def = select([['', '(none)']].concat(state.providers.map(function (p) { return [p.id, p.display_name]; })),
                     pr.default_instance || '');
    form.appendChild(field('Default provider', def));
    var auto = select([['off', 'Off'], ['page_summary', 'Page summary (ids only) — default'],
                       ['page_and_selection', 'Page + the open study (its study.yaml and status)']], pr.auto_context);
    form.appendChild(field('Automatic context', auto, 'What is attached to every message without asking.'));
    var confirmCloud = input('checkbox', null);
    confirmCloud.checked = pr.confirm_first_cloud_send !== false;
    var cl = el('label', 'asst-check');
    cl.appendChild(confirmCloud);
    cl.appendChild(doc.createTextNode(' Preview what will be sent before the first cloud request in each conversation'));
    form.appendChild(cl);
    var persist = input('checkbox', null);
    persist.checked = pr.persist_conversations !== false;
    persist.disabled = caps.mode !== 'local' && !caps.persist_conversations;
    var pl = el('label', 'asst-check');
    pl.appendChild(persist);
    pl.appendChild(doc.createTextNode(' Save conversation history on this computer'));
    form.appendChild(pl);
    var retention = input('number', pr.retention_days || '', { min: '0', max: '3650', placeholder: 'keep forever' });
    form.appendChild(field('Delete conversations older than (days)', retention, 'Leave empty or 0 to keep them.'));
    var readSel = select([['auto', 'Automatic'], ['ask', 'Ask each time'], ['disabled', 'Disabled']], (pr.tools || {}).read || 'auto');
    form.appendChild(field('Read tools (agent mode)', readSel, 'Reading workspace files; sensitive files are never readable.'));
    var execSel = select([['ask', 'Ask each time'], ['disabled', 'Disabled']], (pr.tools || {}).execute || 'ask');
    execSel.disabled = !(caps.tools && caps.tools.execute);
    form.appendChild(field('Run lint / tests / smoke runs', execSel));
    var shellSel = select([['disabled', 'Disabled'], ['ask', 'Enabled — ask for every command']], (pr.tools || {}).shell || 'disabled');
    shellSel.disabled = !(caps.tools && caps.tools.shell);
    form.appendChild(field('Shell commands', shellSel, 'Off by default. Every command needs your approval; never on shared deployments.'));
    var extra = el('textarea');
    extra.value = pr.system_prompt_extra || '';
    extra.rows = 3;
    extra.maxLength = 4000;
    form.appendChild(field('Extra instructions for the assistant', extra, 'Added after the built-in safety instructions.'));
    var save = el('button', 'asst-btn asst-btn-primary', 'Save preferences');
    save.type = 'submit';
    form.appendChild(save);
    var status = el('span', 'asst-muted');
    form.appendChild(status);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var body = {
        auto_context: auto.value, confirm_first_cloud_send: confirmCloud.checked,
        persist_conversations: persist.checked, retention_days: parseInt(retention.value || '0', 10) || 0,
        system_prompt_extra: extra.value, tools: { read: readSel.value, execute: execSel.value, shell: shellSel.value },
      };
      if (def.value) body.default_instance = def.value;
      status.textContent = 'Saving…';
      api('PATCH', '/preferences', body).then(function (p) {
        state.prefs = p;
        status.textContent = 'Saved.';
        if (root.vivAssistant && root.vivAssistant.reloadConfig) root.vivAssistant.reloadConfig();
      }, function (err) { status.textContent = err.message; });
    });
    return form;
  }

  // ── Data ──────────────────────────────────────────────────────────────────
  function refresh() {
    return api('GET', '/status').then(function (st) {
      state.status = st;
      if (!st.available) { render(); return null; }
      return Promise.all([
        api('GET', '/provider-types'), api('GET', '/providers'), api('GET', '/preferences'),
      ]).then(function (r) {
        state.types = r[0].types;
        state.presets = r[0].presets;
        state.providers = r[1].providers;
        state.prefs = r[2];
        return Promise.all(state.providers.map(function (p) {
          return state.models[p.id] ? null : loadModels(p.id, false);
        }));
      });
    }).then(render, function (e) {
      state.status = { available: false, reason: 'Could not load assistant settings: ' + e.message };
      render();
    }).then(function () {
      if (root.vivAssistant && root.vivAssistant.reloadConfig) root.vivAssistant.reloadConfig();
    });
  }

  function mount(body) {
    state.body = body;
    refresh();
  }

  if (root.vivSettings && typeof root.vivSettings.registerSection === 'function') {
    root.vivSettings.registerSection({ id: 'assistant', title: 'AI Assistant', order: 50, mount: mount,
                                       onShow: function () { refresh(); } });
  }

  root.vivAssistantSettings = { refresh: refresh };
})(typeof window !== 'undefined' ? window : globalThis);
