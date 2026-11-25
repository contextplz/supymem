const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

const domains = ['inventory teams at distributors', 'families planning weekend walks'];
const flush = async () => { for (let turn = 0; turn < 40; turn++) await Promise.resolve(); };

function fixture(file, name, imports) {
  const slots = [], effects = [], timers = new Map(), listeners = new Map();
  let cursor = 0, now = 0, timerId = 0;
  const setTimeout = (callback, delay) => { timers.set(++timerId, { callback, at: now + delay }); return timerId; };
  const clearTimeout = id => timers.delete(id);
  const surface = prefix => ({
    addEventListener: (name, callback) => { const key = prefix + name; if (!listeners.has(key)) listeners.set(key, new Set()); listeners.get(key).add(callback); },
    removeEventListener: (name, callback) => listeners.get(prefix + name)?.delete(callback),
  });
  const document = { ...surface('document:'), hidden: false };
  const window = { ...surface('window:'), setTimeout, clearTimeout, clearInterval: clearTimeout };
  const React = {
    createContext: () => ({ Provider: 'provider' }),
    useState(initial) {
      const index = cursor++;
      slots[index] ||= { value: typeof initial === 'function' ? initial() : initial };
      return [slots[index].value, update => { slots[index].value = typeof update === 'function' ? update(slots[index].value) : update; }];
    },
    useRef(value) { const index = cursor++; return slots[index] ||= { current: value }; },
    useCallback(callback, deps) {
      const index = cursor++, saved = slots[index];
      if (!saved || deps.some((dep, position) => !Object.is(dep, saved.deps[position]))) slots[index] = { deps, callback };
      return slots[index].callback;
    },
    useMemo: callback => callback(),
    useEffect(callback, deps) {
      const index = cursor++, saved = slots[index];
      if (!saved || deps.some((dep, position) => !Object.is(dep, saved.deps[position]))) {
        saved?.cleanup?.();
        slots[index] = { deps };
        effects.push(() => { slots[index].cleanup = callback(); });
      }
    },
  };
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, { exports, AbortController, Date: { now: () => now }, setTimeout, clearTimeout,
    setInterval: () => 0, clearInterval() {}, window, document,
    sessionStorage: { removeItem() {} },
    require: id => {
      if (id === 'react') return React;
      if (id === 'react/jsx-runtime') return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
      assert.ok(id in imports, id); return imports[id];
    },
  });
  return {
    document, timers,
    render(...args) { cursor = 0; const result = exports[name](...args); effects.splice(0).forEach(effect => effect()); return result; },
    emit(name) { for (const callback of listeners.get(name) || []) callback(); },
    async advance(milliseconds) {
      const until = now + milliseconds;
      for (;;) {
        const next = [...timers].filter(([, timer]) => timer.at <= until).sort((left, right) => left[1].at - right[1].at)[0];
        if (!next) break;
        now = next[1].at; timers.delete(next[0]); next[1].callback(); await flush();
      }
      now = until; await flush();
    },
    stop() { slots.forEach(slot => slot?.cleanup?.()); },
    listenerCount() { return [...listeners.values()].reduce((count, callbacks) => count + callbacks.size, 0); },
  };
}

