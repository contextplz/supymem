const assert = require('node:assert/strict');
const { test } = require('node:test');

test('concurrent local JSON reads share one request per URL across read helpers', async () => {
  const client = await import('./client.ts');
  const originalFetch = globalThis.fetch;
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  try {
    for (const domain of ['warehouse.example', 'walking.example']) {
      const origin = `https://${domain}`;
      Object.defineProperty(globalThis, 'window', {configurable: true, value: {__SUPYMEM__: {mode: 'local'}, location: {origin}}});
      const calls = [];
      let release;
      const gate = new Promise(resolve => { release = resolve; });
      globalThis.fetch = async url => { calls.push(url); await gate; return Response.json({url}); };
      const path = `/api/ventures/${domain}/chat?conversation_id=saved`;
      const controller = new AbortController();
      const retired = client.apiLive(path, controller.signal);
      const stopped = client.api(path, {signal: controller.signal});
      const reads = [client.api(path), client.apiSwr(path), client.apiLive(path), client.api(path + '&before=older')];
      controller.abort();
      release();
      assert.equal((await retired).status, 409);
      assert.equal((await stopped).status, 0);
      assert.ok((await Promise.all(reads)).every(result => result.ok));
      assert.deepEqual(calls, [origin + path, origin + path + '&before=older']);
      await client.api(path);
      assert.equal(calls.length, 3);
    }
  } finally {
    globalThis.fetch = originalFetch;
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
    else delete globalThis.window;
  }
});

test('local operations URLs send the core version prefix exactly once', async () => {
  const {resolveGateway} = await import('./client.ts');
  const original = Object.getOwnPropertyDescriptor(globalThis, 'window');
  try {
    for (const domain of ['warehouse.example', 'walking.example']) {
      const origin = `https://${domain}`;
      Object.defineProperty(globalThis, 'window', {configurable: true, value: {__SUPYMEM__: {mode: 'local'}, location: {origin}}});
      const suffix = `/connections/status?workspace_id=${domain}`;
      assert.equal(resolveGateway(`/api/api/v1${suffix}`), `${origin}/api/v1${suffix}`);
      assert.equal(resolveGateway(`/api/v1${suffix}`), `${origin}/api/v1${suffix}`);
    }
  } finally {
    if (original) Object.defineProperty(globalThis, 'window', original);
    else delete globalThis.window;
  }
});

test('local browser activity streams use proof-bearing fetch and resume without putting proof in the URL', {timeout: 3000}, async () => {
  const {createAuthenticatedEventSource} = await import('./nativeTransport.ts');
  const originalFetch = globalThis.fetch;
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  try {
    for (const domain of ['warehouse.example', 'walking.example']) {
      const requests = [];
      let stream;
      let cancelStream;
      const cancelled = new Promise(resolve => {cancelStream = resolve;});
      const origin = `http://${domain}`;
      const path = '/api/ventures/saved/activity/stream';
      Object.defineProperty(globalThis, 'window', {configurable: true, value: {__SUPYMEM__: {mode: 'local'}, location: {origin}}});
      globalThis.fetch = async (url, init) => {
        const headers = new Headers(init.headers);
        headers.set('X-Local-Session-Proof', 'fixture-document-proof');
        requests.push({address: new URL(url, origin), headers, init});
        return new Response(new ReadableStream({start(controller) {stream = controller;}, cancel: cancelStream}), {headers: {'Content-Type': 'text/event-stream'}});
      };
      const source = createAuthenticatedEventSource(path, origin, '12');
      const event = new Promise(resolve => source.addEventListener('activity', resolve, {once: true}));
      const dropped = new Promise(resolve => {source.onerror = resolve;});
      assert.equal(requests.length, 1);
      assert.equal(requests[0].address.href, origin + path);
      assert.equal(requests[0].headers.get('X-Local-Session-Proof'), 'fixture-document-proof');
      assert.equal(requests[0].headers.get('Last-Event-ID'), '12');
      assert.equal(requests[0].headers.get('Accept'), 'text/event-stream');
      assert.equal(requests[0].init.credentials, 'same-origin');
      assert.equal(requests[0].init.redirect, 'error');
      const encoder = new TextEncoder();
      stream.enqueue(encoder.encode('id: 13\r\nevent: activity\r\ndata: ' + domain + '\r'));
      stream.enqueue(encoder.encode('\ndata: saved\r\n\r\n'));
      stream.close();
      const received = await event;
      assert.equal(received.lastEventId, '13');
      assert.equal(received.data, `${domain}\nsaved`);
      await dropped;
      const resumed = createAuthenticatedEventSource(path, origin, received.lastEventId);
      assert.equal(requests[1].headers.get('Last-Event-ID'), '13');
      await new Promise(resolve => resumed.addEventListener('open', resolve, {once: true}));
      resumed.close();
      assert.equal(requests[1].init.signal.aborted, true);
      await cancelled;
    }
  } finally {
    globalThis.fetch = originalFetch;
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
    else delete globalThis.window;
  }
});

