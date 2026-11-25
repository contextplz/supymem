const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

function load(file, imports = {}, globals = {}) {
  const exports = {};
  const code = ts.transpileModule(readFileSync(file, 'utf8'), { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  vm.runInNewContext(code, { exports, ...globals, require: name => {
    assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`);
    return imports[name];
  } });
  return exports;
}
const signIn = load(join(__dirname, '../../lib/signInReturn.ts'), {}, { URL });
const availability = load(join(__dirname, '../../lib/nativeAuthAvailability.ts'));
const chatStop = load(join(__dirname, '../../lib/chatStop.ts'), {}, { DOMException });
const settle = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  let clock = 0, cursor = 0, nextTimer = 0;
  let native = { state: 'authorizing', login_url: 'https://fixture.invalid/approve?challenge=A', persisted: false };
  let statusResult;
  const slots = [], effects = [], timers = new Map(), listeners = new Map(), cleared = [], calls = [], copies = [];
  const window = {
    setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, { fn, at: clock + delay }); return id; },
    clearTimeout(id) { cleared.push(id); timers.delete(id); },
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); },
  };
  const hooks = {
    useState(initial) { const i = cursor++; slots[i] ??= { value: initial };
      return [slots[i].value, value => { slots[i].value = typeof value === 'function' ? value(slots[i].value) : value; }]; },
    useEffect(fn, deps) { const i = cursor++, previous = slots[i];
      if (!previous || deps.some((value, j) => value !== previous.deps[j])) {
        slots[i] = { deps, cleanup: previous?.cleanup };
        effects.push(() => { slots[i].cleanup?.(); slots[i].cleanup = fn(); });
      }
    },
  };
  const jsx = (type, props) => typeof type === 'function' ? type(props) : { type, props };
  const { DesktopAccountConnection } = load(join(__dirname, 'DesktopAccountConnection.tsx'), {
    react: hooks, 'react/jsx-runtime': { jsx, jsxs: jsx },
    '@/lib/signInReturn': signIn, '@/lib/nativeAuthAvailability': availability,
    '@/lib/chatStop': chatStop,
    '@/components/primitives/ErrorNotice': { ErrorNotice: ({ error }) => error || null },
    '@/lib/desktop': { isDesktopShell: () => true, desktopListen: async () => () => {},
      invokeErrorMessage: (_, fallback) => fallback,
      desktopInvokeOrThrow: async command => {
        calls.push(command);
        if (command === 'desktop_auth_status' && statusResult !== undefined) {
          if (statusResult instanceof Error) throw statusResult;
          return statusResult;
        }
        if (command === 'desktop_auth_start') native = { ...native, login_url: 'https://fixture.invalid/approve?challenge=B' };
        else assert.equal(command, 'desktop_auth_status', 'presentation must not exchange an approval code');
        return { ...native };
      } },
  }, { window, URL, AbortController, navigator: { clipboard: { writeText: async value => copies.push(value) } }, Date: { now: () => clock }, document: {
    visibilityState: 'visible', addEventListener() {}, removeEventListener() {},
  } });
  const render = () => { cursor = 0; const tree = DesktopAccountConnection({});
    while (effects.length) effects.shift()(); return tree; };
  const view = () => { render(); return render(); };
  const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes)
    : tree && typeof tree === 'object' ? [tree, ...nodes(tree.props?.children)] : [tree];
  return { timers, cleared, calls, copies, view, focus: () => listeners.get('focus')?.(),
    setNative(value) { native = value; },
    setStatusResult(value) { statusResult = value; },
    text: () => nodes(view()).filter(node => typeof node === 'string').join(' '),
    inputs: () => nodes(view()).filter(node => node?.type === 'input').map(node => node.props.value),
    click(label) { const labels = Array.isArray(label) ? label : [label];
      const button = nodes(view()).find(node => node?.type === 'button' && labels.includes(node.props.children));
      assert.ok(button, label); assert.equal(Boolean(button.props.disabled), false); button.props.onClick(); },
    advance(ms) { const end = clock + ms;
      for (;;) { const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break; clock = next[1].at; timers.delete(next[0]); next[1].fn(); } clock = end; },
  };
}

test('slow, offline and old account reads offer a safe retry for both startups', async () => {
  for (const startupDomain of ['linear.app', 'alltrails.com']) for (const failure of ['slow', 'offline', 'old']) {
    const h = harness(); let finishRead;
    h.setStatusResult(failure === 'slow' ? new Promise(resolve => { finishRead = resolve; })
      : failure === 'offline' ? new Error('native_transport_unavailable') : { state: 'legacy_unknown' });
    h.view(); await settle();
    if (failure === 'slow') { h.advance(15_000); await settle(); }
    assert.match(h.text(), /Retry/, `${startupDomain}: ${failure}`);
    assert.doesNotMatch(h.text(), /native_transport_unavailable|legacy_unknown/);
    h.setStatusResult(undefined);
    h.setNative({ state: 'connected', subject: `usr_${startupDomain}`, persisted: true });
    h.click('Retry'); await settle();
    assert.match(h.text(), new RegExp(`Account:\\s+usr_${startupDomain.replaceAll('.', '\\.')}`));
    finishRead?.({ state: 'signed_out', persisted: false }); await settle();
    assert.match(h.text(), /Your account is connected/);
    assert.ok(h.calls.every(command => command === 'desktop_auth_status'));
    assert.equal(h.calls.length, 2);
  }
});

test('native sign-in shows only Supymem links and neutral provider errors', async () => {
  const h = harness(); h.view(); await settle(); h.view();
  const legacy = 'https://example-auth.auth.us-east-1.amazoncognito.com/oauth2/authorize';
  h.setNative({ state: 'authorizing', persisted: false, login_url: legacy, error: `Cognito failed at ${legacy}` });
  h.focus(); await settle();
  assert.ok(!h.inputs().includes(legacy));
  assert.doesNotMatch(h.text(), /Cognito|amazoncognito|us-east-1|prod/);
  assert.match(h.text(), /Supymem could not complete this connection/);
  h.click('Copy Supymem sign-in link'); await settle();
  assert.deepEqual(h.copies, []);
  const link = 'https://gateway.supymem.com/auth/device/approve?challenge=one-time';
  h.setNative({ state: 'authorizing', persisted: false, login_url: link });
  h.focus(); await settle();
  assert.ok(h.inputs().includes(link));
  h.click('Copy Supymem sign-in link'); await settle();
  assert.deepEqual(h.copies, [link]);
  assert.ok(h.calls.every(command => command === 'desktop_auth_status'), 'copying must preserve the current challenge');
});

test('a disconnected local helper shows accessible recovery without retiring the cloud account', async () => {
  const h = harness();
  h.setNative({ state: 'connected', subject: 'usr_fixture', persisted: true, bridge_connected: false });
  h.view(); await settle();
  assert.match(h.text(), /Mac tasks are unavailable/);
  assert.match(h.text(), /Cloud work still works/);
  assert.match(h.text(), /Quit and reopen Supymem/);
  assert.match(h.text(), /saved sign-in and work are retained/);
  assert.ok(h.calls.every(command => command === 'desktop_auth_status'));
});

for (const initialWait of [signIn.LINK_EXPIRES_MS, 30_000]) {
  test(`fresh native sign-in resets presentation after ${initialWait}ms without extending the same challenge`, async () => {
    const h = harness(); h.view(); await settle(); h.view();
    const oldTimers = [...h.timers.keys()];
    h.advance(initialWait);
    if (initialWait === signIn.LINK_EXPIRES_MS) assert.match(h.text(), /That sign-in link has expired/);
    h.click(['Restart browser sign-in', 'Start sign-in again']); await settle();
    assert.doesNotMatch(h.text(), /That sign-in link has expired/);
    assert.match(h.text(), /Waiting for your browser/);
    assert.ok(oldTimers.every(id => h.cleared.includes(id)), 'both retired timers must be cleared');
    assert.equal(Math.max(...[...h.timers.values()].map(timer => timer.at)), initialWait + signIn.LINK_EXPIRES_MS);
    h.advance(signIn.CODE_STEP_AFTER_MS); h.click('Supymem did not open? Use a code');
    assert.match(h.text(), /Paste the code from your browser/);
    h.advance(signIn.LINK_EXPIRES_MS - signIn.CODE_STEP_AFTER_MS - 1);
    assert.doesNotMatch(h.text(), /That sign-in link has expired/, 'the retired challenge must not expire the new one');
    const deadline = [...h.timers.entries()];
    h.focus(); await settle(); h.view();
    assert.deepEqual([...h.timers.entries()], deadline, 'rereading the same link must not extend its deadline');
    h.advance(1); assert.match(h.text(), /That sign-in link has expired/);
    assert.equal(h.calls.filter(command => command === 'desktop_auth_start').length, 1);
    assert.ok(h.calls.every(command => ['desktop_auth_status', 'desktop_auth_start'].includes(command)));
  });
}
