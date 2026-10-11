'use strict';
// Run: node tests/test_collector.js. All browser/network/storage APIs are local fakes.
// Delivery is intentionally at least once. The archive is the authoritative
// cross-gateway/browser deduper; this tests deduping only pending queue entries.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'collector.js'), 'utf8');
const EVENT_URL = '/music/api/v1/event/report';
const STAMP = 1709712000000;
const play = (guid = 'one', stamp = STAMP) => ({eventType: 'track_play', occurredAt: stamp, payload: {trackGUID: guid}});
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(name, listener, options = {}) {
    const handlers = this.listeners.get(name) || [];
    handlers.push({listener, once: !!options.once});
    this.listeners.set(name, handlers);
  }
  emit(name) {
    const handlers = [...(this.listeners.get(name) || [])];
    this.listeners.set(name, handlers.filter(handler => !handler.once));
    for (const {listener} of handlers) listener.call(this, {type: name, target: this});
  }
}
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.textContent = ''; this.attributes = {}; }
  setAttribute(name, value) { this.attributes[name] = value; }
  append(...elements) { this.children.push(...elements); }
  querySelector(tag) { return this.children.find(child => child.tag === tag); }
}
const jsonResponse = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});

function harness({storage = new Map(), account, readyState = 'complete', offline = false, storageThrows = false} = {}) {
  const window = new Events();
  const document = Object.assign(new Events(), {readyState, body: new Element('body'), createElement: tag => new Element(tag)});
  const state = {
    reports: [], ingests: [], intervals: [], offline,
    upstreamStatus: 200, upstreamBody: {code: 0, message: 'success', data: null},
    upstreamRaw: null, upstreamError: null, ingestStatus: 200,
    ingestBody: {ok: true, accepted: 1, deduped: 0, skipped: 0}, ingestRaw: null, pendingResolver: null,
  };
  window.__journalAccount = account;
  const nativeFetch = async (input, options = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    if (new URL(url, 'http://127.0.0.1:5679/music/').pathname === '/api/ingest') {
      state.ingests.push({url, options, body: JSON.parse(options.body)});
      if (state.offline) throw new TypeError('simulated offline');
      if (state.pendingResolver) return new Promise(resolve => { state.pendingResolver.resolve = resolve; });
      return state.ingestRaw == null ? jsonResponse(state.ingestBody, state.ingestStatus)
        : new Response(state.ingestRaw, {status: state.ingestStatus});
    }
    state.reports.push({url, options});
    if (state.upstreamError) throw state.upstreamError;
    return state.upstreamRaw == null ? jsonResponse(state.upstreamBody, state.upstreamStatus)
      : new Response(state.upstreamRaw, {status: state.upstreamStatus});
  };
  class XMLHttpRequest extends Events {
    constructor() { super(); this.responseType = ''; }
    open(method, url) { this.method = method; this.url = url; }
    send(body) {
      state.reports.push({url: this.url, method: this.method, body, xhr: true});
      this.status = state.upstreamStatus;
      this.responseText = state.upstreamRaw == null ? JSON.stringify(state.upstreamBody) : state.upstreamRaw;
      this.response = state.upstreamBody;
      this.emit('load');
    }
  }
  window.fetch = nativeFetch;
  const localStorage = {
    getItem(key) { if (storageThrows) throw new Error('storage unavailable'); return storage.get(key) ?? null; },
    setItem(key, value) { if (storageThrows) throw new Error('quota'); storage.set(key, value); },
  };
  const context = vm.createContext({window, document, localStorage, XMLHttpRequest,
    location: {href: 'http://127.0.0.1:5679/music/'}, URL, Request, Response, Blob, ArrayBuffer, TextDecoder,
    setInterval(callback, delay) { state.intervals.push({callback, delay}); return state.intervals.length; },
  });
  const load = () => vm.runInContext(source, context, {filename: 'collector.js'});
  load();
  const storageKey = 'music-journal.pending.v1:' + (account || 'unassigned');
  return {window, document, state, storage, storageKey, load, XMLHttpRequest,
    queued: () => JSON.parse(storage.get(storageKey) || '[]'),
    fetchReport: (body, input = EVENT_URL) => window.fetch(input, {method: 'POST', body: typeof body === 'string' ? body : JSON.stringify(body)}),
    xhrReport(body, responseType = '', url = EVENT_URL) {
      const xhr = new XMLHttpRequest(); xhr.responseType = responseType; xhr.open('POST', url); xhr.send(JSON.stringify(body)); return xhr;
    },
    async retry() { window.emit('online'); await settle(); },
    async tick() { for (const {callback} of state.intervals) callback(); await settle(); },
  };
}


test('fetch captures only HTTP success AND numeric code 0, preserving response', async () => {
  const h = harness();
  const response = await h.fetchReport(play());
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), h.state.upstreamBody);
  await settle();
  assert.equal(h.state.ingests.length, 1);
  assert.deepEqual(h.state.ingests[0].body.events, [play()]);
  assert.equal(h.state.ingests[0].options.credentials, 'same-origin');
  assert.equal(h.state.ingests[0].options.headers['Content-Type'], 'application/json');
  assert.deepEqual(h.queued(), []);
});

