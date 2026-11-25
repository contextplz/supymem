const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const domains = [
  ['ven_stock', ['Stock alerts for warehouse teams', 'Stock reports for purchasing teams', 'Delivery alerts for dispatch teams']],
  ['ven_walk', ['Walking plans for new parents', 'Walking routes for retirees', 'Walking groups for students']],
];
const tick = () => new Promise(resolve => setImmediate(resolve));
const transpile = source => ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const jsx = (type, props) => typeof type === 'function' ? type(props) : { type, props: props || {} };
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
const button = (tree, label) => nodes(tree).find(node => node.type === 'button' && text(node) === label);
function round(venture, statements, state = 'pending') {
  return { id: `${venture}_round`, state, options: statements.map((statement, index) => ({ id: `${venture}_${index}`, statement, state })) };
}
function entry(option, index, action = 'search', start = '2026-10-27T10:00:00Z', end = '2026-10-27T10:00:10Z') {
  return { id: index, option_id: option.id, text: action === 'email' ? 'Emailed 3 people' : action === 'reply' ? '2 replied' : `Searched ${option.statement}`,
    action, outcome: 'succeeded', evidence_summary: action === 'email' ? '3' : action === 'reply' ? '2' : option.statement,
    evidence_level: action === 'email' ? 'sent_outreach' : action === 'reply' ? 'market_response' : 'desk_research',
    started_at: start, ended_at: end, source_url: 'https://example.org/research' };
}
function harness(responder = () => ({ ok: false, status: 404 })) {
  const state = [], effects = [], dependencies = [], cleanups = [], requests = [], listeners = new Map(), timers = new Map();
  let cursor = 0, epoch = 1, timerId = 0;
  const react = {
    useState(initial) { const index = cursor++; if (!(index in state)) state[index] = typeof initial === 'function' ? initial() : initial;
      return [state[index], value => { state[index] = typeof value === 'function' ? value(state[index]) : value; }]; },
    useRef(initial) { const index = cursor++; if (!(index in state)) state[index] = { current: initial }; return state[index]; },
    useEffect(effect, deps) { const index = cursor++; if (!dependencies[index] || deps.some((value, offset) => value !== dependencies[index][offset])) {
      dependencies[index] = deps; effects.push(() => { cleanups[index]?.(); cleanups[index] = effect(); }); } },
  };
  const loaded = { exports: {} }, loop = { exports: {} };
  vm.runInNewContext(transpile(readFileSync(path.join(__dirname, '../../lib/loopProgress.ts'), 'utf8')), { exports: loop.exports });
  vm.runInNewContext(transpile(readFileSync(path.join(__dirname, 'ExplorationCard.tsx'), 'utf8')), {
    exports: loaded.exports, URL, Date,
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; }, clearTimeout(id) { timers.delete(id); },
    window: { addEventListener(name, fn) { listeners.set(name, fn); }, removeEventListener(name) { listeners.delete(name); } },
    require(name) {
      if (name === 'react') return react;
      if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx };
      if (name === '@/lib/loopProgress') return loop.exports;
      if (name === '@/lib/client') return { ensureSession: async () => {}, verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => epoch,
        api: async url => { requests.push(url); return responder(url); } };
      if (name === '@/components/primitives/ErrorNotice') return { ErrorNotice: ({ error }) => error ? jsx('p', { role: 'alert', children: error }) : null };
      if (name === './ExperimentFeed') return { ExperimentFeed: props => jsx('experiment-feed', props) };
      if (name.endsWith('.css')) return { default: new Proxy({}, { get: (_, key) => key }) };
      throw new Error(name);
    },
  });
  return { ...loaded.exports, requests,
    render(props) { cursor = 0; return loaded.exports.ExplorationCard(props); },
    async flush() { effects.splice(0).forEach(effect => effect()); await tick(); await tick(); },
    async poll() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn()); await tick(); await tick(); },
    switchSession() { epoch++; listeners.get('supymem-session-changed')?.(); },
    cleanup() { cleanups.forEach(cleanup => cleanup?.()); },
  };
}

test('empty and loading states offer no invented options in unrelated domains', () => {
  for (const [venture, statements] of domains) {
    const view = harness();
    const empty = view.ExplorationCardView({ round: null, onRefresh() {}, onAction: async () => true });
    assert.match(text(empty), /Your ideas will appear here/);
    assert.ok(button(empty, 'Explore three ideas'));
    assert.equal(nodes(empty).filter(node => node.type === 'li').length, 0);
    const loading = view.ExplorationCardView({ round: null, loading: true, onRefresh() {} });
    assert.match(text(loading), /Loading your ideas/);
    assert.equal(loading.props['aria-busy'], 'true');
    assert.equal(button(loading, 'Start all three'), undefined);
    assert.ok(round(venture, statements).id.startsWith(venture));
  }
});

