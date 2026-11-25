const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const read = file => readFileSync(path.join(__dirname, file), 'utf8');
const compile = source => ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : !value || typeof value !== 'object' ? []
  : [value, ...nodes(value.props?.children)];
const text = value => Array.isArray(value) ? value.map(text).join('') : typeof value === 'string' || typeof value === 'number' ? String(value)
  : !value || typeof value !== 'object' ? '' : text(value.props?.children);
const button = (tree, label) => nodes(tree).find(node => node.type === 'button' && text(node).trim() === label);
const tick = () => new Promise(resolve => setImmediate(resolve));
const domains = [['ven_repairs', 'Arrange equipment inspections'], ['ven_cooking', 'Plan your family meals']];
const task = (ventureId, title, state = 'working') => ({ id: `tsk_${ventureId}`, title, goal: title, kind: 'one_time', state,
  updated_at: '2026-10-28T08:00:00Z', deadline_at: null, deadline_is_estimate: true, next_fire_at: null, blocker: null,
  action: { label: 'Review saved task', href: `/v/${ventureId}/tasks` }, events: [],
  steps: [{ id: 'done', title: `Read your plan for ${title}`, state: 'completed', deadline_at: null },
    { id: 'running', title: `Check your options for ${title}`, state: 'working', deadline_at: null },
    { id: 'waiting', title: `Review your next step for ${title}`, state: 'approval_required', deadline_at: null },
    { id: 'failed', title: `Prepare your result for ${title}`, state: 'failed', deadline_at: null }],
});
const message = saved => ({ id: `msg_${saved.id}`, role: 'assistant', kind: 'confirmation', actions: [{ label: 'Start your search',
  confirmation: { action: 'gtm.find', interpretation: saved.title, consequences: ['Only the reviewed search will run.'],
    resulting_state: { _goal_step: { task_id: saved.id, run_id: `run_${saved.id}`, step_id: 'waiting' }, query: saved.goal } } }] });

function harness(responder) {
  const state = [], dependencies = [], effects = [], cleanups = [], timers = new Map(), listeners = new Map(), requests = [], events = [];
  let cursor = 0, timerId = 0, epoch = 1;
  const jsx = (type, props) => ({ type, props });
  const react = {
    useState(initial) { const index = cursor++; if (!(index in state)) state[index] = typeof initial === 'function' ? initial() : initial;
      return [state[index], update => { state[index] = typeof update === 'function' ? update(state[index]) : update; }]; },
    useRef(initial) { const index = cursor++; return state[index] ??= { current: initial }; },
    useEffect(effect, deps) { const index = cursor++; if (!dependencies[index] || deps.some((value, offset) => value !== dependencies[index][offset])) {
      dependencies[index] = deps; effects.push(() => { cleanups[index]?.(); cleanups[index] = effect(); }); } },
  };
  const client = { ensureSession: async () => {}, verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => epoch,
    api: async (url, init) => { requests.push([url, init]); return responder(url, init); } };
  const policy = { exports: {} }, taskApi = { exports: {} }, loaded = { exports: {} };
  vm.runInNewContext(compile(read('chatActionPolicy.ts')), { exports: policy.exports });
  vm.runInNewContext(compile(read('../../lib/taskApi.ts')), { exports: taskApi.exports, URL, Intl, Date,
    require: () => client });
  vm.runInNewContext(compile(read('GoalCard.tsx')), { exports: loaded.exports, Set, Event: class { constructor(type) { this.type = type; } },
    setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; }, clearTimeout(id) { timers.delete(id); },
    window: { addEventListener(name, callback) { listeners.set(name, callback); }, removeEventListener(name) { listeners.delete(name); },
      dispatchEvent(event) { events.push(event.type); } },
    require(name) {
      if (name === 'react') return react;
      if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
      if (name === '@/lib/client') return client;
      if (name === '@/lib/taskApi') return taskApi.exports;
      if (name === '@/lib/runStatusPresentation') return { RUN_STATUS_REFRESH: 'run-status-refresh' };
      if (name === './chatActionPolicy') return policy.exports;
      if (name === './ChatLink') return { ChatAnchor: 'a' };
      if (name === '@/components/primitives/ErrorNotice') return { ErrorNotice: ({ error }) => error ? jsx('p', { role: 'alert', children: error }) : null };
      if (name.endsWith('.css')) return { default: new Proxy({}, { get: (_, key) => key }) };
      throw new Error(name);
    },
  });
  function expand(value) {
    if (Array.isArray(value)) return value.map(expand);
    if (!value || typeof value !== 'object') return value;
    if (typeof value.type === 'function') return expand(value.type(value.props));
    return { ...value, props: { ...value.props, children: expand(value.props?.children) } };
  }
  return { ...loaded.exports, requests, events,
    render(props) { cursor = 0; return expand(loaded.exports.GoalCard(props)); },
    async flush() { effects.splice(0).forEach(effect => effect()); await tick(); await tick(); },
    async poll() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(callback => callback()); await tick(); await tick(); },
    switchSession() { epoch++; listeners.get('supymem-session-changed')?.(); },
    cleanup() { cleanups.forEach(cleanup => cleanup?.()); },
  };
}