test('fetch rejected codes, booleans, malformed JSON and failed HTTP never enqueue', async () => {
  for (const [status, body, raw] of [[200, {code: 1}], [200, {code: '0'}], [200, {code: false}],
    [200, {}], [401, {code: 0}], [500, {code: 0}], [200, null, 'not-json']]) {
    const h = harness({offline: true});
    h.state.upstreamStatus = status; h.state.upstreamBody = body; h.state.upstreamRaw = raw ?? null;
    await h.fetchReport(play()); await settle();
    assert.equal(h.state.ingests.length, 0, JSON.stringify({status, body, raw}));
    assert.deepEqual(h.queued(), []);
  }
});

test('fetch transport failure propagates and does not record', async () => {
  const h = harness(); h.state.upstreamError = new TypeError('NAS unreachable');
  await assert.rejects(h.fetchReport(play()), /NAS unreachable/);
  assert.equal(h.state.ingests.length, 0);
});

test('non-report endpoints never capture even if returning code 0', async () => {
  const h = harness({offline: true});
  await h.fetchReport(play(), '/music/api/v1/play-history/list');
  h.xhrReport(play(), '', '/music/api/v1/track/metadata');
  await settle(); assert.equal(h.state.ingests.length, 0); assert.deepEqual(h.queued(), []);
});

test('single event, array batch and events envelope are supported', async () => {
  for (const body of [play(), [play(), play('two')], {events: [play(), play('two')]}]) {
    const h = harness(); await h.fetchReport(body); await settle();
    const expected = Array.isArray(body) ? body : body.events || [body];
    assert.deepEqual(h.state.ingests[0].body.events, expected);
  }
});

test('invalid event types, missing timestamps/GUID and malformed body ignored', async () => {
  const h = harness({offline: true});
  await h.fetchReport({events: [null, {eventType: 'track_pause'}, {eventType: 'track_play'},
    {eventType: 'track_play', payload: {trackGUID: 'one'}}, play(''), play('one', 0)]});
  await h.fetchReport('broken JSON');
  await h.fetchReport({events: 'not-array'});
  await settle(); assert.equal(h.state.ingests.length, 0); assert.deepEqual(h.queued(), []);
});

test('Request body cloning preserves original NAS request and captures', async () => {
  const h = harness();
  const request = new Request('http://127.0.0.1:5679' + EVENT_URL, {method: 'POST', body: JSON.stringify(play())});
  await h.window.fetch(request); await settle();
  assert.deepEqual(h.state.ingests[0].body.events, [play()]);
  assert.equal(request.bodyUsed, false);
});

test('Blob and ArrayBuffer bodies are supported', async () => {
  for (const body of [new Blob([JSON.stringify(play())]), new TextEncoder().encode(JSON.stringify(play())).buffer]) {
    const h = harness(); await h.window.fetch(EVENT_URL, {method: 'POST', body}); await settle();
    assert.deepEqual(h.state.ingests[0].body.events, [play()]);
  }
});

test('XHR text and responseType json capture code-zero success', async () => {
  for (const responseType of ['', 'json']) {
    const h = harness(); h.xhrReport(play(), responseType); await settle();
    assert.deepEqual(h.state.ingests[0].body.events, [play()]);
  }
});

test('XHR errors and code nonzero do not enqueue', async () => {
  for (const [status, body, raw] of [[200, {code: 2}], [200, {code: false}], [200, {code: '0'}],
    [401, {code: 0}], [503, {code: 0}], [200, null, '{broken']]) {
    const h = harness({offline: true});
    h.state.upstreamStatus = status; h.state.upstreamBody = body; h.state.upstreamRaw = raw ?? null;
    h.xhrReport(play()); await settle(); assert.equal(h.state.ingests.length, 0);
  }
});

test('fetch and XHR same pending event share one queue entry, repeated listens retained', async () => {
  const h = harness({offline: true});
  await h.fetchReport({events: [play(), play(), play('two')]}); await settle();
  h.xhrReport(play()); await h.fetchReport(play('one', STAMP + 180000)); await settle();
  assert.deepEqual(h.queued().map(item => item.key).sort(),
    [`one:${STAMP}`, `one:${STAMP + 180000}`, `two:${STAMP}`].sort());
});

test('offline queue persists and successful online retry removes exactly accepted batch', async () => {
  const h = harness({offline: true});
  await h.fetchReport([play(), play('two')]); await settle();
  assert.equal(h.queued().length, 2);
  h.state.offline = false; await h.retry();
  assert.equal(h.state.ingests.length, 2);
  assert.deepEqual(h.state.ingests[1].body.events, [play(), play('two')]);
  assert.deepEqual(h.queued(), []);
});

