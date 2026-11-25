const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

const domains = [
  ['ven_inventory', 'You could help wholesalers spot missing stock'],
  ['ven_garden', 'You could help balcony gardeners grow herbs'],
];
const now = Date.parse('2026-10-27T12:00:00Z');
const flush = () => new Promise(resolve => setImmediate(resolve));
const ok = data => ({ ok: true, status: 200, data });
function rounds(ventureId, statement, state = 'pending', label = 'Idea') {
  return { state, rounds: [{ id: `${ventureId}_round`, number: 1, state, status: label,
    created_at: '2026-10-27T08:00:00Z', ended_at: null, selected_option_id: null,
    options: [0, 1, 2].map(index => ({ id: `${ventureId}_option_${index}`, statement: `${statement} ${index + 1}`, state, status: label })) }] };
}
function action(ventureId, sequence = 1, extra = {}) {
  return { seq: sequence, text: 'Searched public sources', detail: { venture_id: ventureId, option_id: `${ventureId}_option_0`,
    evidence_level: 'desk_research', started_at: '2026-10-27T09:00:00Z', ended_at: '2026-10-27T10:00:00Z', outcome: 'succeeded', ...extra } };
}
function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(nodes);
  return node?.props ? [node, ...nodes(node.props.children)] : [];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join(' ');
  return node?.props ? text(node.props.children) : !node || typeof node === 'boolean' ? '' : String(node);
}
function button(tree, label) { return nodes(tree).find(node => node.type === 'button' && text(node) === label); }
function harness(responder) {
  const states = [], effects = [], dependencies = [], cleanups = [], requests = [], prompts = [], actions = [], timers = new Map(), listeners = new Map();
  let cursor = 0, timerId = 0, actor = 'owner', epoch = 1;
  const jsx = (type, props) => typeof type === 'function' ? type(props) : ({ type, props: props || {} });
  const mocks = {
    react: {
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
    },
    'react/jsx-runtime': { jsx, jsxs: jsx },
    '@/lib/client': {
      ensureSession: async () => { requests.push('session'); },
      verifiedSessionSubject: () => actor, verifiedSessionEpoch: () => epoch,
      api: async url => { requests.push(url); return responder(url); },
    },
    '@/lib/chatPrefill': { prefillChat: message => prompts.push(message) },
    '@/components/chat/ChatLink': { ChatAnchor: props => jsx('a', props) },
    '@/components/chat/MarketBeliefs': { MarketBeliefs: props => jsx('market-map', props) },
    '@/components/primitives/ErrorNotice': { ErrorNotice: props => props.error ? jsx('p', { role: 'alert', children: props.error }) : null },
    './ExperimentFeed': { ExperimentFeed: props => jsx('experiment-feed', props) },
    './ExperimentsView.module.css': { default: new Proxy({}, { get: (_, name) => name }) },
  };
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/ExperimentsView.tsx`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, {
    exports, require: name => mocks[name] || require(name), URL, Date,
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    window: { addEventListener: (name, listener) => listeners.set(name, listener), removeEventListener: name => listeners.delete(name),
      dispatchEvent(event) { assert.equal(event.type, 'supymem:ask'); actions.push(event.detail); } },
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; }, clearTimeout: identifier => timers.delete(identifier),
  });
  return { requests, prompts, actions, timers, effects,
    render(ventureId) { cursor = 0; return exports.ExperimentsView({ ventureId }); },
    chart: exports.InvestigationsChart, priorities: exports.YourPriorities,
    async load(ventureId) { this.render(ventureId); effects.shift()(); await flush(); return this.render(ventureId); },
    async tick() { const [identifier, callback] = timers.entries().next().value; timers.delete(identifier); callback(); await flush(); },
    changeActor(value = 'other') { actor = value; epoch++; listeners.get('supymem-session-changed')?.(); },
    unmount() { cleanups.forEach(cleanup => cleanup?.()); },
  };
}
function server(data, activity = []) {
  return url => ok(url.includes('/experiments/rounds') ? { next_moves: { kind: 'next_moves', actions: [] }, ...data }
    : { items: activity, next_cursor: activity.length ? String(activity.at(-1).seq) : null });
}

test('loading stays neutral until the server confirms exploration is enabled', () => {
  for (const [ventureId] of domains) {
    const view = harness(() => new Promise(() => {})), tree = view.render(ventureId);
    assert.match(text(tree), /Loading your market map/);
    assert.ok(nodes(tree).some(node => node.props.role === 'status'));
    assert.ok(!nodes(tree).some(node => node.type === 'h1'));
    assert.equal(view.requests.length, 0);
  }
});

test('the experiment feed only uses the current server payload and the existing preparation bridge in two domains', async () => {
  for (const [ventureId, statement] of domains) {
    const data = rounds(ventureId, statement), payload = { venture_id: ventureId, proposed_experiments: [{ id: `${ventureId}_test` }] };
    const view = harness(server(data));
    assert.ok(!nodes(await view.load(ventureId)).some(node => node.type === 'experiment-feed'));
    data.experiments_autopilot_v1 = payload;
    await view.tick();
    const feed = nodes(view.render(ventureId)).find(node => node.type === 'experiment-feed');
    assert.equal(feed.props.data, payload); assert.equal(feed.props.ventureId, ventureId);
    assert.deepEqual(view.actions, []);
    feed.props.onAction('experiment.prepare', { experiment_id: `${ventureId}_test` });
    assert.equal(view.actions[0].action, 'experiment.prepare'); assert.equal(view.actions[0].payload.experiment_id, `${ventureId}_test`);
    delete data.experiments_autopilot_v1;
    await view.tick(); assert.ok(!nodes(view.render(ventureId)).some(node => node.type === 'experiment-feed'));
    data.experiments_autopilot_v1 = payload;
    await view.tick();
    const staleFeed = nodes(view.render(ventureId)).find(node => node.type === 'experiment-feed');
    view.changeActor();
    staleFeed.props.onAction('experiment.prepare', { experiment_id: `${ventureId}_test` });
    assert.equal(view.actions.length, 1);
    assert.ok(!nodes(view.render(ventureId)).some(node => node.type === 'experiment-feed'));
    view.unmount();
  }
});

test('the default-off server flag preserves Market map without activity calls or polling', async () => {
  for (const [ventureId] of domains) {
    const view = harness(() => ({ ok: false, status: 404 })), tree = await view.load(ventureId);
    assert.equal(tree.type, 'market-map');
    assert.equal(tree.props.timeline, true);
    assert.equal(tree.props.ventureId, ventureId);
    assert.deepEqual(view.requests, ['session', `/api/ventures/${ventureId}/experiments/rounds`]);
    assert.equal(view.timers.size, 0);
  }
});

test('empty rounds execute exploration through the existing chat action bridge', async () => {
  for (const [ventureId] of domains) {
    const view = harness(server({ state: 'empty', rounds: [] })), tree = await view.load(ventureId);
    assert.match(text(tree), /No ideas to compare yet/);
    button(tree, 'Prepare your ideas').props.onClick();
    assert.equal(view.actions[0].action, 'experiment.exploration.next');
    assert.equal(view.prompts.length, 0);
    assert.ok(view.requests.every(url => url === 'session' || url.startsWith(`/api/ventures/${ventureId}/`)));
    assert.ok(!nodes(tree).some(node => node.type === 'figure'));
    assert.equal(nodes(tree).find(node => node.type === 'market-map').props.plainWords, true);
    view.unmount();
  }
});

for (const [state, label, expected] of [
  ['pending', 'Idea', /Testing has not started yet/],
  ['running', 'Testing', /You are testing this idea/],
  ['worked', 'Worked', /Your test is complete/],
  ['did_not_work', "Didn't work", /Your test is complete/],
  ['failed', 'Too early to tell', /Your test did not finish/],
  ['blocked_by_permission', 'Too early to tell', /connect a service or review its permissions/],
  ['insufficient_evidence', 'Too early to tell', /not have enough real responses/],
  ['too_early', 'Too early to tell', /not have enough real responses/],
]) {
  test(`${state} has plain status, option lanes and an actionable state`, async () => {
    for (const [ventureId, statement] of domains) {
      const data = rounds(ventureId, statement, state, label);
      if (state === 'worked') data.rounds[0].selected_option_id = `${ventureId}_option_0`;
      const view = harness(server(data)), tree = await view.load(ventureId);
      assert.match(text(tree), expected);
      assert.ok(text(tree).includes(statement));
      assert.equal(nodes(tree).filter(node => node.type === 'section' && node.props['aria-label']?.startsWith(statement)).length, 3);
      assert.equal(nodes(tree).filter(node => node.props['aria-label'] === `Status: ${label}`).length, 3);
      assert.doesNotMatch(text(tree), /\b(?:hypothesis|evidence level|job|runner|worker|lease|receipt|credits)\b|\u2014/i);
      if (state === 'worked') {
        assert.match(text(tree), /Your selected idea/);
        button(tree, 'Continue with your selected idea').props.onClick();
        assert.equal(view.actions[0].action, 'experiment.exploration.continue');
        assert.equal(view.actions[0].payload.round_id, data.rounds[0].id);
        assert.equal(view.actions[0].payload.option_id, data.rounds[0].selected_option_id);
      }
      if (state === 'failed') {
        button(tree, 'Retry your test').props.onClick(); button(tree, 'Try another way').props.onClick();
        assert.deepEqual(view.actions.map(item => item.action), ['experiment.exploration.retry', 'experiment.exploration.next']);
        assert.ok(view.actions.every(item => item.payload.round_id === data.rounds[0].id && item.payload.option_id === data.rounds[0].options[0].id));
        const lanes = nodes(tree).filter(node => node.type === 'section' && node.props['aria-label']?.startsWith(statement));
        assert.equal(lanes.flatMap(nodes).filter(node => node.props.role === 'alert').length, 3);
      }
      if (state === 'blocked_by_permission') {
        button(tree, 'Connect your email').props.onClick();
        assert.equal(view.actions[0].action, 'connections.connect');
        assert.equal(view.actions[0].payload.query, 'connect gmail');
      }
      if (['insufficient_evidence', 'too_early'].includes(state)) {
        button(tree, 'Extend your test').props.onClick();
        assert.equal(view.actions[0].action, 'experiment.exploration.next');
        assert.equal(view.actions[0].payload.option_id, data.rounds[0].options[0].id);
      }
      if (state === 'running') assert.ok(nodes(tree).some(node => node.props.role === 'status'));
      if (state === 'pending') {
        button(tree, 'Start all three').props.onClick();
        assert.equal(view.actions[0].action, 'experiment.exploration.start');
        assert.equal(view.actions[0].payload.round_id, data.rounds[0].id);
        assert.equal(view.actions[0].payload.option_id, undefined);
      }
      view.unmount();
    }
  });
}

test('completed research keeps uncertainty and offers a next test instead of claiming demand', async () => {
  for (const [ventureId, statement] of domains) {
    const view = harness(server(rounds(ventureId, statement, 'worked', 'Too early to tell'), [action(ventureId)]));
    const tree = await view.load(ventureId);
    assert.match(text(tree), /Researched/);
    assert.match(text(tree), /not enough real responses to decide/);
    assert.ok(button(tree, 'Extend your test'));
    assert.ok(!nodes(tree).some(node => node.props['aria-label'] === 'Status: Worked'));
    view.unmount();
  }
});

test('a missing search connection reviews the real setup and lets you retry, not connect unrelated email', async () => {
  for (const [ventureId, statement] of domains) {
    const data = rounds(ventureId, statement, 'blocked_by_permission', 'Too early to tell');
    data.rounds[0].actions = [{ action: 'connections.status' }];
    const view = harness(server(data)), tree = await view.load(ventureId);
    assert.equal(button(tree, 'Connect your email'), undefined);
    button(tree, 'Review your connections').props.onClick();
    button(tree, 'Try your test').props.onClick();
    assert.deepEqual(view.actions.map(item => item.action), ['connections.status', 'experiment.exploration.retry']);
    assert.equal(view.actions[1].payload.round_id, data.rounds[0].id);
    assert.equal(view.actions[1].payload.option_id, data.rounds[0].options[0].id);
    assert.equal(view.prompts.length, 0);
    view.unmount();
  }
});

test('load failures offer retry and another way without exposing an unconfirmed view', async () => {
  for (const [ventureId, statement] of domains) {
    let fail = true;
    const view = harness(url => fail ? { ok: false, status: 503 } : server(rounds(ventureId, statement))(url));
    let tree = await view.load(ventureId);
    assert.ok(nodes(tree).some(node => node.props.role === 'alert'));
    assert.ok(!nodes(tree).some(node => node.type === 'h1'));
    button(tree, 'Try in chat').props.onClick(); assert.equal(view.prompts.length, 1);
    fail = false; button(tree, 'Try again').props.onClick();
    tree = await view.load(ventureId);
    assert.ok(text(tree).includes(statement));
    view.unmount();
  }
});

test('activity failures retain your ideas and let you refresh the missing actions', async () => {
  for (const [ventureId, statement] of domains) {
    let fail = true;
    const view = harness(url => url.includes('/activity?') && fail ? { ok: false, status: 503 } : server(rounds(ventureId, statement), [action(ventureId)])(url));
    let tree = await view.load(ventureId);
    assert.ok(text(tree).includes(statement)); assert.match(text(tree), /recorded actions could not load/);
    fail = false; button(tree, 'Refresh your experiments').props.onClick(); tree = await view.load(ventureId);
    assert.match(text(tree), /Searched public sources/);
    assert.ok(!nodes(tree).some(node => node.props.role === 'alert'));
    view.unmount();
  }
});

test('actions that are still loading are not presented as an empty history', async () => {
  for (const [ventureId, statement] of domains) {
    let resolveActions;
    const pending = new Promise(resolve => { resolveActions = resolve; });
    const view = harness(url => url.includes('/activity?') ? pending : ok(rounds(ventureId, statement)));
    let tree = await view.load(ventureId);
    assert.match(text(tree), /Loading your recorded actions/);
    assert.doesNotMatch(text(tree), /No actions recorded yet/);
    resolveActions(ok({ items: [], next_cursor: null })); await flush(); tree = view.render(ventureId);
    assert.doesNotMatch(text(tree), /Loading your recorded actions/);
    assert.match(text(tree), /No actions recorded yet/); view.unmount();
  }
});

test('polling updates one event in place, follows activity pages and stays scoped to your option', async () => {
  for (const [ventureId, statement] of domains) {
    let calls = 0;
    const data = rounds(ventureId, statement, 'running', 'Testing');
    const view = harness(url => {
      if (url.includes('/experiments/rounds')) return ok(data);
      calls++;
      if (calls === 1) return ok({ items: Array.from({ length: 500 }, () => action(ventureId)), next_cursor: '500' });
      if (calls === 2) return ok({ items: [action('ven_other', 501), action(ventureId, 502, { option_id: 'unknown' })], next_cursor: '502' });
      return ok({ items: [action(ventureId, 503, { evidence_level: 'market_response' })], next_cursor: '503' });
    });
    let tree = await view.load(ventureId);
    assert.equal(nodes(tree).filter(node => node.type === 'p' && text(node) === 'Searched public sources').length, 1);
    assert.ok(view.requests.some(url => url.includes('after_seq=500')));
    data.rounds[0].options[0].state = 'worked'; data.rounds[0].options[0].status = 'Worked';
    await view.tick(); tree = view.render(ventureId);
    assert.ok(view.requests.at(-1).includes('after_seq=502'));
    assert.equal(nodes(tree).filter(node => node.type === 'p' && text(node) === 'Searched public sources').length, 2);
    assert.ok(nodes(tree).some(node => node.props['aria-label'] === 'Status: Worked'));
    assert.match(text(tree), /Heard from people/);
    view.unmount(); assert.equal(view.timers.size, 0);
  }
});

test('activity stages and source links remain honest, bounded and safe', async () => {
  for (const [ventureId, statement] of domains) {
    const events = ['desk_research', 'drafted_outreach', 'sent_outreach', 'market_response'].map((level, index) => action(ventureId, index + 1,
      { evidence_level: level, source_url: index === 0 ? 'https://example.org/research' : index === 1 ? 'javascript:alert(1)' : index === 2 ? 'https://secret@example.org' : null }));
    const view = harness(server(rounds(ventureId, statement), events)), tree = await view.load(ventureId);
    for (const label of ['Researched', 'Drafts only', 'Sent to people', 'Heard from people']) assert.ok(text(tree).includes(label));
    const links = nodes(tree).filter(node => node.type === 'a');
    assert.equal(links.length, 1); assert.equal(links[0].props.rel, 'noopener noreferrer');
    const logs = nodes(tree).filter(node => node.type === 'details' && node.props.className === 'log');
    assert.equal(logs[0].props.open, false);
    view.unmount();
  }
});

test('failed and empty actions never claim completed research or sending', async () => {
  for (const [ventureId, statement] of domains) {
    const events = [action(ventureId, 1, { outcome: 'failed', evidence_level: 'sent_outreach' }),
      action(ventureId, 2, { outcome: 'empty' })];
    events[0].text = 'Your email delivery was not confirmed'; events[1].text = 'No readable content at the source';
    const view = harness(server(rounds(ventureId, statement), events)), tree = await view.load(ventureId);
    assert.match(text(tree), /Did not finish/); assert.match(text(tree), /No results yet/);
    assert.ok(!nodes(tree).some(node => node.type === 'p' && ['Researched', 'Sent to people'].includes(text(node))));
    view.unmount();
  }
});

test('timeline uses real per-option intervals, keeps running spans current and counts each plain status', () => {
  for (const [ventureId, statement] of domains) {
    const data = rounds(ventureId, statement, 'running', 'Testing');
    data.rounds[0].ended_at = '2026-10-27T14:00:00Z';
    data.rounds[0].options[1].state = 'worked'; data.rounds[0].options[1].status = 'Worked';
    const view = harness(server(data));
    const tree = view.chart({ rounds: data.rounds, activity: [action(ventureId), action(ventureId, 2, { option_id: `${ventureId}_option_1` })], now });
    const bars = nodes(tree).filter(node => node.type === 'rect');
    assert.equal(bars.length, 2); assert.equal(bars[0].props.width, 392);
    assert.ok(Math.abs(bars[1].props.width - 392 / 3) < .01);
    assert.match(text(tree), /No recorded timing yet/);
    assert.ok(nodes(tree).some(node => node.props['aria-label'] === 'Testing: 2 of 3 ideas'));
    assert.ok(nodes(tree).some(node => node.type === 'svg' && /Still testing/.test(node.props['aria-label'])));
    const invalid = view.chart({ rounds: data.rounds, activity: [action(ventureId, 1, { started_at: 'not-a-date', ended_at: 'not-a-date' })], now });
    assert.equal(nodes(invalid).filter(node => node.type === 'svg').length, 0);
  }
});

test('session changes and late replies cannot reveal another account or venture', async () => {
  for (const [ventureId, statement] of domains) {
    let resolveLate;
    const view = harness(() => new Promise(resolve => { resolveLate = resolve; }));
    view.render(ventureId); view.effects.shift()(); await flush();
    assert.equal(view.requests[0], 'session');
    view.changeActor(); resolveLate(ok(rounds(ventureId, statement))); await flush();
    const tree = view.render(ventureId);
    assert.match(text(tree), /Your session changed/); assert.ok(!text(tree).includes(statement));
    assert.equal(view.timers.size, 0); view.unmount();
  }
  let resolveFirst;
  const first = new Promise(resolve => { resolveFirst = resolve; });
  const view = harness(url => url.includes(domains[0][0]) ? first : server(rounds(...domains[1]))(url));
  view.render(domains[0][0]); view.effects.shift()(); await flush();
  const tree = await view.load(domains[1][0]); resolveFirst(ok(rounds(...domains[0]))); await flush();
  assert.ok(text(tree).includes(domains[1][1])); assert.ok(!text(view.render(domains[1][0])).includes(domains[0][1]));
  view.changeActor(); assert.ok(!text(view.render(domains[1][0])).includes(domains[1][1])); view.unmount();
});

test('malformed responses fail closed and an unverified session never fetches your data', async () => {
  for (const [ventureId] of domains) {
    const view = harness(() => ok({ rounds: [{ id: 'broken', number: 1, options: null }] }));
    assert.ok(nodes(await view.load(ventureId)).some(node => node.props.role === 'alert'));
    assert.equal(view.requests.length, 2); view.unmount();
    const unsigned = harness(server({ state: 'empty', rounds: [] })); unsigned.changeActor(null);
    assert.ok(nodes(await unsigned.load(ventureId)).some(node => node.props.role === 'alert'));
    assert.deepEqual(unsigned.requests, ['session']); unsigned.unmount();
  }
});

test('round history and long logs collapse, with mobile stacking and desktop columns without chat overlays', async () => {
  for (const [ventureId, statement] of domains) {
    const data = rounds(ventureId, statement);
    data.rounds = [1, 2, 3].map(number => ({ ...data.rounds[0], id: `${ventureId}_round_${number}`, number }));
    const view = harness(server(data)), tree = await view.load(ventureId);
    const history = nodes(tree).filter(node => node.type === 'details' && node.props.className?.includes('round'));
    assert.deepEqual(history.map(node => node.props.open), [false, false, true]); view.unmount();
  }
  const css = readFileSync(`${__dirname}/ExperimentsView.module.css`, 'utf8');
  assert.match(css, /\.lanes\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /@media \(min-width: 1000px\)[\s\S]*repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(css, /max-height: 280px; overflow-y: auto/); assert.match(css, /focus-visible/);
  assert.doesNotMatch(css, /position:\s*(?:fixed|absolute)|animation:|prefers-color-scheme:\s*dark/);
});

test('Your priorities reuses saved moves and experiment actions, handles every state and stays read-only until tapped', async () => {
  for (const [ventureId, statement] of domains) {
    const data = rounds(ventureId, statement, 'running', 'Testing');
    data.next_moves = { kind: 'next_moves', actions: [
      { label: 'Find customers', action: 'gtm.find', payload: { prepare: true }, state_hash: 'saved-state' },
      { label: 'Review your drafts', action: 'outreach.draft', payload: { review_only: true } },
      { label: 'Build your landing page', action: 'pagebuilder.build', payload: { prepare: true } },
    ] };
    const finished = rounds(ventureId, `${statement} finished`, 'worked', 'Worked').rounds[0];
    finished.id += '_finished'; finished.options.forEach(option => { option.id += '_finished'; });
    finished.selected_option_id = finished.options[0].id;
    data.rounds.push(finished);
    const view = harness(server(data));
    const panel = nodes(await view.load(ventureId)).find(node => node.props['aria-labelledby'] === 'your-priorities');
    assert.match(text(panel), /Your priorities/);
    assert.match(text(panel), /Nothing starts until you choose an action/);
    assert.deepEqual(view.actions, []);
    assert.ok(view.requests.every(url => url === 'session' || url.includes('/experiments/rounds') || url.includes('/activity?')));
    assert.deepEqual(nodes(panel).filter(node => node.type === 'details').map(node => node.props.open), [false, false]);
    assert.equal(nodes(panel).find(node => node.props['aria-label'] === 'Your experiment options').props.tabIndex, 0);
    assert.match(text(panel), /Testing/); assert.match(text(panel), /Worked/); assert.match(text(panel), new RegExp(statement));
    button(panel, 'Find customers').props.onClick();
    assert.equal(view.actions[0].action, 'gtm.find'); assert.equal(view.actions[0].payload.prepare, true);
    button(panel, 'Continue with this idea').props.onClick();
    assert.equal(view.actions[1].action, 'experiment.exploration.continue');
    assert.equal(view.actions[1].payload.round_id, finished.id); assert.equal(view.actions[1].payload.option_id, finished.options[0].id);
    button(panel, 'Choose this idea').props.onClick();
    assert.equal(view.actions[2].action, 'experiment.exploration.select'); assert.equal(view.actions[2].payload.option_id, finished.options[1].id);
    let retries = 0;
    for (const missing of [null, undefined, { kind: 'next_moves', actions: [{ label: 'Broken' }] },
      { kind: 'next_moves', actions: [{ label: 'Broken', action: 'gtm.find', payload: [] }] },
      { kind: 'next_moves', actions: [{ label: 'Broken', action: 'gtm.find', disabled_reason: {} }] }]) {
      const failed = view.priorities({ data: { ...data, next_moves: missing }, retry: () => retries++ });
      assert.match(text(failed), /Your next moves could not load/); assert.match(text(failed), /finished/);
      button(failed, 'Reload your priorities').props.onClick(); button(failed, 'Ask for your next move').props.onClick();
      assert.equal(view.actions.at(-1).action, 'next_moves');
    }
    assert.equal(retries, 5);
    const empty = view.priorities({ data: { state: 'empty', rounds: [], next_moves: { kind: 'next_moves', actions: [] } }, retry: () => {} });
    assert.match(text(empty), /You have no next moves saved/); assert.match(text(empty), /You have no experiment options yet/);
    const small = view.priorities({ data: { ...data, next_moves: { kind: 'next_moves', actions: data.next_moves.actions.slice(0, 2) },
      rounds: [{ ...data.rounds[0], options: data.rounds[0].options.slice(0, 2) }] }, retry: () => {} });
    assert.deepEqual(nodes(small).filter(node => node.type === 'details').map(node => node.props.open), [true, true]);
    const disabled = view.priorities({ data: { ...data, next_moves: { kind: 'next_moves', actions: [
      { ...data.next_moves.actions[0], disabled_reason: 'Connect your account first.' },
    ] } }, retry: () => {} });
    assert.equal(button(disabled, 'Find customers').props.disabled, true); assert.match(text(disabled), /Connect your account first/);
    for (const [state, label, actionName, buttonLabel] of [
      ['pending', 'Idea', null, null], ['running', 'Testing', null, null],
      ['failed', 'Too early to tell', 'experiment.exploration.retry', 'Retry your test'],
      ['blocked_by_permission', 'Too early to tell', 'connections.connect', 'Connect your email'],
      ['insufficient_evidence', 'Too early to tell', 'experiment.exploration.next', 'Extend your test'],
      ['worked', 'Too early to tell', 'experiment.exploration.next', 'Extend your test'],
      ['did_not_work', "Didn't work", 'experiment.exploration.select', 'Choose this idea'],
    ]) {
      const statePanel = view.priorities({ data: { ...rounds(ventureId, statement, state, label), next_moves: data.next_moves }, retry: () => {} });
      assert.match(text(statePanel), new RegExp(label));
      assert.doesNotMatch(text(statePanel), /\b(?:hypothesis|evidence level|job|runner|worker|lease|receipt|credits)\b|\u2014/i);
      if (buttonLabel) { button(statePanel, buttonLabel).props.onClick(); assert.equal(view.actions.at(-1).action, actionName); }
    }
    const blocked = rounds(ventureId, statement, 'blocked_by_permission', 'Too early to tell');
    blocked.rounds[0].actions = [{ action: 'connections.status' }];
    const blockedPanel = view.priorities({ data: { ...blocked, next_moves: data.next_moves }, retry: () => {} });
    button(blockedPanel, 'Review your connections').props.onClick(); assert.equal(view.actions.at(-1).action, 'connections.status');
    blocked.rounds[0].actions = {};
    const malformed = view.priorities({ data: { ...blocked, next_moves: data.next_moves }, retry: () => {} });
    assert.ok(button(malformed, 'Connect your email'));
    view.changeActor(); assert.ok(!nodes(view.render(ventureId)).some(node => node.props['aria-labelledby'] === 'your-priorities'));
    view.unmount();
  }
  const css = readFileSync(`${__dirname}/ExperimentsView.module.css`, 'utf8');
  assert.match(css, /\.priorities ol, \.priorities ul \{ max-height: 280px; overflow-y: auto/);
});