test('the chat experiment feed ignores saved-message data, clears on flag removal, and scopes preparation locks by experiment in two domains', async () => {
  for (const [ventureId, statements] of domains) {
    const data = { rounds: [round(ventureId, statements)] }, payload = { venture_id: ventureId };
    const view = harness(url => ({ ok: true, data: url.includes('/rounds') ? data : { items: [] } }));
    const calls = [], props = { ventureId, message: { id: 'message', round_id: data.rounds[0].id,
      experiments_autopilot_v1: payload }, onAction: async (name, actionPayload) => { calls.push([name, actionPayload]); return true; } };
    view.render(props); await view.flush();
    assert.ok(!nodes(view.render(props)).some(node => node.type === 'experiment-feed'));
    data.experiments_autopilot_v1 = payload;
    await view.poll();
    let feed = nodes(view.render(props)).find(node => node.type === 'experiment-feed');
    assert.equal(feed.props.data, payload); assert.equal(feed.props.ventureId, ventureId);
    assert.deepEqual(calls, []);
    await feed.props.onAction('experiment.prepare', { experiment_id: `${ventureId}_first` });
    feed = nodes(view.render(props)).find(node => node.type === 'experiment-feed');
    assert.ok(feed.props.completed.includes(`experiment.prepare:${ventureId}_first`));
    await feed.props.onAction('experiment.prepare', { experiment_id: `${ventureId}_first` });
    await feed.props.onAction('experiment.prepare', { experiment_id: `${ventureId}_second` });
    assert.equal(calls.length, 2); assert.ok(calls.every(([name]) => name === 'experiment.prepare'));
    delete data.experiments_autopilot_v1;
    await view.poll(); assert.ok(!nodes(view.render(props)).some(node => node.type === 'experiment-feed'));
    data.experiments_autopilot_v1 = payload;
    await view.poll(); view.switchSession();
    assert.equal(view.render(props), null);
    view.cleanup();
  }
});

test('pending state has one compact three-option card, collapsed logs and guarded start', async () => {
  for (const [venture, statements] of domains) {
    const view = harness(), data = round(venture, statements), calls = [];
    const props = { round: data, onRefresh() {}, onAction: async (name, payload) => { calls.push([name, payload]); return true; } };
    const tree = view.ExplorationCardView(props);
    assert.equal(nodes(tree).filter(node => node.props?.className === 'optionLine').length, 3);
    assert.equal(nodes(tree).filter(node => node.type === 'details').length, 3);
    assert.ok(nodes(tree).filter(node => node.type === 'details').every(node => !node.props.open));
    for (const statement of statements) assert.ok(text(tree).includes(statement));
    await button(tree, 'Start all three').props.onClick();
    assert.equal(calls[0][0], 'experiment.exploration.start');
    assert.equal(calls[0][1].round_id, data.id);
    assert.equal(button(view.ExplorationCardView({ ...props, busy: true }), 'Start all three').props.disabled, true);
    assert.equal(button(view.ExplorationCardView({ ...props, onAction: undefined }), 'Start all three').props.disabled, true);
    assert.doesNotMatch(text(tree), /\b(?:job|runner|worker|hypothesis|evidence level)\b|\u2014/i);
  }
});

