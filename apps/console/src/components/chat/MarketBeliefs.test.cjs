const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');

function harness(responder, local = false) {
  const states = [], effects = [], dependencies = [], requests = [], cleanups = [];
  const listeners = new Map();
  let epoch = 1;
  global.window = { __SUPYMEM__: {mode: local ? 'local' : 'hosted'}, addEventListener: (name, listener) => listeners.set(name, listener), removeEventListener: name => listeners.delete(name) };
  let cursor = 0;
  const jsx = (type, props) => typeof type === 'function' ? type(props) : { type, props: props || {} };
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in states)) states[index] = initial;
      return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }];
    },
    useEffect(effect, deps) {
      const index = cursor++;
      if (!dependencies[index] || deps.some((value, offset) => value !== dependencies[index][offset])) {
        dependencies[index] = deps;
        effects.push(() => { cleanups[index]?.(); cleanups[index] = effect(); });
      }
    },
  };
  const mocks = { react, 'react/jsx-runtime': { jsx, jsxs: jsx },
    '@/lib/client': { ensureSession: async () => { requests.push('session'); }, api: async url => {
      requests.push(url); return responder(url);
    }, verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => epoch },
    '@/lib/apiFailure': require('../../lib/apiFailure.ts'),
    '@/lib/credits': { formatMicroCreditUsage: micro => `${micro / 10000} credits` },
    '@/lib/chatPrefill': { prefillChat: text => requests.push(text) },
    '@/components/primitives/ErrorNotice': { ErrorNotice: ({ error }) => error ? jsx('div', { role: 'alert', children: error }) : null },
  };
  const filename = path.join(__dirname, 'MarketBeliefs.tsx');
  const loaded = new Module(filename, module);
  loaded.filename = filename; loaded.paths = module.paths;
  loaded.require = name => mocks[name] || require(name);
  loaded._compile(ts.transpileModule(readFileSync(filename, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, filename);
  return { requests, effects, switchOwner() { epoch++; listeners.get('supymem-session-changed')?.(); }, list: loaded.exports.BeliefList, render(props) {
    cursor = 0; return loaded.exports.MarketBeliefs(props);
  } };
}
function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(nodes);
  if (!node || typeof node !== 'object') return [];
  return [node, ...nodes(node.props?.children)];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join(' ');
  if (!node || typeof node === 'boolean') return '';
  return typeof node === 'object' ? text(node.props?.children) : String(node);
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const domains = [['ven_supply', 'Your logistics coordinators need reliable stock alerts.'],
                 ['ven_fitness', 'Your new runners want a consistent training routine.']];
function saved(ventureId, statement) {
  return { beliefs: ['who', 'pain', 'price', 'channel', 'message', 'competitor_gap', 'switching'].map((type, index) => ({
    id: `${ventureId}_${type}`, type, statement: `${statement} ${type}`, status: ['to_test', 'testing', 'confirmed', 'weak', 'disproven'][index % 5],
    evidence: index ? [{ experiment_id: `${ventureId}_test_${index}`, metric: 'reply_rate', value: 0.2, events: 20,
      threshold: { op: '>=', value: 0.1, min_events: 20 }, date: '2026-10-27T00:00:00Z' }] : [],
  })), plan: { id: `${ventureId}_plan`, days: 7, state: 'active', ceiling_micro: 50000, reserved_micro: 10000, max_emails_per_day: 15,
    tests: [{ experiment_id: `${ventureId}_test`, hypothesis: statement, state: 'running', deadline: '2026-11-03T00:00:00Z', started_at: '2026-10-27T00:00:00Z' }] } };
}

test('unrelated market maps render every type, accessible statuses, measured evidence and a timeline', () => {
  for (const [ventureId, statement] of domains) {
    const view = harness(), data = saved(ventureId, statement);
    const tree = view.list({ data, timeline: true }), content = text(tree);
    assert.equal(nodes(tree).filter(node => node.type === 'h3').length, 8);
    for (const status of ['To test', 'Testing', 'Confirmed', 'Weak', 'Disproven']) {
      assert.ok(nodes(tree).some(node => node.props['aria-label'] === `Status: ${status}`));
    }
    assert.match(content, /Experiment timeline.*15\s+emails\/day/);
    assert.doesNotMatch(content, /\d+ credits/);
    assert.match(content, /reply rate.*20\s+events.*Threshold.*2026-11-03/s);
    assert.ok(content.includes(statement));
    assert.doesNotMatch(content, /no evidence|\u2014/i);
    assert.ok(!text(view.list({ data })).includes('Threshold'));
    assert.ok(!data.beliefs[0].evidence.length);
  }
});

test('the experiments view uses plain market ideas without turning research into demand or showing internal plan details', () => {
  for (const [ventureId, statement] of domains) {
    const data = saved(ventureId, statement);
    data.plan.tests[0].error = 'worker receipt hold failed';
    const tree = harness().list({ data, plainWords: true, timeline: true });
    assert.match(text(tree), /Your market ideas/);
    assert.match(text(tree), /Your earlier tests/);
    assert.ok(text(tree).includes(statement));
    assert.ok(nodes(tree).some(node => node.props['aria-label'] === 'Status: Idea'));
    assert.ok(nodes(tree).some(node => node.props['aria-label'] === 'Status: Too early to tell'));
    assert.match(text(tree), /Your test did not finish/);
    assert.doesNotMatch(text(tree), /Confirmed|Disproven|Threshold|credits|belief|hypothesis|worker|receipt|hold|\u2014/i);
  }
});

test('plain idea failures keep sign-in guidance and do not expose technical account errors', async () => {
  for (const [ventureId] of domains) {
    for (const status of [401, 402]) {
      const view = harness(async () => ({ ok: false, status }));
      view.render({ ventureId, plainWords: true }); view.effects.shift()(); await flush();
      const content = text(view.render({ ventureId, plainWords: true }));
      assert.match(content, status === 401 ? /Sign in again/ : /Your market ideas could not load/);
      assert.doesNotMatch(content, /credits|worker|receipt|hold|hypothesis|\u2014/i);
    }
  }
});

test('your local market errors offer your settings rather than hosted sign-in across domains', async () => {
  for (const [ventureId] of domains) {
    const view = harness(async () => ({ok: false, status: 401, data: null}), true);
    view.render({ventureId, plainWords: true}); view.effects.shift()(); await flush();
    const content = text(view.render({ventureId, plainWords: true}));
    assert.doesNotMatch(content, /Sign in/);
    assert.match(content, /owner session.*Restart your local app.*retry your request/);
    assert.deepEqual(view.requests, ['session', `/api/ventures/${ventureId}/market-map`]);
  }
});

test('belief cards authenticate, stay venture-scoped, refresh and surface failures without starting work', async () => {
  for (const [ventureId, statement] of domains) {
    let fail = false;
    const view = harness(async () => fail ? { ok: false } : { ok: true, data: saved(ventureId, statement) });
    view.render({ ventureId, timeline: true }); view.effects.shift()(); await flush();
    assert.deepEqual(view.requests.slice(0, 2), ['session', `/api/ventures/${ventureId}/market-map`]);
    let tree = view.render({ ventureId, timeline: true });
    assert.ok(text(tree).includes(statement));
    nodes(tree).find(node => node.props['data-usage-control'] === 'market_beliefs_prepare').props.onClick();
    assert.match(view.requests.at(-1), /Show my market beliefs/);
    fail = true;
    nodes(tree).find(node => node.type === 'button' && text(node) === 'Refresh your beliefs').props.onClick();
    view.render({ ventureId, timeline: true }); view.effects.shift()(); await flush();
    tree = view.render({ ventureId, timeline: true });
    assert.ok(nodes(tree).some(node => node.props.role === 'alert'));
    assert.ok(view.requests.filter(request => request.startsWith('/api/')).every(request => request.endsWith('/market-map')));
  }
});

test('late responses cannot replace a different venture and sidebar, guide and chat mount the market map', async () => {
  let resolveFirst;
  const first = new Promise(resolve => { resolveFirst = resolve; });
  const view = harness(url => url.includes(domains[0][0]) ? first : Promise.resolve({ ok: true, data: saved(...domains[1]) }));
  view.render({ ventureId: domains[0][0] }); view.effects.shift()(); await flush();
  view.render({ ventureId: domains[1][0] }); view.effects.shift()(); await flush();
  resolveFirst({ ok: true, data: saved(...domains[0]) }); await flush();
  const content = text(view.render({ ventureId: domains[1][0] }));
  assert.ok(content.includes(domains[1][1]) && !content.includes(domains[0][1]));
  view.switchOwner();
  const changed = text(view.render({ ventureId: domains[1][0] }));
  assert.match(changed, /Your session changed/);
  assert.ok(!changed.includes(domains[1][1]));
  assert.match(readFileSync(path.join(__dirname, '../layout/SideNav.tsx'), 'utf8'), /market-map.*Market map/);
  assert.match(readFileSync(path.join(__dirname, '../../lib/capabilityGuide.ts'), 'utf8'), /id: "market-beliefs".*destination: "market-map"/);
  assert.match(readFileSync(path.join(__dirname, 'ChatDock.tsx'), 'utf8'), /m.kind === "market_beliefs".*MarketBeliefs/);
});
