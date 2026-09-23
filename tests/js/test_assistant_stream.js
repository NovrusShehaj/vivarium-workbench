// tests/js/test_assistant_stream.js — run with: node tests/js/test_assistant_stream.js
//
// The assistant's SSE client: an incremental parser that must produce the same
// events however the network splits the bytes, plus the fetch/abort/cancel
// run lifecycle.
const assert = require('assert');

global.window = {};
const S = require('../../vivarium_workbench_assistant/static/assistant-stream.js');

function parseAll(chunks) {
  const out = [];
  const p = new S.SSEParser((name, data) => out.push([name, data]));
  chunks.forEach((c) => p.push(c));
  p.end();
  return out;
}

const STREAM =
  ': keep-alive\n\n' +
  'event: run.start\ndata: {"v":1,"run_id":"r_1"}\n\n' +
  'event: text.delta\r\ndata: {"text":"Hel"}\r\n\r\n' +
  'event: text.delta\ndata: {"text":"lo \\u2014 ok"}\n\n' +
  ': ka\n\n' +
  'event: notice\ndata: {"message":\ndata: "two lines"}\n\n' +
  'event: run.end\ndata: {"stop_reason":"end_turn"}\n\n';

const EXPECTED = [
  ['run.start', { v: 1, run_id: 'r_1' }],
  ['text.delta', { text: 'Hel' }],
  ['text.delta', { text: 'lo — ok' }],
  ['notice', { message: 'two lines' }],
  ['run.end', { stop_reason: 'end_turn' }],
];

// Whole, and split at every single position (network chunk boundaries).
assert.deepStrictEqual(parseAll([STREAM]), EXPECTED);
for (let i = 1; i < STREAM.length; i++) {
  assert.deepStrictEqual(parseAll([STREAM.slice(0, i), STREAM.slice(i)]), EXPECTED, 'split at ' + i);
}
// One character at a time.
assert.deepStrictEqual(parseAll(STREAM.split('')), EXPECTED);

// Malformed JSON is reported, not thrown; unterminated final frame flushes on end().
{
  const out = parseAll(['event: x\ndata: {nope\n\nevent: y\ndata: {"a":1}']);
  assert.strictEqual(out[0][0], 'x');
  assert.strictEqual(out[0][1]._malformed, true);
  assert.deepStrictEqual(out[1], ['y', { a: 1 }]);
}

// Oversized buffers are refused (a broken or hostile stream cannot grow memory without bound).
{
  const p = new S.SSEParser(() => {});
  assert.throws(() => p.push('data: ' + 'x'.repeat(5 * 1024 * 1024)), /too large/);
}

// ── startRun lifecycle with a fake fetch/ReadableStream ─────────────────────
function fakeResponse(chunks, { status = 200, ctype = 'text/event-stream', json = null, delayMs = 0 } = {}) {
  const enc = new TextEncoder();
  let i = 0;
  let aborted = false;
  const reader = {
    read() {
      if (aborted) return Promise.reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
      if (i >= chunks.length) return Promise.resolve({ done: true });
      const value = enc.encode(chunks[i++]);
      return new Promise((r) => setTimeout(() => r({ done: false, value }), delayMs));
    },
  };
  return {
    ok: status < 300, status,
    headers: { get: (k) => (k.toLowerCase() === 'content-type' ? ctype : null) },
    body: { getReader: () => reader },
    json: () => Promise.resolve(json),
    _abort() { aborted = true; },
  };
}

async function main() {
  // Happy path: events delivered in order, onDone fires once with the run.end payload.
  {
    const calls = [];
    window.fetch = (url, init) => {
      calls.push([url, init]);
      return Promise.resolve(fakeResponse([STREAM.slice(0, 50), STREAM.slice(50)]));
    };
    const events = [];
    const done = await new Promise((resolve) => {
      S.startRun('/api/ext/assistant/conversations/c_1/runs', { message: 'hi' }, {
        onEvent: (n, d) => events.push([n, d]),
        onError: (e) => resolve({ error: e }),
        onDone: resolve,
      });
    });
    assert.deepStrictEqual(events, EXPECTED);
    assert.deepStrictEqual(done.endEvent, { stop_reason: 'end_turn' });
    const [url, init] = calls[0];
    assert.strictEqual(url, '/api/ext/assistant/conversations/c_1/runs');
    assert.strictEqual(init.method, 'POST');
    assert.strictEqual(init.credentials, 'same-origin');
    assert.strictEqual(JSON.parse(init.body).message, 'hi');
  }

  // HTTP errors surface the server's {error} message and status.
  {
    window.fetch = () => Promise.resolve(fakeResponse([], { status: 409, ctype: 'application/json',
                                                             json: { error: 'A response is already streaming' } }));
    const err = await new Promise((resolve) => {
      S.startRun('/x', {}, { onEvent() {}, onError: resolve, onDone() {} });
    });
    assert.strictEqual(err.status, 409);
    assert.strictEqual(err.message, 'A response is already streaming');
  }

  // stop(): POST /runs/<id>/cancel first, then abort the connection.
  {
    const cancels = [];
    let abortSignal = null;
    let resp = null;
    window.fetch = (url, init) => {
      if (/\/cancel$/.test(url)) {
        cancels.push(url);
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ cancelled: true }) });
      }
      abortSignal = init.signal;
      resp = fakeResponse(['event: run.start\ndata: {"run_id":"r_abc"}\n\n'].concat(new Array(50).fill(': ka\n\n')),
                          { delayMs: 20 });
      abortSignal.addEventListener('abort', () => resp._abort());
      return Promise.resolve(resp);
    };
    let handle;
    const done = await new Promise((resolve) => {
      handle = S.startRun('/runs', {}, {
        onEvent: (n) => { if (n === 'run.start') setTimeout(() => handle.stop(), 10); },
        onError: resolve, onDone: resolve,
      });
    });
    assert.deepStrictEqual(cancels, ['/api/ext/assistant/runs/r_abc/cancel']);
    assert.ok(done.aborted && done.stopped, 'connection dropped after the server-side cancel');
    assert.strictEqual(abortSignal.aborted, true);
  }

  // A connection that ends without run.end is reported as interrupted.
  {
    window.fetch = () => Promise.resolve(fakeResponse(['event: run.start\ndata: {"run_id":"r_2"}\n\n',
                                                        'event: text.delta\ndata: {"text":"partial"}\n\n']));
    const done = await new Promise((resolve) => {
      S.startRun('/x', {}, { onEvent() {}, onError: resolve, onDone: resolve });
    });
    assert.strictEqual(done.interrupted, true);
  }
  console.log('test_assistant_stream.js: all assertions passed');
}

main().catch((e) => { console.error(e); process.exit(1); });