test('running progress uses actual activity starts and only strictly overlapping option intervals', () => {
  for (const [venture, statements] of domains) {
    const view = harness(), data = round(venture, statements, 'running'), now = Date.parse('2026-10-27T10:01:00Z');
    const first = entry(data.options[0], 1), second = entry(data.options[1], 2, 'search', '2026-10-27T10:00:10Z', '2026-10-27T10:00:20Z');
    const props = { round: data, activity: [first, second], now, onRefresh() {} };
    let tree = view.ExplorationCardView(props);
    assert.match(text(tree), /Testing/);
    assert.match(text(tree), /No overlapping actions recorded/);
    assert.equal(nodes(tree).filter(node => node.type === 'time')[0].props.dateTime, '2026-10-27T10:00:00.000Z');
    second.started_at = '2026-10-27T10:00:09Z';
    assert.match(text(view.ExplorationCardView(props)), /recorded actions overlapped/);
    first.ended_at = null; second.ended_at = null;
    assert.match(text(view.ExplorationCardView(props)), /running at the same time/);
    assert.doesNotMatch(text(view.ExplorationCardView({ ...props, activity: [first, { ...first, id: 3 }] })), /running at the same time/);
    assert.doesNotMatch(text(view.ExplorationCardView({ ...props, activity: [{ ...first, started_at: 'invalid' }, second] })), /running at the same time/);
    assert.doesNotMatch(text(view.ExplorationCardView({ ...props, activity: [] })), /running at the same time/);
    assert.match(text(view.ExplorationCardView({ ...props, activity: [] })), /activity has not arrived/);
    tree = view.ExplorationCardView({ ...props, activity: [{ ...first, ended_at: first.started_at }] });
    assert.ok(nodes(tree).some(node => node.type === 'time' && node.props.dateTime === '2026-10-27T10:00:00.000Z'));
  }
});