function activity(domain, onboarding = { ok: false, status: 404 }, local = false) {
  const streams = [], reads = [];
  let seq = 1, failing = false;
  const read = async path => {
    reads.push(path);
    if (failing) return { ok: false, status: 503 };
    return path.endsWith('/onboarding') ? onboarding : {
      ok: true, data: { items: [{ seq, kind: 'search', text: domain, status: 'done' }] },
    };
  };
  const harness = fixture(__dirname + '/ActivityProvider.tsx', 'ActivityProvider', {
    '@/lib/activityRefresh': require('../../lib/activityRefresh.ts'),
    '@/lib/localMode': { isLocalConsole: () => local },
    '@/lib/client': { GATEWAY: 'https://fixture.invalid', api: read, apiLive: read },
    '@/lib/nativeTransport': { createAuthenticatedEventSource: () => {
      const handlers = new Map();
      const stream = { readyState: 0, close() { this.readyState = 2; },
        addEventListener: (name, callback) => handlers.set(name, callback),
        fire(name, data) { if (name === 'open') this.readyState = 1; handlers.get(name)?.({ data: JSON.stringify(data) }); },
      };
      streams.push(stream); return stream;
    } },
    '@/components/primitives/ErrorNotice': { ErrorNotice: 'error' },
  });
  const render = () => harness.render({ ventureId: 'ven_saved', children: domain });
  return { ...harness, streams, reads, render, sequence: value => { seq = value; }, failing: value => { failing = value; } };
}

test('local activity reads stop on stream open and resume at 30 seconds after disconnect', async context => {
  const counts = [];
  for (const domain of domains) {
    const page = activity(domain, undefined, true);
    const activityReads = () => page.reads.filter(path => path.includes('/activity?')).length;
    const feed = page.render().props.children[1].props.value;
    const changes = [];
    feed.subscribe(change => changes.push(change));
    await flush();
    page.streams[0].fire('open');
    const initial = activityReads();
    await page.advance(20_000);
    const connected = activityReads() - initial;
    const line = {seq: 1, kind: 'step', status: 'done', text: domain};
    page.streams[0].fire('activity', line);
    page.streams[0].fire('activity', line);
    page.streams[0].onerror();
    assert.deepEqual(changes, ['connection', 'change', 'connection']);
    const disconnected = activityReads();
    await page.advance(29_999);
    const beforeSafety = activityReads() - disconnected;
    await page.advance(1);
    counts.push([connected, beforeSafety, activityReads() - disconnected]);
    page.stop();
  }
  context.diagnostic(`interval activity GETs [live 20s, down before 30s, down at 30s]: ${JSON.stringify(counts)}`);
  assert.deepEqual(counts, domains.map(() => [0, 0, 1]));
});

test('an idle local venture keeps activity updates without repeatedly polling empty onboarding across domains', async () => {
  for (const domain of domains) {
    const page = activity(domain, {ok: true, data: {status: 'idle', steps: [], current_step: null}});
    page.render(); await flush();
    page.streams[0].fire('phase', {status: 'running', phase: domain, phases_done: 1});
    assert.equal(page.render().props.value.run.phase, domain);
    assert.equal(await page.render().props.value.refreshOnboarding(), null);
    const reads = page.reads.filter(path => path.endsWith('/onboarding')).length;
    await page.advance(17000);
    const {run, onboarding} = page.render().props.value;
    assert.equal(onboarding, null);
    assert.deepEqual({...run}, {status: 'idle'});
    assert.equal(page.reads.filter(path => path.endsWith('/onboarding')).length, reads);
    assert.ok(page.reads.some(path => path.includes('/activity')));
    page.stop();
  }
});

test('refused streams back off without hot activity polling across domains', async () => {
  for (const domain of domains) {
    const page = activity(domain);
    page.render(); await flush();
    let refused;
    for (let second = 0; second < 60; second++) {
      if (refused !== page.streams.at(-1)) {
        refused = page.streams.at(-1);
        refused.onerror();
      }
      await page.advance(1000);
    }
    assert.ok(page.reads.filter(path => path.includes('/activity?')).length <= 5);
    page.stop(); await flush();
    assert.equal(page.timers.size, 0);
  }
});