test('local telemetry never contacts your API across unrelated domains', async () => {
  const client = await import('./client.ts');
  const originals = Object.fromEntries(['window', 'fetch'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  try {
    for (const domain of ['warehouse.example', 'walking.example']) {
      Object.defineProperty(globalThis, 'window', {configurable: true, value: {
        __SUPYMEM__: {mode: 'local'}, location: {origin: `https://${domain}`}, dispatchEvent() {},
      }});
      let calls = 0;
      globalThis.fetch = async () => { calls++; return Response.json({ok: true}); };
      const response = await client.api('/api/telemetry', {method: 'POST', body: JSON.stringify({domain})});
      assert.equal(response.ok, true);
      assert.equal(calls, 0);
    }
  } finally {
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('local gateway paths leave hosted gateway paths unchanged across domains', async () => {
  const client = await import('./client.ts');
  const originals = Object.fromEntries(['window', 'sessionStorage'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  try {
    for (const domain of ['warehouse.example', 'walking.example']) for (const local of [false, true]) {
      Object.defineProperty(globalThis, 'window', {configurable: true, value: {__SUPYMEM__: {mode: local ? 'local' : 'hosted'}, location: {origin: `http://${domain}`}}});
      Object.defineProperty(globalThis, 'sessionStorage', {configurable: true, value: {getItem: () => `https://${domain}/`}});
      assert.equal(client.resolveGateway(), `${local ? 'http' : 'https'}://${domain}`);
      for (const path of ['/api/workspaces', '/auth/session', '/setup/status']) {
        assert.equal(client.resolveGateway(path), `${local ? 'http' : 'https'}://${domain}${local && !path.startsWith('/api/') ? '/api' : ''}${path}`, domain);
      }
    }
  } finally {
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('production loopback sign-in keeps your passphrase fallback while local owner checks never sign in across domains', async () => {
  const client = await import('./client.ts');
  const originals = Object.fromEntries(['window', 'fetch', 'sessionStorage', 'document'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  try {
    for (const domain of ['procurement.example', 'gardening.example']) for (const local of [false, true]) for (const loopback of [false, true]) for (const passphrase of [undefined, `test-only-${domain}`]) {
      const origin = loopback ? 'http://127.0.0.1:3200' : `https://${domain}`, calls = [];
      const store = {getItem: key => key === 'cos_gateway_url' ? origin : null, setItem() {}, removeItem() {}};
      Object.defineProperty(globalThis, 'document', {configurable: true, value: {cookie: ''}});
      Object.defineProperty(globalThis, 'sessionStorage', {configurable: true, value: store});
      Object.defineProperty(globalThis, 'window', {configurable: true, value: {
        __SUPYMEM__: {mode: local ? 'local' : 'hosted'}, location: {origin}, localStorage: store, dispatchEvent: () => true,
      }});
      globalThis.fetch = async (url, init) => {
        calls.push({path: new URL(url).pathname, body: init.body && JSON.parse(init.body)});
        return calls.at(-1).path.endsWith('/auth/session')
          ? Response.json({error: {code: 'session_invalid'}}, {status: 401})
          : Response.json({authenticated: true, subject: `per_${domain}`, csrf: 'test-csrf'});
      };
      client.clearVerifiedSession();
      const result = await client.ensureSession(passphrase, true);
      assert.equal(result.ok, !local && loopback, domain);
      assert.equal(calls.length, !local && loopback ? 2 : 1, domain);
      if (!local && loopback) assert.equal(calls[1].body.passphrase, passphrase ?? 'changeme');
    }
  } finally {
    client.clearVerifiedSession();
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('local startup shares one owner check and sends your first idea without hosted credentials', async () => {
  const client = await import('./client.ts');
  const {listenForUsageSubject} = await import('./usageCollectorState.ts');
  const originals = Object.fromEntries(['window', 'fetch', 'sessionStorage', 'document'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  try {
    for (const domain of ['Supplier audit scheduling', 'Neighborhood cooking classes']) for (const native of [false, true]) {
      const calls = [], storage = new Map([['supymem.invite', JSON.stringify({code: 'INVITE42', at: Date.now()})]]);
      const events = new EventTarget(), sessionStates = [], hostedStates = [];
      listenForUsageSubject(events, state => sessionStates.push(state), true);
      listenForUsageSubject(events, state => hostedStates.push(state));
      const store = {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)};
      Object.defineProperty(globalThis, 'document', {configurable: true, value: {cookie: ''}});
      Object.defineProperty(globalThis, 'sessionStorage', {configurable: true, value: store});
      Object.defineProperty(globalThis, 'window', {configurable: true, value: {
        __SUPYMEM__: {mode: 'local'}, location: {origin: 'http://127.0.0.1:3100'}, sessionStorage: store, localStorage: store,
        dispatchEvent: event => events.dispatchEvent(event), ...(native ? {__TAURI_INTERNALS__: {invoke: async () => assert.fail('Local requests use the local stack')}} : {}),
      }});
      globalThis.fetch = async (url, init) => {
        const path = new URL(url).pathname;
        calls.push({path, init});
        return Response.json(path.endsWith('/auth/session')
          ? {authenticated: true, subject: 'per_local000000', email: null, csrf: null, mode: 'local'} : {domain});
      };
      client.clearVerifiedSession();
      sessionStates.length = 0;
      hostedStates.length = 0;
      const sessions = await Promise.all(Array.from({length: 4}, () => client.ensureSession(undefined, true)));
      assert.ok(sessions.every(session => session.ok), domain);
      const result = await client.api('/api/entry/chat/preview', {method: 'POST', body: JSON.stringify({text: domain, source_kind: 'typed', evidence_ids: [], request_id: crypto.randomUUID()})});
      assert.equal(result.ok, true, domain);
      assert.equal(result.data.domain, domain);
      assert.deepEqual(calls.map(call => call.path), ['/api/auth/session', '/api/entry/chat/preview']);
      assert.equal(new Headers(calls[1].init.headers).has('X-CSRF-Token'), false);
      assert.equal(client.verifiedSessionSubject(), 'per_local000000');
      assert.deepEqual(sessionStates, [{subject: 'per_local000000', recheck: false}]);
      assert.deepEqual(hostedStates, [{subject: undefined, recheck: true}]);
    }
  } finally {
    client.clearVerifiedSession();
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('local owner requests use your console without native or passphrase sign-in across domains', async () => {
  const client = await import('./client.ts');
  const originals = Object.fromEntries(['window', 'fetch', 'sessionStorage', 'document'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  try {
    for (const domain of ['procurement.example', 'gardening.example']) {
      const calls = [], subject = `per_${domain}`;
      Object.defineProperty(globalThis, 'document', {configurable: true, value: {cookie: ''}});
      Object.defineProperty(globalThis, 'sessionStorage', {configurable: true, value: {getItem: () => 'https://stale-gateway.invalid', setItem() {}, removeItem() {}}});
      Object.defineProperty(globalThis, 'window', {configurable: true, value: {
        __SUPYMEM__: {mode: 'local'}, location: {origin: `http://${domain}`}, dispatchEvent: () => true,
        __TAURI_INTERNALS__: {invoke: async () => assert.fail('Your local owner does not need native sign-in')},
      }});
      globalThis.fetch = async (url, init) => {
        assert.equal(new Headers(init.headers).get('x-local-proxy-secret'), null);
        assert.equal(new URL(String(url)).origin, `http://${domain}`);
        calls.push(new URL(String(url)).pathname);
        return new Response(JSON.stringify(String(url).endsWith('/auth/session')
          ? {authenticated: true, subject, mode: 'local', csrf: null}
          : {domain}), {status: 200});
      };
      client.clearVerifiedSession();
      assert.equal((await client.ensureSession(undefined, true)).ok, true);
      assert.equal(client.verifiedSessionSubject(), subject);
      const result = await client.api('/api/workspaces', {method: 'POST', body: JSON.stringify({name: domain})});
      assert.equal(result.ok, true);
      assert.equal(result.data.domain, domain);
      assert.deepEqual([...new Set(calls)], ['/api/auth/session', '/api/workspaces']);
      await client.api('/setup/status');
      await client.authenticatedFetch('/exports/current');
      assert.deepEqual(calls.slice(-2), ['/api/setup/status', '/api/exports/current']);
      const { createAuthenticatedEventSource, NativeEventSource } = await import('./nativeTransport.ts');
      const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'EventSource');
      try {
        Object.defineProperty(globalThis, 'EventSource', { configurable: true, value: class {
          constructor(url, options) { this.url = url; this.options = options; }
        } });
        const nativeCalls = [], path = '/api/ventures/owner/activity/stream';
        window.__TAURI__ = { event: { listen: async () => () => {} } };
        window.__TAURI_INTERNALS__.invoke = async (command, args) => { nativeCalls.push({ command, args }); };
        const native = createAuthenticatedEventSource(path, client.resolveGateway());
        assert.ok(native instanceof NativeEventSource);
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(nativeCalls, [{ command: 'desktop_events_subscribe', args: { id: native.id, path, lastEventId: undefined } }]);
        native.close();
        assert.equal(nativeCalls.at(-1).command, 'desktop_events_unsubscribe');
        delete window.__TAURI__;
        delete window.__TAURI_INTERNALS__;
        window.__SUPYMEM__.mode = 'hosted';
        sessionStorage.getItem = () => `http://${domain}`;
        const browser = createAuthenticatedEventSource(path, client.resolveGateway());
        assert.equal(browser.url,
          `http://${domain}/api/ventures/owner/activity/stream`);
        assert.equal(browser.options.withCredentials, true);
      } finally {
        if (descriptor) Object.defineProperty(globalThis, 'EventSource', descriptor);
        else delete globalThis.EventSource;
      }
      window.__SUPYMEM__.mode = 'hosted';
      delete window.__TAURI_INTERNALS__;
      sessionStorage.getItem = () => `http://${domain}`;
      globalThis.fetch = async url => {
        assert.equal(new URL(String(url)).pathname, '/auth/session', 'Hosted sign-in never posts a default passphrase');
        return new Response(JSON.stringify({authenticated: false}), {status: 401});
      };
      client.clearVerifiedSession();
      assert.equal((await client.ensureSession(undefined, true)).status, 401);
      assert.equal(client.verifiedSessionSubject(), null);
    }
  } finally {
    client.clearVerifiedSession();
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('native first-run session reads time out without cookie fallback or losing either startup account', async () => {
  const client = await import('./client.ts');
  const originals = Object.fromEntries(['window', 'fetch', 'sessionStorage'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const originalTimeout = AbortSignal.timeout;
  try {
    for (const startupDomain of ['linear.app', 'alltrails.com']) for (const stalledCommand of ['gateway_url', 'desktop_api_request']) {
      const subject = `usr_${startupDomain}`, controllers = [], calls = [];
      let stalled = false, finishRead;
      AbortSignal.timeout = milliseconds => { assert.equal(milliseconds, 15_000); const controller = new AbortController(); controllers.push(controller); return controller.signal; };
      Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: { getItem: () => null, setItem() {}, removeItem() {} } });
      Object.defineProperty(globalThis, 'window', { configurable: true, value: {
        location: { origin: 'https://console.fixture.invalid' }, dispatchEvent: () => true,
        __TAURI_INTERNALS__: { invoke: async (command, args) => {
          calls.push({ command, args });
          if (stalled && command === stalledCommand) return new Promise(resolve => { finishRead = resolve; });
          if (command === 'gateway_url') return 'https://gateway.fixture.invalid';
          assert.equal(command, 'desktop_api_request'); assert.equal(args.method, 'GET');
          assert.equal(args.path, '/auth/session'); assert.deepEqual(Object.keys(args.headers), []);
          return { status: 200, data: { authenticated: true, subject } };
        } },
      } });
      globalThis.fetch = async () => { assert.fail('Native sign-in must not fall back to browser cookies'); };
      client.clearVerifiedSession(); assert.equal((await client.ensureSession('unused', true)).ok, true);
      const epoch = client.verifiedSessionEpoch(), before = controllers.length;
      stalled = true; const pending = client.ensureSession('unused', true);
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(controllers.length, before + (stalledCommand === 'gateway_url' ? 1 : 2), `${startupDomain}: ${stalledCommand}`);
      controllers.at(-1).abort();
      assert.equal((await pending).status, 0);
      assert.equal(client.verifiedSessionSubject(), subject); assert.equal(client.verifiedSessionEpoch(), epoch);
      finishRead(stalledCommand === 'gateway_url' ? 'https://old.fixture.invalid'
        : { status: 200, data: { authenticated: true, subject: 'usr_retired_account' } });
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(client.verifiedSessionSubject(), subject);
      stalled = false; assert.equal((await client.ensureSession('unused', true)).ok, true);
      assert.ok(calls.every(call => ['gateway_url', 'desktop_api_request'].includes(call.command)));
    }
  } finally {
    client.clearVerifiedSession(); AbortSignal.timeout = originalTimeout;
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  }
});

test('first-run native saved-state reads have a deadline even with a caller signal for both startups', async () => {
  const client = await import('./client.ts');
  const originals = Object.fromEntries(['window', 'fetch'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const originalTimeout = AbortSignal.timeout;
  try {
    for (const startupDomain of ['linear.app', 'alltrails.com']) for (const path of ['/api/ventures?summary=true', '/api/me/greeting', '/api/entry/chat/history']) {
      const controllers = [], calls = [], caller = new AbortController();
      let finishRead, stalled = true;
      AbortSignal.timeout = milliseconds => { assert.equal(milliseconds, 15_000); const controller = new AbortController(); controllers.push(controller); return controller.signal; };
      Object.defineProperty(globalThis, 'window', { configurable: true, value: {
        location: { origin: 'https://console.fixture.invalid' }, dispatchEvent: () => true,
        __TAURI_INTERNALS__: { invoke: async (command, args) => {
          calls.push({ command, args }); assert.equal(command, 'desktop_api_request');
          assert.equal(args.method, 'GET'); assert.equal(args.path, path); assert.deepEqual(Object.keys(args.headers), []);
          if (stalled) return new Promise(resolve => { finishRead = resolve; });
          return { status: 200, data: { startupDomain } };
        } },
      } });
      globalThis.fetch = async () => { assert.fail('A native read must never fall back to cookies'); };
      client.clearVerifiedSession();
      const pending = client.api(path, { signal: caller.signal });
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(controllers.length, 1, `${startupDomain}: ${path}`);
      controllers[0].abort(); assert.equal((await pending).status, 0);
      assert.equal(caller.signal.aborted, false); assert.equal(calls.length, 1);
      stalled = false; assert.equal((await client.api(path, { signal: caller.signal })).data.startupDomain, startupDomain);
      finishRead({ status: 200, data: { startupDomain: 'retired-response' } });
      await new Promise(resolve => setImmediate(resolve)); assert.equal(calls.length, 2);
    }
  } finally {
    client.clearVerifiedSession(); AbortSignal.timeout = originalTimeout;
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  }
});

test('route checks and feature failures preserve sessions through restarts across unrelated domains', async () => {
  const client = await import('./client.ts');
  const cache = await import('./apiCache.ts');
  const originals = Object.fromEntries(['window', 'fetch', 'sessionStorage'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const store = new Map();
  store.set('cos_gateway_url', 'https://fixture.invalid');
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: {
    getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, String(value)),
    removeItem: key => store.delete(key), key: index => [...store.keys()][index], get length() { return store.size; },
  } });
  try {
    for (const [subject, audience] of [['usr_dispatch', 'commercial dispatch teams'], ['usr_gardens', 'home gardeners']]) {
      for (const native of [false, true]) {
        client.clearVerifiedSession();
        let failure = null, featureFailure = null, state = 'connected';
        const events = [], commands = [];
        const session = () => {
          if (failure instanceof Error) throw failure;
          return failure || { status: 200, data: { authenticated: true, subject, csrf: 'fixture-csrf' } };
        };
        const transfer = path => {
          if (path === '/auth/session') return session();
          if (featureFailure instanceof Error) throw featureFailure;
          return featureFailure || { status: 200, data: { audience } };
        };
        const invoke = async (command, args) => {
          commands.push(command);
          if (command === 'gateway_url') return 'https://fixture.invalid';
          if (command === 'desktop_auth_status') return { state, subject };
          if (command === 'desktop_http_request') {
            const reply = transfer(args.path);
            return { status: reply.status, bodyBase64: btoa(JSON.stringify(reply.data)), headers: {} };
          }
          assert.equal(command, 'desktop_api_request');
          assert.deepEqual(Object.keys(args.headers), []);
          return transfer(args.path);
        };
        Object.defineProperty(globalThis, 'window', { configurable: true, value: {
          location: { origin: 'https://console.fixture.invalid' },
          ...(native ? { __TAURI_INTERNALS__: { invoke } } : {}),
          dispatchEvent: event => { events.push(event); return true; },
        } });
        globalThis.fetch = async (url, init) => {
          assert.equal(native, false, 'Mac sessions must never fall back to cookies');
          const path = new URL(url).pathname;
          if (init?.signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
          if (path === '/auth/session') assert.ok(init.signal, 'Hung browser session checks must time out');
          const reply = transfer(path);
          return new Response(JSON.stringify(reply.data), { status: reply.status, headers: { 'Content-Type': 'application/json' } });
        };
        assert.equal((await client.ensureSession('unused', true)).ok, true);
        const epoch = client.verifiedSessionEpoch();
        const path = '/api/ventures/ven_restart/summary';
        cache.cachePut(subject, path, { audience });
        for (const outage of [502, 503, 504, 401, 403, new DOMException('Timed out', 'TimeoutError'), new TypeError('Connection reset')]) {
          failure = typeof outage === 'number' ? { status: outage, data: { authenticated: false, error: { code: 'core_unavailable' } } } : outage;
          assert.equal((await client.ensureSession('unused', true)).ok, false);
          assert.equal(client.verifiedSessionSubject(), subject);
          assert.equal(client.verifiedSessionEpoch(), epoch);
          assert.deepEqual(cache.cacheGet(subject, path)?.data, { audience });
          featureFailure = { status: 401, data: { detail: { code: 'unauthenticated' } } };
          assert.equal((await client.api('/api/ventures/ven_restart/activity', { method: 'HEAD' })).ok, false);
          assert.equal(client.verifiedSessionSubject(), subject, 'A feature 401 during a failed verification is not a sign-out');
          featureFailure = null;
          failure = null;
          assert.equal((await client.ensureSession('unused', true)).ok, true);
          assert.equal(client.verifiedSessionEpoch(), epoch);
        }
        assert.ok(events.some(event => event.type === 'supymem-session-reconnecting'));
        assert.equal(events.filter(event => event.type === 'supymem-session-changed').length, 1);
        featureFailure = new TypeError('Transfer connection reset');
        const beforeTransfer = events.length;
        await assert.rejects(client.authenticatedFetch('/api/ventures/ven_restart/export'));
        assert.ok(events.slice(beforeTransfer).some(event => event.type === 'supymem-session-reconnecting'));
        assert.equal(client.verifiedSessionSubject(), subject);
        featureFailure = null;
        const stopped = new AbortController(); stopped.abort();
        const beforeStop = events.filter(event => event.type === 'supymem-session-reconnecting').length;
        assert.equal((await client.api('/api/ventures/ven_restart/activity', { signal: stopped.signal })).ok, false);
        assert.equal(events.filter(event => event.type === 'supymem-session-reconnecting').length, beforeStop);
        failure = { status: 401, data: { error: { code: 'session_invalid' } } };
        state = 'reauth_required';
        const invalid = await client.ensureSession('unused', true);
        assert.equal(invalid.ok, false);
        assert.equal(client.verifiedSessionSubject(), null);
        assert.equal(cache.cacheGet(subject, path), null, 'Confirmed sign-out still removes private cached data');
        if (native) assert.ok(commands.every(command => ['gateway_url', 'desktop_auth_status', 'desktop_api_request', 'desktop_http_request'].includes(command)));
        failure = null; state = 'connected';
        assert.equal((await client.ensureSession('unused', true)).ok, true);
        failure = { status: 200, data: { authenticated: false } };
        assert.equal(client.isInvalidSession(await client.ensureSession('unused', true)), true);
        assert.equal(client.verifiedSessionSubject(), null);
      }
    }
  } finally {
    client.clearVerifiedSession();
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('09: delete and undo invalidate fresh venture lists, including the switcher', async () => {
  const client = await import('./client.ts');
  const cache = await import('./apiCache.ts');
  const originals = Object.fromEntries(['window', 'fetch', 'sessionStorage'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const store = new Map([['cos_gateway_url', 'https://fixture.invalid']]);
  Object.defineProperty(globalThis, 'sessionStorage', {configurable: true, value: {
    getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key),
    key: index => [...store.keys()][index], get length() { return store.size; },
  }});
  Object.defineProperty(globalThis, 'window', {configurable: true, value: {location: {origin: 'https://console.fixture.invalid'}, dispatchEvent() {}}});
  try {
    for (const domain of ['rivetworks', 'mossmornings']) {
      const subject = `usr_${domain}`, id = `ven_${domain}`;
      let deleted = false;
      client.clearVerifiedSession();
      store.set('cos_gateway_url', 'https://fixture.invalid');
      globalThis.fetch = async (url, init) => {
        const route = new URL(url).pathname;
        if (init?.method === 'DELETE') deleted = true;
        if (route.endsWith('/restore')) deleted = false;
        const data = route === '/auth/session' ? {authenticated: true, subject, csrf: 'fixture'}
          : {items: route.endsWith('/deleted') === deleted ? [id] : []};
        return new Response(JSON.stringify(data), {headers: {'Content-Type': 'application/json'}});
      };
      assert.equal((await client.ensureSession('unused', true)).ok, true);
      const paths = ['/api/ventures', '/api/ventures?summary=1', '/api/ventures/deleted', '/api/ventures/deleted?summary=1'];
      for (const path of paths) cache.cachePut(subject, path, {items: path.includes('/deleted') ? [] : [id]});
      for (const [suffix, method, inTrash] of [['', 'DELETE', true], ['/restore', 'POST', false]]) {
        assert.equal((await client.api(`/api/ventures/${id}${suffix}`, {method})).ok, true);
        for (const path of paths) {
          const result = await client.apiSwr(path);
          assert.deepEqual(result.data.items, path.includes('/deleted') === inTrash ? [id] : []);
        }
      }
    }
  } finally {
    client.clearVerifiedSession();
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  }
});