test('done comparison preserves supplied reasons, selection and next step without calling research a people test', async () => {
  for (const [venture, statements] of domains) {
    const view = harness(), data = round(venture, statements, 'worked'), calls = [];
    data.selected_option_id = data.options[0].id;
    data.comparison = { summary: 'You received replies for the first idea.', selected_reason: 'More people replied to this idea.',
      reasons: Object.fromEntries(data.options.map((option, index) => [option.id, `You recorded ${index + 1} replies for ${option.statement}.`])),
      next_step: 'You can review the next test before continuing.' };
    data.options[1].state = 'did_not_work';
    const activity = [entry(data.options[0], 1, 'email'), entry(data.options[0], 2, 'reply'), entry(data.options[1], 3, 'email'),
      entry(data.options[2], 4), { ...entry(data.options[2], 5, 'email'), outcome: 'failed' }];
    const props = { round: data, activity, onRefresh() {}, onAction: async (name, payload) => { calls.push([name, payload]); return true; } };
    const tree = view.ExplorationCardView(props), content = text(tree);
    assert.match(content, /Worked/); assert.match(content, /Didn't work/); assert.match(content, /Too early to tell/);
    assert.equal(nodes(tree).filter(node => node.props?.children === 'Tested with people').length, 2);
    assert.equal(nodes(tree).filter(node => node.props?.children === 'Researched').length, 1);
    assert.ok(content.includes(data.comparison.selected_reason)); assert.ok(content.includes(data.comparison.next_step));
    for (const reason of Object.values(data.comparison.reasons)) assert.ok(content.includes(reason));
    await button(tree, 'Continue with this idea').props.onClick();
    assert.equal(calls[0][0], 'experiment.exploration.continue'); assert.equal(calls[0][1].option_id, data.selected_option_id);
    assert.doesNotMatch(content, /too small to conclude/);
    const noReasons = view.ExplorationCardView({ ...props, round: { ...data, comparison: undefined } });
    assert.match(text(noReasons), /3 people emailed, 2 replies recorded/);
    assert.match(text(noReasons), /detailed comparison is not ready/);
  }
});

test('you can select a researched idea without claiming demand, then continue through the same action layer', async () => {
  for (const [venture, statements] of domains) {
    const view = harness(), data = round(venture, statements, 'insufficient_evidence'), calls = [];
    data.comparison = { summary: 'Your recorded research needs a next test.', next_step: { action: 'experiment.exploration.next_round' } };
    const tree = view.ExplorationCardView({ round: data, onRefresh() {}, onAction: async (name, payload) => { calls.push([name, payload]); return true; } });
    for (const option of data.options) {
      await button(tree, `Choose: ${option.statement}`).props.onClick();
      assert.equal(calls.at(-1)[0], 'experiment.exploration.select');
      assert.equal(calls.at(-1)[1].round_id, data.id);
      assert.equal(calls.at(-1)[1].option_id, option.id);
    }
    assert.doesNotMatch(text(tree), /\[object Object\]|Tested with people/);
  }
});

test('failed tests and failed reads offer retry and another way without raw diagnostics', async () => {
  for (const [venture, statements] of domains) {
    const view = harness(), data = round(venture, statements, 'failed'), calls = [];
    const props = { round: data, onRefresh() {}, onAction: async (name, payload) => { calls.push([name, payload]); return false; } };
    const tree = view.ExplorationCardView(props);
    assert.match(text(tree), /Your test did not finish/);
    assert.ok(button(tree, 'Do it another way'));
    await button(tree, 'Try again').props.onClick();
    assert.equal(calls[0][0], 'experiment.exploration.retry');
    assert.equal(calls[0][1].option_id, data.options[0].id);
    let refreshed = 0;
    const load = view.ExplorationCardView({ round: null, error: 'Your results could not load. Try again.', onRefresh() { refreshed++; } });
    button(load, 'Try again').props.onClick(); assert.equal(refreshed, 1);
    assert.ok(nodes(load).some(node => node.props?.role === 'alert'));
    assert.equal(button(load, 'Explore three ideas').props.disabled, true);
  }
});

test('missing search setup never asks you to connect unrelated email and can retry the same scoped option', async () => {
  for (const [venture, statements] of domains) {
    const view = harness(), data = round(venture, statements, 'blocked_by_permission'), calls = [];
    data.actions = [{ action: 'connections.status' }];
    const tree = view.ExplorationCardView({ round: data, onRefresh() {}, onAction: async (name, payload) => { calls.push([name, payload]); return true; } });
    assert.match(text(tree), /configured search connection/);
    assert.equal(button(tree, 'Connect your email'), undefined);
    await button(tree, 'Review your connections').props.onClick();
    await button(tree, 'Try your test').props.onClick();
    assert.deepEqual(calls.map(item => item[0]), ['connections.status', 'experiment.exploration.retry']);
    assert.equal(calls[1][1].round_id, data.id);
    assert.equal(calls[1][1].option_id, data.options[0].id);
  }
});

test('permission blocks connect through the existing chat action and never start outreach automatically', async () => {
  for (const [venture, statements] of domains) {
    const view = harness(), data = round(venture, statements, 'blocked_by_permission'), calls = [];
    const tree = view.ExplorationCardView({ round: data, onRefresh() {}, onAction: async (name, payload) => { calls.push([name, payload]); return true; } });
    assert.match(text(tree), /connect your email before contacting anyone/);
    assert.equal(calls.length, 0); assert.equal(button(tree, 'Start all three'), undefined);
    await button(tree, 'Connect your email').props.onClick();
    assert.equal(calls[0][0], 'connections.connect'); assert.equal(calls[0][1].query, 'connect gmail');
    assert.doesNotMatch(text(tree), /Tested with people/);
  }
});

test('too-small and too-early states always offer a next step; negative results are not relabelled as too small', async () => {
  for (const [venture, statements] of domains) {
    const view = harness();
    for (const state of ['insufficient_evidence', 'too_early', 'did_not_work']) {
      const data = round(venture, statements, state), calls = [];
      const tree = view.ExplorationCardView({ round: data, activity: state === 'did_not_work' ? [entry(data.options[0], 1, 'email')] : [],
        onRefresh() {}, onAction: async (name, payload) => { calls.push([name, payload]); return true; } });
      assert.equal(button(tree, 'Continue with this idea'), undefined);
      if (state === 'did_not_work') { assert.match(text(tree), /tests didn't work/); assert.doesNotMatch(text(tree), /too small/); }
      else assert.match(text(tree), /too small to conclude/);
      await button(tree, 'Explore another three').props.onClick();
      assert.equal(calls[0][0], 'experiment.exploration.next'); assert.equal(calls[0][1].round_id, data.id);
    }
    const researchOnly = view.ExplorationCardView({ round: round(venture, statements, 'did_not_work'), onRefresh() {} });
    assert.match(text(researchOnly), /research has not shown a clear next step/);
    assert.doesNotMatch(text(researchOnly), /tests didn't work/);
  }
});

test('logs keep safe sources and plain words while omitting unsafe URLs and private details', () => {
  for (const [venture, statements] of domains) {
    const view = harness(), data = round(venture, statements), item = entry(data.options[0], 1);
    for (const source_url of ['javascript:alert(1)', 'data:text/html,secret', 'https://user:password@example.org', 'not a URL']) {
      const tree = view.ExplorationCardView({ round: data, activity: [{ ...item, source_url, text: 'worker checked hypothesis evidence level' }], onRefresh() {} });
      assert.equal(nodes(tree).filter(node => node.type === 'a').length, 0);
      assert.doesNotMatch(text(tree), /\bworker\b|\bhypothesis\b|evidence level/i);
    }
    const tree = view.ExplorationCardView({ round: data, activity: [item, { ...item, id: 2, text: 'token=private-value', source_url: null }], onRefresh() {} });
    const link = nodes(tree).find(node => node.type === 'a');
    assert.equal(link.props.rel, 'noopener noreferrer'); assert.equal(link.props.href, item.source_url);
    assert.doesNotMatch(text(tree), /private-value/);
    assert.match(text(tree), /private detail was removed/);
  }
});

test('default-off flag hides saved messages and never reads activity or starts actions', async () => {
  for (const [venture, statements] of domains) {
    const view = harness(), data = round(venture, statements), calls = [];
    const props = { ventureId: venture, message: { ...data, kind: 'exploration_options', role: 'assistant' }, onAction: async name => calls.push(name) };
    assert.equal(view.render(props), null); await view.flush(); assert.equal(view.render(props), null);
    assert.equal(view.requests.length, 1); assert.ok(view.requests[0].endsWith('/experiments/rounds')); assert.equal(calls.length, 0);
    view.cleanup();
  }
});

test('enabled empty rounds show loading while real activity is pending, then the empty state', async () => {
  for (const [venture, statements] of domains) {
    const data = round(venture, statements); let release;
    const view = harness(url => url.includes('/experiments/rounds') ? { ok: true, status: 200, data: { rounds: [] } }
      : new Promise(resolve => { release = resolve; }));
    const props = { ventureId: venture, message: { id: data.id, role: 'assistant' } };
    view.render(props); await view.flush(); assert.match(text(view.render(props)), /Loading your ideas/);
    release({ ok: true, status: 200, data: { items: [] } }); await tick();
    assert.match(text(view.render(props)), /Your ideas will appear here/); view.cleanup();
  }
});

test('live reads paginate, deduplicate and update the same card without crossing options or sessions', async () => {
  for (const [venture, statements] of domains) {
    const data = round(venture, statements, 'running'), observed = entry(data.options[0], 500);
    const page = Array.from({ length: 500 }, (_, index) => ({ seq: index + 1, text: observed.text,
      detail: { ...observed, option_id: index === 499 ? observed.option_id : 'unrelated_option' } }));
    let state = 'running';
    const view = harness(url => url.includes('/experiments/rounds')
      ? { ok: true, status: 200, data: { rounds: [{ ...data, state, options: data.options.map(option => ({ ...option, state })) }] } }
      : { ok: true, status: 200, data: { items: url.includes('after_seq=0&') ? page : [] } });
    const props = { ventureId: venture, message: { id: data.id, role: 'assistant', kind: 'exploration_options' } };
    view.render(props); await view.flush(); let tree = view.render(props);
    assert.match(text(tree), /Testing/); assert.ok(view.requests.some(url => url.includes('after_seq=500&')));
    assert.equal(nodes(tree).filter(node => node.type === 'a').length, 1);
    state = 'too_early'; await view.poll(); tree = view.render(props);
    assert.match(text(tree), /Too early to tell/); assert.equal(nodes(tree).filter(node => node.type === 'a').length, 1);
    view.switchSession(); assert.equal(view.render(props), null); view.cleanup();
  }
});

test('saved chat IDs resolve the exact round through preserved action payloads rather than the latest round', async () => {
  for (const [venture, statements] of domains) {
    const data = round(venture, statements), newer = round(`${venture}_newer`, statements.map(statement => `Another ${statement}`));
    const view = harness(url => ({ ok: true, status: 200, data: url.includes('/experiments/rounds') ? { rounds: [data, newer] } : { items: [] } }));
    const props = { ventureId: venture, message: { id: `${venture}_chat_message`, role: 'assistant', kind: 'exploration_options',
      actions: [{ label: 'Start all three', action: 'experiment.exploration.start', payload: { round_id: data.id } }] } };
    view.render(props); await view.flush(); const tree = view.render(props);
    for (const statement of statements) assert.ok(text(tree).includes(statement));
    assert.doesNotMatch(text(tree), /Another/); view.cleanup();
  }
});

test('late venture replies are discarded, malformed rounds never show options and activity failures preserve a retry', async () => {
  for (const [venture, statements] of domains) {
    const data = round(venture, statements); let resolve;
    const view = harness(() => new Promise(done => { resolve = done; }));
    const props = { ventureId: venture, message: { id: data.id, role: 'assistant' } };
    view.render(props); await view.flush(); view.render({ ...props, ventureId: `${venture}_other` });
    resolve({ ok: true, status: 200, data: { rounds: [data] } }); await tick();
    assert.equal(view.render({ ...props, ventureId: `${venture}_other` }), null); view.cleanup();
    for (const malformed of [{ ...data, options: data.options.slice(0, 2) }, { ...data, options: [data.options[0], data.options[0], data.options[1]] },
      { ...data, selected_option_id: 'unknown' }]) {
      const broken = harness(() => ({ ok: true, status: 200, data: { rounds: [malformed] } }));
      broken.render(props); await broken.flush(); const tree = broken.render(props);
      assert.match(text(tree), /Your results could not load/);
      assert.equal(nodes(tree).filter(node => node.type === 'li').length, 0);
      assert.ok(button(tree, 'Try again')); broken.cleanup();
    }
    const failure = harness(url => url.includes('/experiments/rounds') ? { ok: true, status: 200, data: { rounds: [data] } } : { ok: false, status: 503 });
    failure.render(props); await failure.flush();
    assert.match(text(failure.render(props)), /Your results could not load/);
    assert.ok(button(failure.render(props), 'Refresh your results')); failure.cleanup();
  }
});

test('start locks immediately, failures unlock retry and successful starts are not repeated or invented', async () => {
  for (const [venture, statements] of domains) {
    const data = round(venture, statements), calls = []; let release;
    const view = harness(url => ({ ok: true, status: 200, data: url.includes('/experiments/rounds') ? { rounds: [data] } : { items: [] } }));
    const props = { ventureId: venture, message: { id: data.id, role: 'assistant' },
      onAction: async (name, payload) => { calls.push([name, payload]); return new Promise(resolve => { release = resolve; }); } };
    view.render(props); await view.flush(); let tree = view.render(props);
    const start = button(tree, 'Start all three'); start.props.onClick(); start.props.onClick();
    assert.equal(calls.length, 1); assert.equal(button(view.render(props), 'Start all three').props.disabled, true);
    release(false); await tick(); tree = view.render(props);
    assert.match(text(tree), /That did not start/); assert.equal(button(tree, 'Start all three').props.disabled, false);
    assert.doesNotMatch(text(tree), /Testing|Tested with people/);
    button(tree, 'Start all three').props.onClick(); release(true); await tick(); tree = view.render(props);
    assert.equal(calls.length, 2); assert.equal(button(tree, 'Start all three').props.disabled, true);
    assert.doesNotMatch(text(tree), /Testing|Tested with people/); view.cleanup();
  }
});

test('a retry rearms only when recorded state or attempt changes, including a second failed attempt', async () => {
  for (const [venture, statements] of domains) {
    const data = round(venture, statements, 'failed'), calls = [];
    data.options[0].experiment_ids = ['attempt_one'];
    const view = harness(url => ({ ok: true, status: 200, data: url.includes('/experiments/rounds') ? { rounds: [data] } : { items: [] } }));
    const props = { ventureId: venture, message: { id: data.id, role: 'assistant' }, onAction: async name => { calls.push(name); return true; } };
    view.render(props); await view.flush(); view.render(props); await view.flush(); let tree = view.render(props);
    button(tree, 'Try again').props.onClick(); await tick(); tree = view.render(props);
    assert.equal(button(tree, 'Try again').props.disabled, true);
    data.options[0].experiment_ids = ['attempt_one', 'attempt_two'];
    await view.flush(); await view.poll(); view.render(props); await view.flush(); tree = view.render(props);
    assert.equal(button(tree, 'Try again').props.disabled, false);
    button(tree, 'Try again').props.onClick(); await tick(); assert.equal(calls.length, 2); view.cleanup();
  }
});

test('responsive light layout is bounded, stacked at 375px and three columns at 1280px with keyboard access', () => {
  const css = readFileSync(path.join(__dirname, 'ExplorationCard.module.css'), 'utf8');
  assert.match(css, /\.logs\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /@media \(min-width: 768px\)[^\n]*repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(css, /max-height: 520px; overflow: auto/); assert.match(css, /max-height: 220px; overflow: auto/);
  assert.match(css, /focus-visible/); assert.match(css, /prefers-reduced-motion/); assert.match(css, /background: #fff/);
  assert.doesNotMatch(css, /position:\s*(?:fixed|absolute)|prefers-color-scheme/);
});

test('all new exploration chat actions are in the existing feature catalog', () => {
  const catalog = readFileSync(path.resolve(__dirname, '../../../../../services/company_os/app/analytics/feature_catalog.py'), 'utf8');
  for (const action of ['start', 'retry', 'continue', 'next']) assert.ok(catalog.includes(`"experiment.exploration.${action}": "experiments"`));
});
