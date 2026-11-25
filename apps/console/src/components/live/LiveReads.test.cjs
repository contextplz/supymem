const assert = require('node:assert/strict');
const { test } = require('node:test');

const flush = async () => { for (let turn = 0; turn < 50; turn++) await Promise.resolve(); };

test('native streams retry transient server errors before surfacing a persistent failure while access denials remain visible across domains', async context => {
  const { NativeEventSource } = await import('../../lib/nativeTransport.ts');
  const saved = global.window;
  context.after(() => { global.window = saved; });
  for (const [index, domain] of ['procurement teams at factories', 'friends organizing neighborhood games'].entries()) {
    const callbacks = new Map(), events = [];
    global.window = { dispatchEvent: event => { events.push(event); return true; }, __TAURI__: {
      core: { invoke: async () => {} }, event: { listen: async (name, callback) => { callbacks.set(name, callback); return () => callbacks.delete(name); } },
    } };
    const source = new NativeEventSource(`/api/ventures/ven_${index}/activity/stream`, '12');
    let errors = 0; source.onerror = () => errors++;
    await flush();
    const frame = (kind, status) => callbacks.get('supymem-native-sse')({ payload: { id: source.id, kind, status, data: domain } });
    for (const status of [503, 500, 408]) frame('error', status);
    const diagnostics = () => events.filter(event => event.detail?.context === 'api').map(event => event.detail);
    assert.deepEqual(diagnostics().map(detail => detail.severity), ['transient', 'transient', 'transient']);
    assert.equal(errors, 0);
    frame('error', 429); assert.equal(errors, 1); assert.equal(diagnostics().at(-1).severity, undefined);
    frame('open', 200); frame('error', 503); assert.equal(errors, 1); assert.equal(diagnostics().at(-1).severity, 'transient');
    frame('error', 403); assert.equal(errors, 2); assert.equal(diagnostics().at(-1).severity, undefined);
    assert.ok(diagnostics().every(detail => !JSON.stringify(detail).includes(domain)));
    source.close(); await flush(); assert.equal(callbacks.size, 0);
  }
});

test('live reads back off, time out, and stop on cancellation or account changes without retrying writes across domains and transports', async context => {
  const client = await import('../../lib/client.ts');
  const savedWindow = global.window, savedStorage = global.sessionStorage;
  const store = new Map();
  global.sessionStorage = { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key) };
  context.after(() => { context.mock.restoreAll(); global.window = savedWindow; global.sessionStorage = savedStorage; client.clearVerifiedSession(); });
  for (const domain of ['accounting teams at wholesalers', 'runners organizing park meetups']) {
    for (const native of [false, true]) {
      client.clearVerifiedSession();
      const events = [], calls = [], timers = new Map();
      let now = 0, timerId = 0, mode = 'recover', failures = 0;
      const respond = async (path, init = {}) => {
        if (path === '/auth/session') return { status: 200, data: { authenticated: true, subject: 'usr_live' } };
        calls.push({ path, method: init.method || 'GET' });
        if (mode === 'hang' && failures++ === 0) {
          if (native) return new Promise(() => {});
          return new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(new DOMException('stopped', 'AbortError')), { once: true }));
        }
        if (mode === 'fail') return { status: 503, data: null };
        if (mode === 'rate' && failures++ === 0) return { status: 429, data: { error: { retry_after: 5 } } };
        if (mode === 'recover' && failures++ < 2) {
          if (failures === 1) throw new TypeError('offline');
          return { status: 503, data: null };
        }
        return { status: 200, data: { domain } };
      };
      global.window = { dispatchEvent: event => { events.push(event); return true; }, location: { origin: 'https://fixture.invalid' },
        ...(native ? { __TAURI__: { core: { invoke: async (command, args) => {
          if (command === 'gateway_url') return 'https://fixture.invalid';
          assert.equal(command, 'desktop_api_request');
          return respond(args.path, args);
        } } } } : {}),
      };
      context.mock.method(globalThis, 'fetch', async (url, init) => {
        assert.equal(native, false, 'Native reads must never fall back to browser cookies');
        const reply = await respond(new URL(url).pathname, init);
        return new Response(JSON.stringify(reply.data), { status: reply.status, headers: { 'Content-Type': 'application/json' } });
      });
      assert.equal((await client.ensureSession('unused', true)).ok, true);
      context.mock.method(globalThis, 'setTimeout', (callback, delay) => { timers.set(++timerId, { callback, at: now + delay }); return timerId; });
      context.mock.method(globalThis, 'clearTimeout', id => timers.delete(id));
      const advance = async milliseconds => {
        const until = now + milliseconds;
        for (;;) {
          const next = [...timers].filter(([, timer]) => timer.at <= until).sort((left, right) => left[1].at - right[1].at)[0];
          if (!next) break;
          now = next[1].at; timers.delete(next[0]); next[1].callback(); await flush();
        }
        now = until; await flush();
      };
      const path = '/api/ventures/ven_saved/activity';
      let waiting = client.apiLive(path); await flush();
      assert.equal(calls.length, 1);
      await advance(999); assert.equal(calls.length, 1);
      await advance(1); assert.equal(calls.length, 2);
      await advance(1999); assert.equal(calls.length, 2);
      await advance(1);
      assert.deepEqual((await waiting).data, { domain });
      assert.ok(events.filter(event => event.detail?.context === 'api').every(event => event.detail.severity === 'transient'));

      mode = 'rate'; failures = 0; calls.length = 0;
      waiting = client.apiLive(path); await flush(); await advance(4999);
      assert.equal(calls.length, 1); await advance(1);
      assert.deepEqual((await waiting).data, { domain }); assert.equal(calls.length, 2);

      mode = 'fail'; calls.length = 0;
      waiting = client.apiLive(path); await flush(); await advance(7000);
      assert.equal((await waiting).status, 503); assert.equal(calls.length, 4);
      assert.equal(timers.size, 0);

      mode = 'hang'; failures = 0; calls.length = 0;
      waiting = client.apiLive(path); await flush(); await advance(15000);
      assert.equal(calls.length, 1); await advance(1000);
      assert.deepEqual((await waiting).data, { domain }); assert.equal(calls.length, 2);

      mode = 'fail'; calls.length = 0;
      const controller = new AbortController();
      waiting = client.apiLive(path, controller.signal); await flush(); controller.abort(); await flush();
      assert.equal((await waiting).status, 409); assert.equal(calls.length, 1); assert.equal(timers.size, 0);
      waiting = client.apiLive(path); await flush(); client.clearVerifiedSession(); await advance(1000);
      assert.equal((await waiting).status, 409); assert.equal(calls.length, 2);

      assert.equal((await client.ensureSession('unused', true)).ok, true);
      calls.length = 0;
      assert.equal((await client.api(path, { method: 'POST', body: '{}' })).status, 503);
      assert.equal(calls.length, 1, 'Never retry a write'); assert.equal(timers.size, 0);
      context.mock.restoreAll();
    }
  }
});