test('goal progress shows only for a true ui-config flag and never guesses a task from handoff text in two domains', async () => {
  for (const [ventureId, title] of domains) {
    for (const flag of [undefined, false, 1, 'true', true]) {
      const saved = task(ventureId, title), children = { type: 'button', props: { children: 'Exact action approval' } };
      const view = harness(url => ({ ok: true, data: url === '/api/ui-config'
        ? { founder_ui: 'chat', goals_v1: flag } : { items: [saved] } }));
      const props = { ventureId, message: { ...message(saved), goals_v1: true }, children };
      view.render(props); await view.flush();
      const tree = view.render(props);
      assert.equal(nodes(tree).some(node => node.type === 'section'), flag === true);
      assert.match(text(tree), /Exact action approval/);
      if (flag === true) {
        assert.match(text(tree), new RegExp(title));
        assert.ok(view.requests.some(([url]) => url === `/api/ventures/${ventureId}/handoffs`));
      } else assert.ok(view.requests.every(([url]) => url === '/api/ui-config'));
      view.cleanup();
    }
    for (const role of ['assistant', 'founder']) {
      const view = harness(() => { throw new Error('No request is allowed'); });
      const props = { ventureId, message: { id: 'handoff', role, kind: 'task_handoff', body_md: title,
        actions: [{ label: 'Open tasks', href: `/v/${ventureId}/tasks` }] } };
      view.render(props); await view.flush();
      assert.equal(nodes(view.render(props)).some(node => node.type === 'section'), false);
      assert.equal(view.requests.length, 0);
      view.cleanup();
    }
  }
});

test('saved checklist states, loading, empty and failed retry use plain copy and existing styles in two domains', async () => {
  for (const [ventureId, title] of domains) {
    const saved = task(ventureId, title), view = harness(() => ({ ok: true, data: { goals_v1: true } }));
    const props = { task: saved, ventureId, loading: false, error: '', busy: false, onStop() {}, onRefresh() {} };
    const tree = view.GoalCardView(props);
    assert.match(text(tree), new RegExp(title));
    assert.deepEqual(nodes(tree).filter(node => node.type === 'li').map(node => text(node).split(title).at(-1).trim()), ['Done', 'Running', 'Waiting for you', 'Failed']);
    assert.equal(nodes(tree).find(node => node.type === 'details').props.open, undefined);
    assert.ok(button(tree, 'Stop'));
    assert.equal(nodes(tree).find(node => node.type === 'a' && text(node) === 'Try again').props.href, `/v/${ventureId}/tasks`);
    assert.ok(nodes(tree).some(node => node.type === 'a' && text(node) === 'Do it another way'));
    const small = view.GoalCardView({ ...props, task: { ...saved, steps: saved.steps.slice(0, 2) } });
    assert.equal(nodes(small).find(node => node.type === 'details').props.open, true);
    const loading = view.GoalCardView({ ...props, task: null, loading: true });
    assert.equal(loading.props['aria-busy'], true); assert.match(text(loading), /Loading your progress/); assert.equal(button(loading, 'Stop'), undefined);
    assert.match(text(view.GoalCardView({ ...props, task: { ...saved, steps: [] } })), /Your steps will appear when your plan is ready/);
    for (const state of ['auth_required', 'input_required', 'canceled', 'failed']) {
      const result = view.GoalCardView({ ...props, task: { ...saved, state } });
      assert.equal(!!button(result, 'Stop'), !['canceled', 'failed'].includes(state));
      assert.doesNotMatch(text(result), /auth_required|input_required|canceled|reconciliation|receipt|worker|—/);
    }
    const foreignLink = view.GoalCardView({ ...props, task: { ...saved, action: { label: 'Retry', href: '/v/another-venture/tasks' } } });
    assert.ok(nodes(foreignLink).filter(node => node.type === 'a').every(node => node.props.href.includes(ventureId)));
    const css = read('GoalCard.module.css');
    assert.match(css, /max-height:[^;]+; overflow: auto/); assert.match(css, /:focus-visible/); assert.match(css, /prefers-reduced-motion/);
    assert.equal(button(tree, 'Stop').props.className, 'actionLink actionGhost');
  }
});