test('HTTP 401/409/500, ok false and malformed ingest reply retain retry queue', async () => {
  for (const [status, body, raw] of [[401, {ok: false}], [409, {ok: false}], [500, {ok: true}],
    [200, {ok: false}], [200, null, 'broken']]) {
    const h = harness(); h.state.ingestStatus = status; h.state.ingestBody = body; h.state.ingestRaw = raw ?? null;
    await h.fetchReport(play()); await settle(); assert.equal(h.queued().length, 1);
    h.state.ingestStatus = 200; h.state.ingestBody = {ok: true, deduped: 1}; h.state.ingestRaw = null;
    await h.retry(); assert.deepEqual(h.queued(), []);
  }
});

test('queued events restored after page reload and timer retries while offline', async () => {
  const storage = new Map(); const h = harness({storage, offline: true});
  await h.fetchReport(play()); await settle();
  const reload = harness({storage, offline: true}); await settle();
  assert.equal(reload.queued().length, 1); assert.equal(reload.state.ingests.length, 1);
  await reload.tick(); assert.equal(reload.queued().length, 1); assert.equal(reload.state.ingests.length, 2);
  assert.equal(reload.state.intervals[0].delay, 10000);
  reload.state.offline = false; await reload.tick(); assert.deepEqual(reload.queued(), []);
});

test('100 item batches drain larger queues without loss', async () => {
  const h = harness({offline: true});
  const events = Array.from({length: 205}, (_, i) => play('one', STAMP + i * 1000));
  await h.fetchReport({events}); await settle(); assert.equal(h.queued().length, 205);
  h.state.offline = false;
  await h.retry(); assert.equal(h.queued().length, 105);
  await h.tick(); assert.equal(h.queued().length, 5);
  await h.tick(); assert.equal(h.queued().length, 0);
  assert.deepEqual(h.state.ingests.slice(1).map(call => call.body.events.length), [100, 100, 5]);
  assert.deepEqual(h.state.ingests.slice(1).flatMap(call => call.body.events), events);
});

test('inflight batch does not drop newer events and prevents concurrent sends', async () => {
  const h = harness(); h.state.pendingResolver = {};
  await h.fetchReport(play()); await settle();
  await h.fetchReport(play('two')); await settle();
  assert.equal(h.state.ingests.length, 1); assert.equal(h.queued().length, 2);
  const resolve = h.state.pendingResolver.resolve; h.state.pendingResolver = null;
  resolve(jsonResponse({ok: true})); await settle();
  assert.deepEqual(h.queued().map(item => item.event), [play('two')]);
  await h.tick(); assert.deepEqual(h.queued(), []);
});

test('corrupt localStorage and unavailable storage do not break NAS fetch', async () => {
  for (const value of ['not-json', '{}', 'null']) {
    const storage = new Map([['music-journal.pending.v1:unassigned', value]]);
    const h = harness({storage}); await h.fetchReport(play()); await settle();
    assert.equal(h.state.ingests.length, 1);
  }
  const h = harness({storageThrows: true});
  const response = await h.fetchReport(play()); await settle();
  assert.equal(response.status, 200); assert.equal(h.state.ingests.length, 1);
});

test('loading document mounts once on DOMContentLoaded and never hooks twice', async () => {
  const h = harness({readyState: 'loading'});
  assert.equal(h.document.body.children.length, 0);
  h.document.emit('DOMContentLoaded'); h.document.emit('DOMContentLoaded');
  assert.equal(h.document.body.children.length, 1);
  assert.equal(h.document.body.children[0].querySelector('a').href, '/');
  const fetch = h.window.fetch; h.load();
  assert.equal(h.window.fetch, fetch); assert.equal(h.state.intervals.length, 1);
  await h.fetchReport(play()); await settle(); assert.equal(h.state.ingests.length, 1);
});

test('queue is account scoped and stale account 409 keeps only original account queue', async () => {
  const storage = new Map(); const alice = harness({storage, account: 'hash-alice'});
  alice.state.ingestStatus = 409; alice.state.ingestBody = {ok: false};
  await alice.fetchReport(play()); await settle();
  assert.equal(alice.queued().length, 1);
  assert.equal(alice.state.ingests[0].options.headers['X-Journal-Account'], 'hash-alice');
  const bob = harness({storage, account: 'hash-bob'}); await settle();
  assert.equal(bob.state.ingests.length, 0); assert.deepEqual(bob.queued(), []);
  await bob.fetchReport(play('two')); await settle();
  assert.deepEqual(bob.queued(), []); assert.equal(alice.queued().length, 1);
  alice.state.ingestStatus = 200; alice.state.ingestBody = {ok: true}; await alice.retry();
  assert.deepEqual(alice.queued(), []);
});

test('at-least-once resent confirmed event is accepted as deduped, not lost offline', async () => {
  const h = harness(); await h.fetchReport(play()); await settle();
  assert.equal(h.state.ingests.length, 1); assert.deepEqual(h.queued(), []);
  h.state.offline = true; await h.fetchReport(play()); await settle();
  assert.equal(h.queued().length, 1);
  h.state.offline = false; h.state.ingestBody = {ok: true, accepted: 0, deduped: 1};
  await h.retry(); assert.deepEqual(h.queued(), []);
});
