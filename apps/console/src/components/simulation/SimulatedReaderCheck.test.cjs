const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

// Execute the production component and its handlers; replace browser transport only.
function harness(enabled = true) {
  let actor = 'usr_a', epoch = 1, cursor = 0, effects = [], tree;
  let owner = { actorId: actor, workspaceId: 'w_a', ventureId: 'v_a' };
  let drafts = [{ doc_id: 'draft_a', revision: 'a'.repeat(64), subject: 'Saved draft' }];
  const hooks = [], calls = [], reports = [], listeners = new Map();
  const same = (a, b) => a && b && a.length === b.length && a.every((x, i) => Object.is(x, b[i]));
  const react = {
    useState(value) { const i = cursor++; hooks[i] ||= { value }; return [hooks[i].value, next => { hooks[i].value = next; }]; },
    useRef(value) { const i = cursor++; return hooks[i] ||= { current: value }; },
    useEffect(fn, deps) { const i = cursor++, old = hooks[i]; if (!old || !same(old.deps, deps)) effects.push(() => { old?.cleanup?.(); hooks[i] = { deps, cleanup: fn() }; }); },
  };
  const jsx = (type, props) => typeof type === 'function' ? type(props || {}) : ({ type, props: props || {} });
  react.createElement = (type, props, ...children) => jsx(type, { ...props, children: children.length === 1 ? children[0] : children });
  const window = { location: { href: 'https://console.test/v/v_a/gtm' },
    addEventListener(name, fn) { (listeners.get(name) || listeners.set(name, new Set()).get(name)).add(fn); },
    removeEventListener(name, fn) { listeners.get(name)?.delete(fn); },
    dispatchEvent(event) { reports.push(event.detail); return true; } };
  const userError = {}, notice = {};
  const compile = source => ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compile(fs.readFileSync(__dirname + '/../../lib/userError.ts', 'utf8')), {
    exports: userError, window, CustomEvent,
    require(id) { assert.equal(id, './uiDiagnostics.ts'); return { UI_DIAGNOSTIC_EVENT: 'supymem-ui-diagnostic' }; },
  }, { filename: 'userError.ts' });
  vm.runInNewContext(compile(fs.readFileSync(__dirname + '/../primitives/ErrorNotice.tsx', 'utf8')), {
    exports: notice, require(id) {
      if (id === 'react') return react;
      if (id === '@/lib/userError') return userError;
      if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => false};
      if (id === '@/lib/credits') return {USAGE_LIMIT_MESSAGE: 'You have reached your usage limit. Buy more in Settings, then retry.', BUY_MORE_HREF: '/account#account-buy'};
      throw new Error(id);
    },
  }, { filename: 'ErrorNotice.tsx' });
  const output = ts.transpileModule(fs.readFileSync(__dirname + '/SimulatedReaderCheck.tsx', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(output, { exports, window, AbortController, Date, Error, crypto: { randomUUID: () => 'click-id' },
    require(id) {
      if (id === 'react') return react;
      if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
      if (id === '@/components/primitives/ErrorNotice') return notice;
      if (id === '@/lib/apiFailure') return {apiFailure: () => 'Your saved check could not run. Refresh its status, then retry.'};
      if (id === '@/lib/client') return { verifiedSessionSubject: () => actor, verifiedSessionEpoch: () => epoch,
        api: (path, init) => new Promise((resolve, reject) => calls.push({ path, init, resolve, reject })) };
      throw new Error(id);
    } }, { filename: 'SimulatedReaderCheck.tsx' });
  function render() { cursor = 0; tree = exports.SimulatedReaderCheck({ owner, drafts, enabled }); const run = effects; effects = []; run.forEach(fn => fn()); return tree; }
  function nodes(node) { if (Array.isArray(node)) return node.flatMap(nodes); if (!node || typeof node !== 'object') return [node]; return [node, ...nodes(node.props.children)]; }
  function click(label) { render(); const button = nodes(tree).find(n => n?.type === 'button' && n.props.children === label); assert.ok(button, label); assert.equal(button.props.disabled, false); button.props.onClick(); }
  const flush = async () => { for (let n = 0; n < 4; n++) await Promise.resolve(); render(); };
  const quote = () => ({ workspace_id: owner.workspaceId, venture_id: owner.ventureId, actor_id: owner.actorId, input_digest: 'input-a',
    quote_digest: 'a'.repeat(64), epoch: 1, expires_at: Date.now() / 1000 + 300, resources: { ceiling_micro: 500, source: 'platform_default', model: 'Pinned model' } });
  render();
  return { calls, reports, render, click, flush, quote, text: () => nodes(tree).filter(n => typeof n === 'string').join(' '),
    changeVenture() { owner = { ...owner, ventureId: 'v_b' }; render(); },
    changeAccount() { actor = 'usr_b'; epoch++; owner = { ...owner, actorId: actor }; render(); },
    changeDraft() { drafts = [{ ...drafts[0], revision: 'b'.repeat(64) }]; render(); },
    navigate() { window.location.href = 'https://console.test/other'; },
    retire() { for (const fn of listeners.get('supymem-session-changed') || []) fn(); render(); },
  };
}

test('off by default, no automatic calls, two explicit decisions and fixed evidence labels', async () => {
  assert.equal(harness(false).render(), null);
  const h = harness();
  assert.equal(h.calls.length, 0);
  assert.match(h.text(), /Simulated · AI personas/);
  assert.match(h.text(), /Not customer evidence/);
  h.click('Run check');
  assert.match(h.calls[0].path, /\/quote\?/);
  assert.deepEqual(JSON.parse(h.calls[0].init.body), { drafts: [{ doc_id: 'draft_a', revision: 'a'.repeat(64) }] });
  h.calls[0].resolve({ ok: true, data: h.quote() }); await h.flush();
  assert.equal(h.calls.length, 2);
  assert.equal(JSON.parse(h.calls[1].init.body).confirmed, true);
  assert.equal(JSON.parse(h.calls[1].init.body).request_id, 'click-id');
});

for (const change of ['changeVenture', 'changeAccount', 'changeDraft', 'navigate', 'retire']) {
  test(`late quote/error is retired on ${change}`, async () => {
    const h = harness(); h.click('Run check'); const old = h.quote();
    h[change](); h.calls[0].resolve({ ok: true, data: old }); await h.flush();
    assert.doesNotMatch(h.text(), /Pinned model/);
    assert.equal(h.calls.length, 1);
  });
}

test('unknown start can only check the same saved intent, never creates a new paid request', async () => {
  const h = harness(); h.click('Run check'); h.calls[0].resolve({ ok: true, data: h.quote() }); await h.flush();
  const body = h.calls[1].init.body;
  h.calls[1].reject(new Error('Unconfirmed saved request')); await h.flush();
  h.click('Check saved run');
  assert.equal(h.calls[2].init.body, body);
});

test('delayed start error cannot leak into another account', async () => {
  const h = harness(); h.click('Run check'); h.calls[0].resolve({ ok: true, data: h.quote() }); await h.flush();
  h.changeAccount();
  h.calls[1].reject(new Error('Old account private error')); await h.flush();
  assert.doesNotMatch(h.text(), /Old account private error|Check saved run/);
  assert.equal(h.calls.length, 2);
});

test('malformed or negative estimate never offers credit confirmation', async () => {
  const h = harness(); h.click('Run check'); const q = h.quote(); q.resources.ceiling_micro = -1;
  h.calls[0].resolve({ ok: true, data: q }); await h.flush();
  assert.equal(h.calls.length, 1);
  assert.match(h.text(), /estimate could not be verified/);
});

test('current displayed failure uses the real shared notice and reporter once', async () => {
  const h = harness(); h.click('Run check');
  h.calls[0].reject(new Error('Current check failed for private@example.test'));
  await h.flush();
  assert.match(h.text(), /Current check failed/);
  assert.equal(h.reports.length, 1);
  assert.equal(h.reports[0].source, 'simulation.rehearse');
  assert.equal(h.reports[0].text, 'Current check failed for [email]');
  h.render(); h.render();
  assert.equal(h.reports.length, 1);
});

for (const change of ['changeVenture', 'changeAccount', 'changeDraft', 'navigate', 'retire']) {
  test(`late failure is neither displayed nor reported after ${change}`, async () => {
    const h = harness(); h.click('Run check'); h[change]();
    h.calls[0].reject(new Error('Retired private check failure'));
    await h.flush();
    assert.doesNotMatch(h.text(), /Retired private check failure/);
    assert.equal(h.reports.length, 0);
  });
  test(`already displayed failure retires without another report after ${change}`, async () => {
    const h = harness(); h.click('Run check');
    h.calls[0].reject(new Error('Current private check failure'));
    await h.flush(); assert.equal(h.reports.length, 1);
    h[change](); h.render();
    assert.doesNotMatch(h.text(), /Current private check failure/);
    assert.equal(h.reports.length, 1);
  });
}

test('quote restores durable saved run without a start request', async () => {
  const h = harness(); h.click('Run check');
  const quote = h.quote();
  quote.existing_job = { ...quote, job_id: 'job_saved', state: 'needs_attention', uncertain: true, credits_held: true, credits_micro: 0 };
  h.calls[0].resolve({ ok: true, data: quote }); await h.flush();
  assert.match(h.text(), /still being resolved/);
  assert.doesNotMatch(h.text(), /Run check/);
  h.click('Refresh saved check'); assert.match(h.calls[1].path, /\/job_saved\?/);
  assert.equal(h.calls[1].init.method, 'GET');
});