test('all exact confirmation cards stay batched in the original ChatDock flow without a blanket approval in two domains', async () => {
  const dock = ts.createSourceFile('ChatDock.tsx', read('ChatDock.tsx'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let wrapper;
  const visit = node => { if (ts.isJsxElement(node) && node.openingElement.tagName.getText(dock) === 'GoalCard') wrapper = node; ts.forEachChild(node, visit); };
  visit(dock);
  assert.ok(wrapper);
  assert.match(wrapper.getText(dock), /confirmations\.map/);
  assert.match(wrapper.getText(dock), /confirmation_message_id: m\.id/);
  assert.match(read('ChatDock.tsx'), /const actionPlan = planChatActions\(/);
  for (const [ventureId, title] of domains) {
    const saved = task(ventureId, title, 'approval_required');
    const children = Array.from({ length: 5 }, (_, index) => ({ type: 'confirmation', props: {
      interpretation: `${title} action ${index}`, consequences: [`Review effect ${index}`], resulting_state: { step_id: index },
      children: { type: 'button', props: { children: `Review action ${index}` } },
    } }));
    const view = harness(() => ({ ok: true, data: {} }));
    const tree = view.GoalCardView({ task: saved, ventureId, loading: false, error: '', busy: false, children, onStop() {}, onRefresh() {} });
    const group = nodes(tree).find(node => node.props?.['aria-label'] === 'Actions for you to review');
    assert.equal(group.props.children, children);
    assert.equal(nodes(group).filter(node => node.type === 'confirmation').length, 5);
    assert.equal(nodes(tree).some(node => node.type === 'button' && /approve|confirm/i.test(text(node))), false);
    assert.equal(view.requests.length, 0);
  }
});

test('goal progress uses the validated owned read model and clears stale data on flag or account changes in two domains', async () => {
  for (const [ventureId, title] of domains) {
    const saved = task(ventureId, title); let flag = true, unavailable = false;
    const view = harness(url => {
      if (url === '/api/ui-config' && unavailable) throw new Error('Unavailable');
      return { ok: true, data: url === '/api/ui-config' ? { goals_v1: flag } : { items: [saved] } };
    });
    const props = { ventureId, message: message(saved) };
    view.render(props); await view.flush(); assert.match(text(view.render(props)), new RegExp(title));
    assert.ok(view.requests.some(([url]) => url === `/api/ventures/${ventureId}/handoffs`));
    flag = false; await view.poll(); assert.equal(nodes(view.render(props)).some(node => node.type === 'section'), false);
    flag = true; await view.poll(); assert.match(text(view.render(props)), new RegExp(title));
    unavailable = true; await view.poll(); assert.equal(nodes(view.render(props)).some(node => node.type === 'section'), false);
    unavailable = false; await view.poll(); assert.match(text(view.render(props)), new RegExp(title));
    view.switchSession(); assert.equal(nodes(view.render(props)).some(node => node.type === 'section'), false);
    view.cleanup();
    let release;
    const delayed = harness(url => url === '/api/ui-config' ? { ok: true, data: { goals_v1: true } }
      : new Promise(resolve => { release = () => resolve({ ok: true, data: { items: [saved] } }); }));
    delayed.render(props); await delayed.flush(); delayed.switchSession(); release(); await tick();
    assert.equal(nodes(delayed.render(props)).some(node => node.type === 'section'), false); delayed.cleanup();
    const invalid = harness(url => ({ ok: true, data: url === '/api/ui-config' ? { goals_v1: true } : { items: [{ ...saved, steps: 'invalid' }] } }));
    invalid.render(props); await invalid.flush(); const tree = invalid.render(props);
    assert.match(text(tree), /Your progress could not load/); assert.equal(button(tree, 'Stop'), undefined); invalid.cleanup();
  }
});

test('only the latest message for an exact goal shows progress while older approvals stay intact in two domains', async () => {
  for (const [ventureId, title] of domains) {
    const saved = task(ventureId, title), first = message(saved), latest = { ...message(saved), id: 'latest' };
    const responder = url => ({ ok: true, data: url === '/api/ui-config' ? { goals_v1: true } : { items: [saved] } });
    const view = harness(responder), children = { type: 'button', props: { children: 'Review your exact action' } };
    const props = { ventureId, message: first, messages: [first], children };
    view.render(props); await view.flush(); assert.ok(nodes(view.render(props)).some(node => node.type === 'section'));
    props.messages = [first, latest]; view.render(props); await view.flush();
    const old = view.render(props); assert.equal(nodes(old).some(node => node.type === 'section'), false);
    assert.match(text(old), /Review your exact action/);
    const next = harness(responder), newProps = { ...props, message: latest };
    next.render(newProps); await next.flush(); assert.ok(nodes(next.render(newProps)).some(node => node.type === 'section'));
    view.cleanup(); next.cleanup();
  }
});

test('Stop uses only the existing task cancel action, prevents duplicate writes and keeps saved results in two domains', async () => {
  for (const [ventureId, title] of domains) {
    const saved = task(ventureId, title), stopped = { ...saved, state: 'canceled', steps: saved.steps.map(step => step.state === 'completed' ? step : { ...step, state: 'canceled' }) };
    let release;
    const view = harness((url, init) => init ? new Promise(resolve => { release = () => resolve({ ok: true, data: stopped }); })
      : { ok: true, data: url === '/api/ui-config' ? { goals_v1: true } : { items: [saved] } });
    const props = { ventureId, message: message(saved) };
    view.render(props); await view.flush(); const stop = button(view.render(props), 'Stop');
    stop.props.onClick(); stop.props.onClick(); await tick();
    assert.equal(button(view.render(props), 'Stopping…').props.disabled, true);
    const writes = view.requests.filter(([, init]) => init);
    assert.equal(writes.length, 1); assert.equal(writes[0][0], `/api/ventures/${ventureId}/handoffs/${saved.id}/cancel`);
    assert.equal(writes[0][1].method, 'POST'); assert.equal(writes[0][1].body, '{}');
    release(); await tick(); const tree = view.render(props);
    assert.equal(button(tree, 'Stop'), undefined); assert.match(text(tree), /saved results are kept/);
    assert.match(text(tree), new RegExp(saved.steps[0].title)); assert.match(text(tree), /1 of 4 steps done/);
    assert.deepEqual(view.events, ['run-status-refresh']); view.cleanup();
  }
});

test('Stop rechecks the flag, blocks stale-session writes and reports failed cancellation without replay in two domains', async () => {
  for (const [ventureId, title] of domains) {
    for (const outcome of ['flag_off', 'account_changed', 'failed']) {
      const saved = task(ventureId, title); let flag = true, release;
      const view = harness((url, init) => {
        if (init) return { ok: false, status: 500, data: { detail: 'Internal approval receipt note' } };
        if (url === '/api/ui-config') return outcome === 'account_changed' && release === null
          ? new Promise(resolve => { release = () => resolve({ ok: true, data: { goals_v1: true } }); }) : { ok: true, data: { goals_v1: flag } };
        return { ok: true, data: { items: [saved] } };
      });
      const props = { ventureId, message: message(saved) };
      view.render(props); await view.flush(); const stop = button(view.render(props), 'Stop');
      if (outcome === 'flag_off') flag = false;
      if (outcome === 'account_changed') release = null;
      stop.props.onClick(); await tick();
      if (outcome === 'account_changed') { view.switchSession(); release(); await tick(); }
      const tree = view.render(props), writes = view.requests.filter(([, init]) => init);
      if (outcome === 'failed') {
        assert.equal(writes.length, 1); assert.match(text(tree), /Your stop could not be confirmed/);
        assert.doesNotMatch(text(tree), /Internal approval receipt note/); assert.equal(button(tree, 'Stop').props.disabled, true);
        assert.ok(button(tree, 'Refresh your progress')); await view.poll(); assert.equal(view.requests.filter(([, init]) => init).length, 1);
      } else { assert.equal(writes.length, 0); assert.equal(nodes(tree).some(node => node.type === 'section'), false); }
      view.cleanup();
    }
  }
});