test('streams reconnect past the former cutoff, poll stalled subscriptions, and resume after backgrounding across domains', async () => {
  for (const domain of domains) {
    const page = activity(domain);
    page.render(); await flush();
    page.streams[0].fire('open');
    page.sequence(2);
    await page.advance(17000);
    assert.equal(page.render().props.value.lines.at(-1).seq, 2, 'An open but stalled stream still receives saved updates');
    for (const wait of [1400, 2400, 4400, 8400]) {
      page.streams.at(-1).onerror(); await page.advance(wait);
    }
    assert.ok(page.streams.length >= 5, 'Fallback polling must not permanently replace reconnection');
    const retired = page.streams.at(-1);
    page.document.hidden = true; page.emit('document:visibilitychange');
    page.sequence(3); page.document.hidden = false; page.emit('window:focus'); await flush();
    retired.fire('activity', { seq: 99, text: 'retired', status: 'done' });
    const value = page.render().props.value;
    assert.equal(value.lines.at(-1).seq, 3);
    assert.equal(value.lines.at(-1).text, domain);
    assert.ok(page.reads.some(path => path.includes('after_seq=2')));
    page.stop(); await flush();
    assert.equal(page.timers.size, 0);
    assert.equal(page.listenerCount(), 0);
  }
});

test('only persistent update failures show a plain Try again action and recovery clears it across domains', async () => {
  for (const domain of domains) {
    const page = activity(domain); page.failing(true);
    page.render(); await flush();
    assert.equal(page.render().props.children[0].props.error, '');
    await page.advance(3000);
    assert.equal(page.render().props.children[0].props.error, '');
    await page.advance(6000);
    const notice = page.render().props.children[0];
    assert.match(notice.props.error, /You.*saved work/);
    const button = notice.props.children.find(child => child?.type === 'button');
    assert.equal(button.props.children, 'Try again');
    page.failing(false); button.props.onClick(); await flush();
    assert.equal(page.render().props.children[0].props.error, '');
    page.stop(); await flush(); assert.equal(page.timers.size, 0);
  }
});

test('saved work retries and resumes without duplicate completion or restarting work across domains', async () => {
  const durable = await import('../../lib/durableJobs.ts');
  for (const [index, domain] of domains.entries()) {
    let failing = true, complete = false;
    const calls = [], finished = [];
    const id = `job_saved_${index}`;
    const page = fixture(__dirname + '/../../hooks/useDurableJob.ts', 'useDurableJob', {
      '@/lib/client': { apiLive: async path => {
        calls.push(path);
        return failing ? { ok: false, status: 503 } : { ok: true, data: {
          id, kind: index ? 'idea.validate' : 'plan.compile', status: complete ? 'succeeded' : 'running', progress: { domain },
        } };
      } },
      '@/lib/durableJobs': durable,
    });
    const render = () => page.render(id, job => finished.push(job));
    render(); await flush();
    assert.match(render().error, /You.*Try again/);
    failing = false; render().retry(); await flush();
    assert.equal(render().error, ''); assert.equal(render().job.progress.domain, domain);
    complete = true; page.document.hidden = true;
    await page.advance(3000); assert.equal(finished.length, 1);
    page.document.hidden = false; page.emit('document:visibilitychange'); page.emit('window:online'); page.emit('window:focus');
    await flush(); assert.equal(finished.length, 1);
    assert.ok(calls.every(path => path === `/api/jobs/${id}`));
    page.stop(); assert.equal(page.timers.size, 0); assert.equal(page.listenerCount(), 0);
  }
});

test('work failure copy keeps saved answers without billing or reconciliation language across domains', async () => {
  const { jobFailure } = await import('../../lib/durableJobs.ts');
  for (const [index, domain] of domains.entries()) {
    for (const error_code of ['job_outcome_uncertain', 'clarification_invalid_output', 'clarification_outcome_uncertain',
      'plan_outcome_uncertain', 'research_invalid_output', 'plan_invalid_output', 'clarification_insufficient_credits']) {
      const text = jobFailure({ id: `job_${index}`, kind: 'idea.validate', status: 'failed', error_code, progress: { domain } });
      assert.doesNotMatch(text, /charg|reconcil|credit|usage|—/i);
      assert.match(text, /your|you/i);
    }
  }
});
