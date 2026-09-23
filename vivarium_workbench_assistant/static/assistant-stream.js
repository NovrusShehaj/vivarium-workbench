// assistant-stream.js — streamed runs over fetch() + ReadableStream.
//
// Not EventSource: a run is a POST with a JSON body, and EventSource can only
// GET and cannot carry the X-VW-Session header that static/session.js adds to
// every same-origin fetch. The response body is text/event-stream; this module
// parses it incrementally:
//   * frames split across network chunks, \n and \r\n line endings;
//   * comment lines (": ka" keep-alives) are ignored;
//   * multi-line "data:" fields are joined with "\n";
//   * a JSON "data" payload is parsed; a malformed one is reported, not thrown.
// Stop = POST /runs/<id>/cancel (server-side, idempotent) followed by
// AbortController.abort() once the server acknowledged (or after 2 s).
//
// window.vivAssistantStream = { SSEParser, startRun }
// Also require()-able under Node for tests/js/test_assistant_stream.js.
(function (root) {
  'use strict';

  var MAX_BUFFER = 4 * 1024 * 1024;

  function SSEParser(onEvent) {
    this.onEvent = onEvent;
    this.buffer = '';
    this.event = '';
    this.data = [];
  }

  SSEParser.prototype.push = function (chunk) {
    this.buffer += chunk;
    if (this.buffer.length > MAX_BUFFER) {
      this.buffer = '';
      throw new Error('stream event too large');
    }
    var idx;
    // Process complete lines only; keep the partial tail in the buffer.
    while ((idx = this.buffer.search(/\r\n|\n|\r/)) !== -1) {
      var line = this.buffer.slice(0, idx);
      var sepLen = (this.buffer.charAt(idx) === '\r' && this.buffer.charAt(idx + 1) === '\n') ? 2 : 1;
      // A lone "\r" at the very end may be the first half of "\r\n": wait.
      if (this.buffer.charAt(idx) === '\r' && idx + 1 === this.buffer.length) break;
      this.buffer = this.buffer.slice(idx + sepLen);
      this._line(line);
    }
  };

  SSEParser.prototype.end = function () {
    if (this.buffer) {
      var rest = this.buffer;
      this.buffer = '';
      this._line(rest);
    }
    this._dispatch();
  };

  SSEParser.prototype._line = function (line) {
    if (line === '') { this._dispatch(); return; }
    if (line.charAt(0) === ':') return;             // comment / keep-alive
    var colon = line.indexOf(':');
    var field = colon === -1 ? line : line.slice(0, colon);
    var value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.charAt(0) === ' ') value = value.slice(1);
    if (field === 'event') this.event = value;
    else if (field === 'data') this.data.push(value);
    // "id" and "retry" are not used by this protocol.
  };

  SSEParser.prototype._dispatch = function () {
    if (!this.data.length) { this.event = ''; return; }
    var raw = this.data.join('\n');
    var name = this.event || 'message';
    this.event = '';
    this.data = [];
    var payload;
    try { payload = JSON.parse(raw); } catch (e) { payload = { _malformed: true, raw: raw.slice(0, 200) }; }
    this.onEvent(name, payload);
  };

  // POST a run and stream its events.
  //   url      : '/api/ext/assistant/conversations/<cid>/runs'
  //   body     : RunRequest JSON
  //   handlers : { onEvent(name, data), onError(err), onDone(info) }
  // Returns { stop(), abort(), runId() }.
  function startRun(url, body, handlers) {
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var runId = null;
    var finished = false;
    var stopping = false;

    function finish(info) {
      if (finished) return;
      finished = true;
      if (handlers.onDone) handlers.onDone(info || {});
    }

    var parser = new SSEParser(function (name, data) {
      if (name === 'run.start' && data && data.run_id) runId = data.run_id;
      if (handlers.onEvent) handlers.onEvent(name, data);
      if (name === 'run.end') finish({ endEvent: data });
    });

    var req = root.fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body: JSON.stringify(body),
      signal: controller ? controller.signal : undefined,
    });

    req.then(function (resp) {
      var ctype = (resp.headers && resp.headers.get && resp.headers.get('content-type')) || '';
      if (!resp.ok || ctype.indexOf('text/event-stream') === -1) {
        return resp.json().then(function (j) {
          var err = new Error((j && j.error) || ('HTTP ' + resp.status));
          err.status = resp.status;
          err.body = j;
          throw err;
        }, function () {
          var err = new Error('HTTP ' + resp.status);
          err.status = resp.status;
          throw err;
        });
      }
      if (!resp.body || typeof resp.body.getReader !== 'function') {
        return resp.text().then(function (t) { parser.push(t); parser.end(); finish({}); });
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder('utf-8');
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) {
            parser.push(decoder.decode());
            parser.end();
            finish({ interrupted: !finished });
            return;
          }
          parser.push(decoder.decode(r.value, { stream: true }));
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      if (err && err.name === 'AbortError') {
        finish({ aborted: true, stopped: stopping });
        return;
      }
      if (handlers.onError) handlers.onError(err);
      finish({ error: err });
    });

    function cancelOnServer() {
      if (!runId) return Promise.resolve(null);
      return root.fetch('/api/ext/assistant/runs/' + encodeURIComponent(runId) + '/cancel', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: '{}',
      }).then(function (r) { return r.ok ? r.json() : null; }, function () { return null; });
    }

    return {
      runId: function () { return runId; },
      // Ask the server to cancel (it persists the partial reply as "cancelled"
      // and ends the stream), then drop the connection.
      stop: function () {
        if (finished || stopping) return;
        stopping = true;
        var aborted = false;
        var hardStop = setTimeout(function () {
          if (!finished && controller && !aborted) { aborted = true; controller.abort(); }
        }, 2000);
        cancelOnServer().then(function () {
          // The server now sends run.end; give it a moment before aborting.
          setTimeout(function () {
            clearTimeout(hardStop);
            if (!finished && controller && !aborted) { aborted = true; controller.abort(); }
          }, 400);
        });
      },
      abort: function () { if (controller) controller.abort(); },
    };
  }

  var api = { SSEParser: SSEParser, startRun: startRun };
  root.vivAssistantStream = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
