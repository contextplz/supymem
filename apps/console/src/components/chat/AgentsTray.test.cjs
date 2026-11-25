const assert = require('node:assert/strict');
const test = require('node:test');
const {readFileSync} = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

const domains = [['ven_suppliers', 'Procurement teams', 'Compare supplier invoices'], ['ven_readers', 'Book club members', 'Choose your next novel']];
const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
const text = node => !node || typeof node !== 'object' ? String(node ?? '') : [node.props?.children].flat(8).map(text).join('');
const tick = () => new Promise(resolve => setImmediate(resolve));

test('unstamped build failures are signed Charlie while named teammates keep their names across domains', () => {
  const source = ts.createSourceFile('ChatDock.tsx', readFileSync(path.join(__dirname, 'ChatDock.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const helper = source.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'speakerName');
  const code = ts.transpileModule(helper.getText(source), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText;
  const speakerName = new Function(`${code}; return speakerName;`)();
  for (const [, audience] of domains) {
    assert.equal(speakerName('assistant'), 'Charlie', audience);
    assert.equal(speakerName('assistant', {name: audience}), audience);
    assert.equal(speakerName('founder'), 'You');
  }
});

test('checked page previews open separately and exports download without leaving chat across domains', () => {
  const source = ts.createSourceFile('ChatDock.tsx', readFileSync(path.join(__dirname, 'ChatDock.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const helper = source.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'externalLinkProps');
  const code = ts.transpileModule(helper.getText(source), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText;
  const linkProps = new Function(`${code}; return externalLinkProps;`)();
  for (const [venture] of domains) {
    const prefix = `/api/ventures/${venture}/pagebuilder/jobs/page`;
    assert.deepEqual(linkProps(`${prefix}/preview`), {target: '_blank', rel: 'noopener noreferrer'});
    assert.deepEqual(linkProps(`${prefix}/source`), {download: true});
    assert.deepEqual(linkProps(`/api/ventures/${venture}/device-work-orders/order/source-archive`), {download: true});
    assert.deepEqual(linkProps(`/v/${venture}/device-work-orders/order`), {});
    assert.deepEqual(linkProps('/api/ventures/../pagebuilder/jobs/page/source'), {});
  }
});

test('page starts update one card with the latest order, preview and export across domains', () => {
  const {collapseAgentStatuses} = harness().exports;
  for (const [venture, audience] of domains) {
    const jobs = [0, 1].map(index => ({...job(venture, 'pagebuilder.build', index, index ? 'done' : 'failed'),
      task_id: 'landing_page_build', label: audience, created_at: `2026-11-17T10:0${index}:00Z`,
      reviewHref: `/v/${venture}/device-work-orders/order-${index}`, build: {orderId: `order-${index}`,
        ...(index ? {previewHref: `/api/ventures/${venture}/pagebuilder/jobs/${venture}_${index}/preview`,
          exportHref: `/api/ventures/${venture}/pagebuilder/jobs/${venture}_${index}/source`} : {})}}));
    const messages = jobs.map((entry, index) => ({id: `start-${index}`, role: 'assistant', kind: 'text', run_id: entry.id,
      body_md: 'Your coding tool will build your private page from your saved venture brief. Watch or stop it in Build activity; nothing is published.',
      actions: [{label: 'Watch your page build', href: `/v/${venture}/builds`}]}));
    const original = JSON.stringify(messages);
    const cards = collapseAgentStatuses(messages, false, jobs);
    assert.equal(cards.length, 1);
    assert.equal(cards[0].id, 'start-1');
    assert.ok(cards[0].actions.some(action => action.href === jobs[1].reviewHref));
    assert.ok(cards[0].actions.some(action => action.href === jobs[1].build.previewHref));
    assert.ok(cards[0].actions.some(action => action.href === jobs[1].build.exportHref));
    assert.doesNotMatch(cards[0].body_md, /Build activity|Website/);
    assert.equal(JSON.stringify(messages), original);
    const legacy = messages.map(message => ({...message, run_id: undefined}));
    assert.equal(collapseAgentStatuses(legacy, false, jobs).length, 1);
    assert.ok(collapseAgentStatuses(legacy, false, jobs)[0].actions.some(action => action.href === jobs[1].reviewHref));
    const saved = legacy.map(message => ({...message, actions: [{label: 'Show status here', action: 'status'}]}));
    assert.equal(collapseAgentStatuses(saved, false, jobs).length, 1);
    assert.ok(collapseAgentStatuses(saved, false, jobs)[0].actions.some(action => action.href === jobs[1].reviewHref));
    const pending = {...saved[1], id: 'pending', actions: [{label: 'Show status here', action: 'status', task_id: 'landing_page_build',
      payload: {venture_id: venture, job_id: 'pending-page'}}]};
    assert.equal(collapseAgentStatuses([...saved, pending], false, [], venture).length, 1);
  }
});

test('fallback page result buttons use the authenticated transport across domains', () => {
  function PageResultLink() {}
  for (const [venture] of domains) {
    const page = harness({'./PageResultLink': {PageResultLink}});
    const saved = job(venture, 'pagebuilder.build', 0, 'done');
    const prefix = `/api/ventures/${venture}/pagebuilder/jobs/${saved.id}`;
    saved.build = {previewHref: `${prefix}/preview`, exportHref: `${prefix}/source`};
    const tree = page.render(page.exports.AgentsTray, {jobs: [saved], now: Date.now(), onDismiss() {}, showInline: true});
    assert.deepEqual(nodes(tree).filter(node => node.type === PageResultLink).map(node => node.props.href),
      [saved.build.previewHref, saved.build.exportHref]);
    assert.ok(!nodes(tree).some(node => node.type === 'a' && node.props.href?.startsWith(prefix)));
  }
});

test('Charlie explains the saved page failure and offers an inline retry across domains', async () => {
  const {collapseAgentStatuses} = harness().exports;
  for (const [venture, audience] of domains) {
    const reason = `Your signup button for ${audience} is missing.`;
    const attempt = {...job(venture, 'device.work_order', 0, 'failed'), task_id: 'landing_page_build', label: 'Page build',
      failure_reason: reason, reviewHref: `/v/${venture}/device-work-orders/order`, build: {orderId: 'order', jobId: `${venture}_page`}};
    const messages = [{id: 'failure', role: 'assistant', kind: 'mac_task_stopped',
      body_md: 'Your coding tool could not confirm the result; choose Retry.',
      actions: [{label: 'Retry', href: attempt.reviewHref, payload: {order_id: 'order', build_action: 'pagebuilder.build'}}]}];
    const [card] = collapseAgentStatuses(messages, false, [attempt]);
    assert.match(card.body_md, /^<!--employee:Charlie:Manager:manager-->/);
    assert.ok(card.body_md.includes(reason));
    assert.doesNotMatch(card.body_md, /could not confirm Page build/);
    assert.ok(card.actions.some(action => action.label === 'Retry' && action.action === 'pagebuilder.build' && !action.href));
    assert.deepEqual(card.actions.find(action => action.label === 'Retry').payload, {retry_job_id: attempt.build.jobId});
    assert.ok(card.actions.some(action => action.href === attempt.reviewHref && action.label !== 'Retry'));
    const [legacy] = collapseAgentStatuses(messages, false, [{...attempt,
      failure_reason: `${reason} Open Build activity to review it, then choose Retry on Website.`}]);
    assert.ok(legacy.body_md.includes(reason));
    assert.doesNotMatch(legacy.body_md, /Build activity|Website/);
    let release;
    const calls = [], page = harness();
    const tree = page.render(page.exports.AgentsTray, {jobs: [attempt], now: Date.now(), onDismiss() {}, showInline: true,
      onAction: (action, payload) => {calls.push({action, payload}); return new Promise(resolve => {release = resolve;});}});
    const button = nodes(tree).find(node => node.type === 'button' && text(node) === 'Retry');
    assert.ok(button);
    const retry = button.props.onClick();
    await button.props.onClick();
    release(true); await retry;
    assert.deepEqual(calls, [{action: 'pagebuilder.build', payload: {retry_job_id: attempt.build.jobId}}]);
  }
});

test('coding outcomes update one task card without repeated build recommendations across domains', () => {
  const {collapseAgentStatuses} = harness().exports;
  for (const [venture, audience] of domains) {
    const outcome = (id, order, task = 'page-task') => ({id, role: 'assistant', kind: 'mac_task_stopped',
      body_md: `Your coding tool could not finish ${audience}; choose Retry.`,
      actions: [{label: 'Retry', href: `/v/${venture}/device-work-orders/${order}`, task_id: task, payload: {order_id: order, build_action: task === 'page-task' ? 'pagebuilder.build' : 'build.tool.start'}}]});
    const recommendation = id => ({id, role: 'assistant', kind: 'next_moves', body_md: `Build a page for ${audience}`,
      actions: [{label: 'Build a page', action: 'pagebuilder.build', payload: {prepare: true}}]});
    const messages = [outcome('first', 'order-one'), recommendation('offer-one'),
      {id: 'owner', role: 'founder', body_md: audience}, outcome('latest', 'order-two'), recommendation('offer-two'),
      outcome('other-task', 'other-order', 'scraper-task')];
    const before = JSON.stringify(messages), visible = collapseAgentStatuses(messages);
    assert.deepEqual(visible.map(message => message.id), ['owner', 'latest', 'other-task']);
    assert.equal(visible.find(message => message.id === 'latest').actions[0].payload.order_id, 'order-two');
    assert.equal(JSON.stringify(messages), before);
    const legacy = [outcome('legacy-one', 'order-one'), outcome('legacy-two', 'order-two')]
      .map(message => ({...message, actions: message.actions.map(({task_id, payload, ...action}) => action)}));
    assert.deepEqual(collapseAgentStatuses(legacy).map(message => message.id), ['legacy-two']);
    const success = {...job(venture, 'device.work_order', 3, 'done'), task_id: 'page-task', label: audience};
    const resolved = collapseAgentStatuses(messages, false, [success]).find(message => message.id === 'latest');
    assert.match(resolved.body_md, /finished this task/);
    assert.equal(resolved.actions[0].payload.order_id, success.id);
    assert.ok(resolved.actions.some(action => action.action === 'status'));
  }
});

test('coding attention counts tasks once and only their newer success clears failures across domains', testContext => {
  const previous = global.window;
  global.window = {setTimeout: () => 1, clearTimeout() {}};
  testContext.after(() => {global.window = previous;});
  for (const [venture, audience] of domains) {
    const page = harness(), failed = [0, 1].map(index => ({...job(venture, 'device.work_order', index, 'failed'),
      task_id: 'page-task', label: audience, created_at: `2026-11-16T10:0${index}:00Z`}));
    const props = {jobs: failed, now: Date.now(), onDismiss() {}};
    assert.match(text(page.render(page.exports.AgentsTray, props)), /1 needs you/);
    const success = {...failed[1], id: 'success', status: 'done', created_at: '2026-11-16T10:02:00Z'};
    const retrying = page.render(page.exports.AgentsTray, {...props, jobs: [...failed, {...success, status: 'running'}]});
    assert.match(text(retrying), /1 needs you/);
    assert.doesNotMatch(text(retrying), /1 running/, 'the same task is not counted twice');
    assert.match(text(page.render(page.exports.AgentsTray, {...props, jobs: [...failed, {...success, task_id: 'another-task'}]})), /1 needs you/);
    assert.doesNotMatch(text(page.render(page.exports.AgentsTray, {...props, jobs: [...failed, success]})), /needs you/);
    const latest = page.render(page.exports.AgentsTray, props);
    nodes(latest).find(node => node.props?.className === 'summary').props.onClick();
    const opened = page.render(page.exports.AgentsTray, props);
    assert.equal(nodes(opened).filter(node => node.props?.className === 'chip').length, 1);
    nodes(opened).find(node => node.props?.className === 'chip').props.onClick();
    assert.doesNotMatch(text(page.render(page.exports.AgentsTray, props)), /Mac task|your Mac/);
    const dismissedEarlier = [], retryProps = {...props, jobs: [...failed, {...success, status: 'running'}], onDismiss: id => dismissedEarlier.push(id)};
    const retryTree = page.render(page.exports.AgentsTray, retryProps);
    nodes(retryTree).find(node => node.props?.className === 'chip').props.onClick();
    nodes(page.render(page.exports.AgentsTray, retryProps)).find(node => node.type === 'button' && text(node) === 'Dismiss earlier result').props.onClick();
    assert.deepEqual(dismissedEarlier, [failed[1].id]);
    const resolved = harness(), dismissed = [], resolvedProps = {...props, jobs: [...failed, success], onDismiss: id => {
      dismissed.push(id); resolvedProps.jobs = resolvedProps.jobs.filter(item => item.id !== id);
    }};
    resolved.render(resolved.exports.AgentsTray, resolvedProps); resolved.start();
    assert.deepEqual(dismissed, failed.map(item => item.id));
    resolvedProps.onDismiss(success.id);
    assert.equal(resolved.render(resolved.exports.AgentsTray, resolvedProps), null);
    resolved.cleanup();
  }
});

test('saved failed orders override stale tool status in inline cards and stay dismissed across domains', async testContext => {
  const previous = {window: global.window, document: global.document};
  testContext.after(() => Object.assign(global, previous));
  for (const [venture, audience] of domains) {
    for (const [desktop, linkedPage] of [[false, false], [false, true], [true, false], [true, true]]) {
      let poll;
      const storage = new Map(), dismissedStorage = new Map();
      global.window = {setInterval: callback => {poll = callback; return 1;}, clearInterval() {}, addEventListener() {}, removeEventListener() {},
        localStorage: {getItem: key => dismissedStorage.get(key) || null, setItem: (key, value) => dismissedStorage.set(key, value)},
        sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)}};
      global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
      const order = {id: 'failed-order', venture_id: venture, state: 'failed', failure_code: 'coding_cli_missing',
        result_summary: `Your coding tool could not find the signup button for ${audience}.`,
        manifest: {task_id: 'page-task', title: audience, coding_loop_verification: {provider: 'codex', phase: 'implement', max_iterations: 2}}};
      const overrides = {'@/lib/localMode': {isLocalConsole: () => true}, '@/lib/client': {
        verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
        api: async path => {
          if (path === `/api/ventures/${venture}`) return {ok: true, data: {workspace_id: 'workspace'}};
          if (path.includes('/workspaces/')) return {ok: true, data: {items: linkedPage ? [job(venture, 'pagebuilder.build', 0)] : []}};
          if (path.includes('/pagebuilder/jobs/')) return {ok: true, data: {job_id: `${venture}_0`, state: linkedPage ? 'ready' : 'working', stage: 'brief_direction', order_id: order.id}};
          if (path.endsWith('/device-work-orders')) return {ok: true, data: {orders: [{id: order.id, state: order.state, task_id: 'page-task', created_at: '2026-11-16T10:01:00Z'}]}};
          if (path.endsWith(`/device-work-orders/${order.id}`)) return {ok: true, data: order};
          throw new Error(path);
        }}, '@/lib/desktop': {isDesktopShell: () => desktop, desktopInvoke: async () => ({order_id: order.id, state: 'approved'})}};
      const page = harness(overrides);
      page.hook(venture, true); page.start(); await tick();
      let result = page.hook(venture, true);
      assert.equal(result.error, ''); assert.equal(result.jobs.length, 1); assert.equal(result.jobs[0].status, 'failed');
      assert.equal(result.jobs[0].failure_reason, order.result_summary);
      assert.equal(result.jobs[0].build.previewHref, undefined);
      assert.equal(result.jobs[0].build.exportHref, undefined);
      assert.notEqual(result.jobs[0].progress.step_label, 'Ready');
      const tray = harness(overrides), tree = tray.render(tray.exports.AgentsTray, {jobs: result.jobs, now: Date.now(), onDismiss() {}, showInline: true});
      const review = nodes(tree).find(node => typeof node.type === 'function' && node.type.name === 'ProductBuildReview');
      assert.ok(review);
      assert.doesNotMatch(text(tray.render(review.type, review.props)), /getting ready/);
      assert.match(text(tray.render(review.type, review.props)), /Keys and connections/);
      result.dismiss(result.jobs[0].id); poll(); await tick(); result = page.hook(venture, true);
      assert.equal(result.jobs.length, 0);
      page.cleanup();
      storage.clear();
      const reopened = harness(overrides);
      reopened.hook(venture, true); reopened.start(); await tick();
      assert.equal(reopened.hook(venture, true).jobs.length, 0);
      reopened.cleanup();
    }
  }
});

test('saved successful stops need matching verification before clearing coding failures across domains', async testContext => {
  const previous = {window: global.window, document: global.document};
  testContext.after(() => Object.assign(global, previous));
  global.window = {setInterval: () => 1, clearInterval() {}, addEventListener() {}, removeEventListener() {},
    sessionStorage: {getItem: () => null, setItem() {}}};
  global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
  for (const [venture, audience] of domains) {
    for (const proof of ['verified', 'unverified', 'wrong-execution', 'uncertain', 'wrong-result']) {
      const order = {id: 'saved-stop', venture_id: venture, state: 'stopped', execution_id: 'execution',
        inference_reconciliation_required: proof === 'uncertain',
        native_stop: {execution_id: proof === 'wrong-execution' ? 'other-execution' : 'execution', task_verified: proof !== 'unverified', processes_stopped: true},
        iterations: [{execution_id: 'execution', outcome: proof === 'wrong-result' ? 'tests_failed' : 'pass'}],
        manifest: {task_id: 'page-task', title: audience, coding_loop_verification: {provider: 'codex', phase: 'implement', max_iterations: 2}}};
      const page = harness({'@/lib/localMode': {isLocalConsole: () => true}, '@/lib/client': {
        verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
        api: async path => {
          if (path === `/api/ventures/${venture}`) return {ok: true, data: {workspace_id: 'workspace'}};
          if (path.includes('/workspaces/')) return {ok: true, data: {items: []}};
          if (path.endsWith('/device-work-orders')) return {ok: true, data: {orders: [{id: order.id, state: 'stopped', created_at: '2026-11-16T10:02:00Z'}]}};
          if (path.endsWith(`/device-work-orders/${order.id}`)) return {ok: true, data: order};
          if (path.endsWith('/source-receipt')) return {ok: true, data: {order_id: order.id, venture_id: venture, state: 'stored'}};
          throw new Error(path);
        }}});
      page.hook(venture, true); page.start(); await tick();
      const result = page.hook(venture, true);
      assert.equal(result.error, ''); assert.equal(result.jobs[0].status, proof === 'verified' ? 'done' : 'failed');
      page.cleanup();
    }
  }
});

function harness(overrides = {}, dock = {querySelector: () => ({tagName: 'DIV'})}) {
  const state = [], effects = [], cache = new Map(), portalTargets = [];
  let cursor = 0;
  const react = {
    useState(initial) { const index = cursor++; if (!(index in state)) state[index] = typeof initial === 'function' ? initial() : initial;
      return [state[index], next => {state[index] = typeof next === 'function' ? next(state[index]) : next;}]; },
    useRef(initial) { const index = cursor++; if (!(index in state)) state[index] = {current: initial}; return state[index]; },
    useContext: context => context.testValue ?? 'home',
    createContext: testValue => ({testValue}),
    useEffect(effect, deps) { const index = cursor++, previous = state[index];
      if (!previous || deps.some((dep, position) => dep !== previous.deps[position])) {
        previous?.cleanup?.(); state[index] = {deps}; effects.push(() => {state[index].cleanup = effect();});
      } },
  };
  const jsx = (type, props, key) => ({type, props, key});
  const mocks = {
    react, 'react/jsx-runtime': {jsx, jsxs: jsx}, 'react-dom': {createPortal: (children, target) => {portalTargets.push(target); return children;}},
    '@/lib/client': {api: async () => ({ok: true, data: {items: []}}),
      verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1},
    '@/lib/desktop': {isDesktopShell: () => false, desktopInvoke: async () => null},
    '@/components/office/avatars': {parseEmployeeStamp: body => ({text: body.replace(/^<!--employee:[^>]*-->\s*/, '')})},
    './ChatLink': {ChatAnchor: 'a'}, '@/components/chat/ChatLink': {ChatAnchor: 'a'},
    './PageResultLink': {PageResultLink: 'page-result-link'}, ...overrides,
    '@/components/primitives/ErrorNotice': {ErrorNotice: 'error-notice'},
  };
  function load(filename) {
    if (cache.has(filename)) return cache.get(filename).exports;
    const loaded = new Module(filename, module); cache.set(filename, loaded);
    loaded.filename = filename; loaded.paths = Module._nodeModulePaths(path.dirname(filename));
    const native = loaded.require.bind(loaded);
    loaded.require = id => {
      if (id in mocks) return mocks[id];
      if (id.endsWith('.css')) return {default: new Proxy({}, {get: (_, key) => key})};
      if (id.startsWith('@/lib/')) return load(path.resolve(__dirname, '../../lib', id.slice(6) + '.ts'));
      if (id.startsWith('@/components/desktop/')) return load(path.resolve(__dirname, '../desktop', id.slice('@/components/desktop/'.length) + '.tsx'));
      if (['./client.ts', './apiFailure.ts', './uiDiagnostics.ts', './pendingJourneyQuestion.ts'].includes(id)) return mocks['@/lib/client'];
      if (id.startsWith('.')) return load(path.resolve(path.dirname(filename), id));
      return native(id);
    };
    loaded._compile(ts.transpileModule(readFileSync(filename, 'utf8'), {compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    }}).outputText, filename);
    return loaded.exports;
  }
  const exports = load(path.join(__dirname, 'AgentsTray.tsx'));
  return {exports, state, effects, portalTargets, load: file => load(path.join(__dirname, file)), render(component, props) {
    cursor = 0;
    let tree = component(props);
    if (component === exports.AgentsTray) {
      const anchor = nodes(tree).find(node => node.props?.hidden && node.props?.ref);
      if (anchor && !anchor.props.ref.current) {
        anchor.props.ref.current = {parentElement: dock};
        effects.shift()();
        cursor = 0; tree = component(props);
      }
    }
    return tree;
  },
    hook(venture, visible, onFinished) {cursor = 0; return exports.useAgentJobs(venture, visible, onFinished);},
    start() {effects.splice(0).forEach(effect => effect());},
    cleanup() {state.forEach(value => value?.cleanup?.());},
  };
}

const job = (venture, kind, index, state = 'running') => ({id: `${venture}_${index}`, venture_id: venture, kind, status: state,
  created_at: '2026-10-27T10:00:00Z', started_at: '2026-10-27T10:00:00Z', steps: ['Reading saved inputs'],
  progress: {state, percent: 40, step_label: 'Checking sources', started_at: '2026-10-27T10:00:00Z'}});

test('local tray refreshes from activity and polls only at the 30-second safety interval', async context => {
  const previous = {window: global.window, document: global.document};
  context.after(() => Object.assign(global, previous));
  context.mock.timers.enable({apis: ['setInterval']});
  const counts = [];
  for (const [venture] of domains) {
    global.window = {setInterval, clearInterval, addEventListener() {}, removeEventListener() {}};
    global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
    const calls = [];
    let active = false;
    const page = harness({'@/lib/localMode': {isLocalConsole: () => true}, '@/lib/client': {
      verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async path => {
        if (path.includes('/jobs?')) {calls.push('jobs'); return {ok: true, data: {items: active ? [job(venture, 'research.deep', 0)] : []}};}
        if (path.endsWith('/device-work-orders')) {calls.push('orders'); return {ok: true, data: {orders: []}};}
        return {ok: true, data: {workspace_id: `wsp_${venture}`}};
      },
    }});
    const {ActivityRefreshContext, createActivityRefresh} = page.load('../../lib/activityRefresh.ts');
    const feed = createActivityRefresh(venture);
    ActivityRefreshContext.testValue = feed;
    feed.setConnected(true);
    const readCounts = () => ['jobs', 'orders'].map(kind => calls.filter(call => call === kind).length);
    const advance = async seconds => {
      for (let elapsed = 0; elapsed < seconds; elapsed++) {context.mock.timers.tick(1000); await tick();}
    };
    page.hook(venture, true); page.start(); await tick(); calls.length = 0;
    await advance(20); const idle = readCounts(); calls.length = 0;
    active = true; feed.changed(); feed.changed(); await tick();
    const event = readCounts(); const jobs = page.hook(venture, true).jobs.length;
    calls.length = 0; await advance(20); const activeInterval = readCounts();
    feed.setConnected(false); calls.length = 0;
    await advance(20); const down20s = readCounts();
    await advance(10); const down30s = readCounts();
    counts.push({idle, event, jobs, activeInterval, down20s, down30s});
    page.cleanup();
  }
  context.diagnostic(`GETs [jobs, orders]: ${JSON.stringify(counts)}`);
  assert.deepEqual(counts, domains.map(() => ({idle: [0, 0], event: [1, 1], jobs: 1, activeInterval: [0, 0], down20s: [0, 0], down30s: [1, 1]})));
});

test('build stages are plain words in live and saved progress across domains', () => {
  const page = harness(), progress = page.load('../../lib/loopProgress.ts');
  for (const [venture] of domains) {
    for (const [stage, expected] of [['brief_direction_plan', 'Writing the brief'], ['plan_repair_3', 'Writing the brief'],
      ['building', 'Codex is building'], ['checking_layout', 'Checking the page'], ['unknown_internal_stage', 'Codex is building']]) {
      assert.equal(progress.buildStageLabel(stage, 'codex'), expected);
      const saved = page.exports.updateAgentJobs([], [{...job(venture, 'pagebuilder.build', 0), progress: {step_label: stage}}]);
      assert.doesNotMatch(saved[0].steps.join(' '), /brief_direction|plan_repair|internal_stage|checking_layout/);
    }
  }
});

test('local research progress and chat refresh do not wait for coding-tool status across domains', async testContext => {
  const previous = {window: global.window, document: global.document};
  testContext.after(() => Object.assign(global, previous));
  for (const [venture] of domains) {
    let poll, completed = false, reloads = 0;
    global.window = {setInterval: callback => {poll = callback; return 1;}, clearInterval() {}, addEventListener() {}, removeEventListener() {}};
    global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
    const running = {...job(venture, 'research.deep', 0), progress: {state: 'running', percent: 50, step_label: 'Reading source pages'}};
    const page = harness({'@/lib/localMode': {isLocalConsole: () => true}, '@/lib/client': {
      verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async path => {
        if (path === `/api/ventures/${venture}`) return {ok: true, data: {workspace_id: 'workspace'}};
        if (path.startsWith('/api/workspaces/')) return {ok: true, data: {items: completed ? [] : [running]}};
        if (path === `/api/jobs/${running.id}`) return {ok: true, data: {...running, status: 'succeeded',
          progress: {state: 'done', percent: 100, step_label: 'Complete'}}};
        return {ok: false};
      }}});
    const reload = () => {reloads++;};
    page.hook(venture, true, reload); page.start(); await tick();
    assert.equal(page.hook(venture, true, reload).jobs[0]?.progress.percent, 50);
    completed = true; poll(); await tick();
    const saved = page.hook(venture, true, reload);
    assert.equal(saved.jobs[0]?.progress.percent, 100);
    const tray = harness(), props = {...saved, error: '', onDismiss() {}};
    const tree = tray.render(tray.exports.AgentsTray, props);
    nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
    assert.match(text(tray.render(tray.exports.AgentsTray, props)), /Research ready/);
    assert.equal(reloads, 1);
    poll(); await tick();
    assert.equal(reloads, 1);
    page.cleanup();
  }
});

test('local builds poll their connected tool and keep only scrubbed activity across domains', async testContext => {
  const previous = {window: global.window, document: global.document};
  testContext.after(() => Object.assign(global, previous));
  for (const [venture, audience] of domains) {
    let poll, stopped = false;
    const storage = new Map(), calls = [];
    global.window = {setInterval: callback => {poll = callback; return 1;}, clearInterval() {}, addEventListener() {}, removeEventListener() {},
      sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)}};
    global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
    const order = {id: 'coding-order', venture_id: venture, state: 'claimed', requested_build: true,
      manifest: {title: audience, coding_loop_verification: {provider: 'codex', profile: 'web_app_vercel_v1', workspace_slug: venture,
        phase: 'implement', max_iterations: 2, max_wall_seconds: 60, per_call_seconds: 30}}};
    const page = harness({'@/lib/localMode': {isLocalConsole: () => true}, '@/lib/client': {
      verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async path => {
        calls.push(path);
        if (path === `/api/ventures/${venture}`) return {ok: true, data: {workspace_id: 'workspace'}};
        if (path.includes('/workspaces/')) return {ok: true, data: {items: []}};
        if (path.endsWith('/device-work-orders')) return {ok: true, data: {orders: [order]}};
        if (path.endsWith('/device-work-orders/coding-order')) return {ok: true, data: order};
        if (path.endsWith('/source-receipt')) return {ok: true, data: {order_id: order.id, venture_id: venture, state: 'stored'}};
        throw new Error(path);
      }}, '@/lib/desktop': {isDesktopShell: () => true, desktopInvoke: async (command, args) => {
        assert.equal(command, 'device_work_order_action'); assert.equal(args.action, 'status');
        return {order_id: order.id, state: stopped ? 'stopped' : 'running_coding', task_verified: stopped,
          evidence: {processes_stopped: stopped, product_build: {outcome: stopped ? 'pass' : undefined}},
          events: [{at: 1, text: `${audience}: Reading the project`}, {at: 2, text: 'password=private-fixture'}]};
      }}});
    page.hook(venture, true); page.start(); await tick();
    let result = page.hook(venture, true);
    assert.equal(result.error, ''); assert.equal(result.jobs.length, 1);
    assert.equal(result.jobs[0].progress.step_label, 'Codex is building');
    assert.match(result.jobs[0].build.events[0].text, new RegExp(audience));
    assert.doesNotMatch(JSON.stringify([...storage.values()]), /private-fixture/);
    stopped = true; poll(); await tick(); result = page.hook(venture, true);
    assert.equal(result.jobs[0].status, 'done'); assert.equal(result.jobs[0].progress.step_label, 'Ready');
    assert.equal(result.jobs[0].build.exportHref, `/api/ventures/${venture}/device-work-orders/coding-order/source-archive`);
    page.cleanup();
  }
});

test('show status exposes an inline build with real stop and folder controls across domains', async () => {
  for (const [venture, audience] of domains) {
    const calls = [], page = harness({'@/lib/desktop': {isDesktopShell: () => true,
      desktopInvokeOrThrow: async (command, args) => {
        calls.push([command, args]);
        if (command === 'device_work_order_action') return {order_id: 'coding-order', state: 'stopped', evidence: {processes_stopped: true}};
        if (command === 'venture_workspace_path') return '/tmp/project';
      }}});
    const loop = {provider: 'codex', workspace_slug: venture, max_iterations: 2, profile: 'web_app_vercel_v1'};
    const props = {jobs: [{...job(venture, 'device.work_order', 0), build: {orderId: 'coding-order', loop,
      watch: {state: 'running_coding'}, events: [{at: 1, text: audience}]}}], now: Date.now(), onDismiss() {}, showInline: true};
    const tree = page.render(page.exports.AgentsTray, props);
    assert.ok(nodes(tree).find(node => node.props?.['aria-label'] === 'Your build activity'));
    const review = nodes(tree).find(node => typeof node.type === 'function' && node.type.name === 'ProductBuildReview');
    assert.ok(review); assert.equal(review.props.inlineStage, 'Codex is building');
    await review.props.onStopBuild();
    assert.deepEqual(calls[0], ['device_work_order_action', {ventureId: venture, orderId: 'coding-order', action: 'cancel'}]);
    await review.props.onRevealFolder(); assert.equal(calls.at(-1)[0], 'reveal_in_file_manager');
    const before = calls.length;
    page.render(page.exports.AgentsTray, {...props, jobs: [{...props.jobs[0], venture_id: 'another-venture'}]});
    await review.props.onStopBuild(); assert.equal(calls.length, before, 'a stale chat card cannot stop another venture session');
  }
});

test('your inline build uses scrubbed core events and round failures without a native stream', async t => {
  const previous = {window: global.window, document: global.document};
  t.after(() => Object.assign(global, previous));
  for (const [venture, audience] of domains) {
    global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
    global.window = {setInterval: () => 1, clearInterval() {}, addEventListener() {}, removeEventListener() {},
      sessionStorage: {getItem: () => null, setItem() {}}};
    const order = {id: 'live-page', venture_id: venture, state: 'claimed',
      events: [{at: 1, text: `${audience}: pagebuilder_plan_copy_rejected:Your offer contains an unsupported claim.`}],
      iterations: [{index: 0, phase: 'code', outcome: 'tests_failed', summary: 'On Website, request a fix and choose Retry.',
        diagnosis: {failures: ['required_text_missing:Your signup label is missing.', 'password=private-round-fixture']}}],
      manifest: {title: audience, coding_loop_verification: {provider: 'codex', phase: 'implement', max_iterations: 2}}};
    const page = harness({'@/lib/localMode': {isLocalConsole: () => true}, '@/lib/client': {
      verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async path => {
        if (path === `/api/ventures/${venture}`) return {ok: true, data: {workspace_id: 'workspace'}};
        if (path.includes('/workspaces/')) return {ok: true, data: {items: []}};
        if (path.endsWith('/device-work-orders')) return {ok: true, data: {orders: [order]}};
        if (path.endsWith('/live-page')) return {ok: true, data: order};
        throw new Error(path);
      }}, '@/lib/desktop': {isDesktopShell: () => true, desktopInvoke: async () =>
        ({order_id: order.id, state: 'running_coding', events: []})}});
    page.hook(venture, true); page.start(); await tick();
    const result = page.hook(venture, true);
    assert.equal(result.error, '');
    assert.match(result.jobs[0].build.events[0].text, /unsupported claim/);
    assert.doesNotMatch(result.jobs[0].build.events[0].text, /pagebuilder_/);
    assert.match(JSON.stringify(result.jobs[0].build.iterations), /Your signup label is missing/);
    assert.doesNotMatch(JSON.stringify(result.jobs[0].build.iterations), /required_text_missing|private-round-fixture/);
    page.cleanup();
  }
});

test('an unconfirmed Stop does not claim the tool stopped or expose its error across domains', async () => {
  for (const [venture] of domains) {
    const page = harness({'@/lib/desktop': {isDesktopShell: () => true, desktopInvokeOrThrow: async () => ({order_id: 'coding-order',
      state: 'stopped', evidence: {processes_stopped: false}, message: 'password=private-fixture'})}});
    const props = {jobs: [{...job(venture, 'device.work_order', 0), build: {orderId: 'coding-order',
      loop: {provider: 'claude', workspace_slug: venture, max_iterations: 2}, watch: {state: 'running_coding'}}}],
      now: Date.now(), onDismiss() {}, showInline: true};
    const tree = page.render(page.exports.AgentsTray, props);
    const review = nodes(tree).find(node => typeof node.type === 'function' && node.type.name === 'ProductBuildReview');
    await review.props.onStopBuild();
    const result = page.render(page.exports.AgentsTray, props);
    const failure = nodes(result).find(node => node.type === 'error-notice' && node.props.source === 'chat.build_activity').props.error;
    assert.match(failure, /not confirmed Stop.*Watch your tool/); assert.doesNotMatch(failure, /private-fixture/);
  }
});

test('local venture polling discovers builds without requesting computer-use details across domains', async testContext => {
  const previous = global.window, previousDocument = global.document;
  testContext.after(() => {global.window = previous; global.document = previousDocument;});
  for (const [venture] of domains) {
    const calls = [];
    let poll;
    global.window = {setInterval: callback => {poll = callback; return 1;}, clearInterval() {},
      addEventListener() {}, removeEventListener() {}, dispatchEvent() {}};
    global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
    const page = harness({'@/lib/localMode': {isLocalConsole: () => true}, '@/lib/client': {
      verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async path => {
        calls.push(path);
        if (path === `/api/ventures/${venture}`) return {ok: true, data: {workspace_id: `wsp_${venture}`}};
        if (path.includes('/workspaces/')) return {ok: true, data: {items: [job(venture, 'research.deep', 0)]}};
        if (path.endsWith('/device-work-orders')) return {ok: true, data: {orders: []}};
        throw new Error(path);
      },
    }});
    page.hook(venture, true); page.start(); await tick(); poll(); await tick();
    const result = page.hook(venture, true);
    assert.equal(result.error, '');
    assert.equal(result.jobs.length, 1);
    assert.ok(calls.some(path => path.includes('/workspaces/')));
    assert.ok(calls.every(path => !path.includes('/device-work-orders/')));
    page.cleanup();
  }
});

test('local chat caches settled builds and backs off unsettled stops across domains', async testContext => {
  const previous = {window: global.window, document: global.document};
  testContext.after(() => Object.assign(global, previous));
  let now = Date.now();
  testContext.mock.method(Date, 'now', () => now);
  for (const [venture, audience] of domains) {
    const base = `/api/ventures/${venture}/device-work-orders`;
    const calls = new Map(), statuses = new Map();
    const orders = Array.from({length: 31}, (_, index) => ({
      id: `build-${index}`, venture_id: venture, state: index === 30 ? 'claimed' : ['stopped', 'failed', 'cancelled', 'expired'][index % 4],
      closed_at: index % 4 === 0 ? new Date(now).toISOString() : undefined,
      manifest: {title: audience, coding_loop_verification: {provider: 'codex', phase: 'implement'}},
    }));
    const stopping = orders[2];
    stopping.claimed_at = new Date(now).toISOString();
    stopping.stop_reconciliation_required = true;
    const running = orders.at(-1);
    let poll, visibilityChanged;
    global.window = {setInterval: callback => {poll = callback; return 1;}, clearInterval: () => {poll = undefined;},
      addEventListener() {}, removeEventListener() {}, sessionStorage: {getItem: () => null, setItem() {}}};
    global.document = {hidden: false, addEventListener: (_, callback) => {visibilityChanged = callback;}, removeEventListener() {}};
    const tray = harness({'@/lib/localMode': {isLocalConsole: () => true}, '@/lib/client': {
      verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async route => {
        calls.set(route, (calls.get(route) || 0) + 1);
        if (route === `/api/ventures/${venture}`) return {ok: true, data: {workspace_id: 'workspace'}};
        if (route.includes('/workspaces/')) return {ok: true, data: {items: []}};
        if (route === base) return {ok: true, data: {orders: orders.map(({id, state}) => ({id, state}))}};
        const [, id, endpoint] = route.slice(base.length).split('/');
        const order = orders.find(item => item.id === id);
        assert.ok(order, route);
        if (endpoint === 'source-receipt') return id === 'build-0' ? {ok: false, status: 404, data: null}
          : {ok: true, data: {order_id: id, venture_id: venture, state: 'stored'}};
        if (id === 'build-1') return {ok: false, status: 404, data: null};
        if (order.state === 'expired') return {ok: true, data: {...order, state: 'pending', expires_at: new Date(Date.now() - 1000).toISOString()}};
        return {ok: true, data: {...order}};
      },
    }, '@/lib/desktop': {isDesktopShell: () => true, desktopInvoke: async (_, {orderId}) => {
      statuses.set(orderId, (statuses.get(orderId) || 0) + 1);
      if (orderId === stopping.id) {
        if (statuses.get(orderId) === 2) {
          stopping.stop_reconciliation_required = false;
          stopping.closed_at = new Date(now).toISOString();
        }
        return {order_id: orderId, state: stopping.stop_reconciliation_required ? 'reconciliation_required' : 'stopped',
          credits_reconciled: !stopping.stop_reconciliation_required};
      }
      const {state} = orders.find(order => order.id === orderId);
      return {order_id: orderId, state: state === 'expired' ? 'not_started' : state, task_verified: state === 'stopped',
        evidence: {processes_stopped: state === 'stopped', product_build: {outcome: 'pass'}}};
    }}});
    tray.hook(venture, true); tray.start(); await tick();
    for (let repeat = 0; repeat < 3; repeat++) {poll(); await tick();}
    assert.equal(calls.get(base), 4, 'one summary list per refresh');
    assert.equal(calls.get(`${base}/${running.id}`), 4, 'running work still updates');
    for (const order of orders.slice(0, -1)) {
      assert.equal(calls.get(`${base}/${order.id}`), 1, `${order.id} detail`);
      assert.equal(statuses.get(order.id) || 0, order.id === 'build-1' ? 0 : 1, `${order.id} native status`);
      assert.equal(calls.get(`${base}/${order.id}/source-receipt`) || 0, order.state === 'stopped' ? 1 : 0, `${order.id} source receipt`);
    }
    global.document.hidden = true; visibilityChanged(); await tick();
    assert.equal(poll, undefined, 'the hidden tab cancels its timer');
    assert.equal(calls.get(base), 4);
    running.state = 'stopped';
    running.closed_at = new Date(now).toISOString();
    global.document.hidden = false; visibilityChanged(); await tick();
    poll(); await tick();
    tray.hook(venture, false); tray.start();
    tray.hook(venture, true); tray.start(); await tick();
    const result = tray.hook(venture, true);
    assert.equal(result.error, '');
    assert.equal(result.jobs.length, 30, 'an inaccessible old order does not block visible results');
    assert.equal(result.jobs.find(item => item.id === running.id).build.exportHref, `${base}/${running.id}/source-archive`);
    assert.equal(calls.get(`${base}/${running.id}`), 5, 'the final state is read after resuming, then cached');
    assert.equal(calls.get(`${base}/${running.id}/source-receipt`), 1);
    assert.equal(calls.get(`${base}/build-0/source-receipt`), 1, 'reopening does not bypass the backoff');
    assert.equal(stopping.stop_reconciliation_required, true);
    now += 30_000;
    poll(); await tick();
    assert.equal(stopping.stop_reconciliation_required, false, 'a status recheck can reconcile the stop');
    now += 30_000;
    poll(); await tick();
    now += 30_000;
    poll(); await tick();
    assert.equal(statuses.get(stopping.id), 3, 'cache the cancelled order after confirming the saved stop');
    assert.equal(calls.get(`${base}/build-0/source-receipt`), 4, 'missing receipts are retried on backoff');
    assert.equal(calls.get(`${base}/build-1`), 4, 'inaccessible details are retried on backoff');
    assert.equal(calls.get(`${base}/build-4`), 1, 'a settled order with a verified archive stays cached');
    tray.cleanup();
  }
});

test('a transient source receipt failure recovers the export link without restarting across domains', async testContext => {
  const previous = {window: global.window, document: global.document};
  testContext.after(() => Object.assign(global, previous));
  let now = Date.now();
  testContext.mock.method(Date, 'now', () => now);
  for (const [venture, audience] of domains) {
    const base = `/api/ventures/${venture}/device-work-orders`;
    const order = {id: 'checked-build', venture_id: venture, state: 'stopped', closed_at: new Date(now).toISOString(),
      manifest: {title: audience, coding_loop_verification: {provider: 'codex', phase: 'implement'}}};
    let poll, sourceCalls = 0;
    global.window = {setInterval: callback => {poll = callback; return 1;}, clearInterval() {},
      addEventListener() {}, removeEventListener() {}, sessionStorage: {getItem: () => null, setItem() {}}};
    global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
    const tray = harness({'@/lib/localMode': {isLocalConsole: () => true}, '@/lib/client': {
      verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async route => {
        if (route === `/api/ventures/${venture}`) return {ok: true, data: {workspace_id: 'workspace'}};
        if (route.includes('/workspaces/')) return {ok: true, data: {items: []}};
        if (route === base) return {ok: true, data: {orders: [order]}};
        if (route === `${base}/${order.id}`) return {ok: true, data: order};
        assert.equal(route, `${base}/${order.id}/source-receipt`);
        if (++sourceCalls === 1) return {ok: false, status: 503, data: null};
        return {ok: true, data: {order_id: order.id, venture_id: venture, state: 'stored'}};
      },
    }, '@/lib/desktop': {isDesktopShell: () => true, desktopInvoke: async () => ({order_id: order.id, state: 'stopped', task_verified: true,
      evidence: {processes_stopped: true, product_build: {outcome: 'pass'}}})}});
    const started = {id: 'started', role: 'assistant', kind: 'build_tool_started',
      actions: [{href: `/v/${venture}/device-work-orders/${order.id}`}]};
    const exportLink = () => tray.exports.collapseAgentStatuses([started], false, tray.hook(venture, true).jobs)[0]
      .actions.find(action => action.label === 'Export')?.href;
    tray.hook(venture, true); tray.start(); await tick();
    assert.equal(exportLink(), undefined);
    now += 29_999;
    poll(); await tick();
    assert.equal(sourceCalls, 1, 'regular polling respects the backoff');
    now += 1;
    poll(); await tick();
    assert.equal(exportLink(), `${base}/${order.id}/source-archive`);
    now += 30_000;
    poll(); await tick();
    assert.equal(sourceCalls, 2, 'a confirmed archive does not need another lookup');
    tray.cleanup();
  }
});

test('task tiles reserve red for failures and explain each saved result in unrelated domains', () => {
  for (const [venture, audience] of domains) {
    for (const [status, error_code, failure_reason, red, kind = 'research.deep', reason = failure_reason || 'Your task did not finish.'] of [
      ['pending', null, null, false],
      ['running', null, null, false],
      ['retry_wait', 'provider_retryable', null, false],
      ['succeeded', null, null, false],
      ['done', null, null, false],
      ['cancelled', null, null, false],
      ['failed_dead', 'research_saved_sources_unavailable', 'You have no usable sources yet.', false],
      ['failed_dead', 'research_byok_unavailable', 'Your selected connection is not ready.', false],
      ['failed_dead', 'research_confirmation_required', 'You need to confirm your idea before starting research.', false],
      ['failed_dead', 'capability_unavailable', 'You cannot use this workflow yet.', false],
      ['failed_dead', 'confirmed_revision_required', 'You need to confirm your idea before planning.', false, 'plan.compile'],
      ['failed_dead', 'plan_research_required', 'You need to finish your research before planning.', false, 'plan.compile'],
      ['failed_dead', 'plan_resource_inventory_required', 'You need to add your available resources before planning.', false, 'plan.compile'],
      ['failed_dead', 'research_idea_missing', 'You need to save your idea before researching.', false],
      ['failed_dead', 'research_call_budget_exhausted', 'Research used up its call budget before it could finish.', false, 'research.deep',
        'Research used up its search limit before it could finish.'],
      ['failed', 'research_invalid_output', 'Your research review could not be read.', true],
      ['failed_dead', 'handler_failed', 'Your task did not finish.', true],
      ['poison', 'job_failed', null, true],
    ]) {
      const trayHarness = harness(), saved = {...job(venture, kind, 0, status), error_code, failure_reason, label: audience};
      const props = {jobs: [saved], now: Date.now(), onDismiss() {}, error: 'Your task status could not load. Try again.'};
      let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
      nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
      tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
      assert.equal(nodes(tree).some(node => node.props?.className?.split(' ').includes('failed')), red, `${audience}: ${status}/${error_code}`);
      assert.equal(nodes(tree).find(node => node.type === 'error-notice' && node.props.source === 'chat.agents.status').props.error, props.error);
      if (['failed', 'failed_dead', 'poison'].includes(status)) {
        assert.ok(text(tree).includes(reason), `${audience}: the tile explains what happened`);
        assert.match(text(tree), /Review what stopped before trying again\./);
        nodes(tree).find(node => node.props?.className === 'chip').props.onClick();
        tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
        assert.ok(nodes(tree).some(node => node.type === 'p' && text(node).includes(reason)
          && text(node).includes('Next: Review what stopped before trying again.')), 'expanded text keeps the whole explanation readable');
      }
      assert.doesNotMatch(text(tree), /research_invalid_output|handler_failed|job_failed|call budget|\u2014/);
    }
    const trayHarness = harness(), jobs = [
      {...job(venture, 'research.deep', 0, 'failed_dead'), error_code: 'research_saved_sources_unavailable', failure_reason: 'You have no usable sources yet.'},
      {...job(venture, 'research.deep', 1, 'failed_dead'), error_code: 'research_invalid_output', failure_reason: 'Your research review could not be read.'},
    ];
    const props = {jobs, now: Date.now(), onDismiss() {}};
    let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    const cards = nodes(tree).filter(node => node.props?.className?.split(' ').includes('agent'));
    assert.equal(cards.length, 2, 'different results must not share a misleading tile');
    assert.ok(cards.some(card => !card.props.className.split(' ').includes('failed') && text(card).includes(jobs[0].failure_reason)));
    assert.ok(cards.some(card => card.props.className.split(' ').includes('failed') && text(card).includes(jobs[1].failure_reason)));
  }
});

test('browse tasks show bounded progress and cited results without duplicate chatter in unrelated domains', () => {
  for (const [venture, audience] of domains) {
    const trayHarness = harness(), {browseProgressFor, collapseAgentStatuses, AgentsTray} = trayHarness.exports;
    const active = job(venture, 'browse.web', 1);
    const progress = {job_id: active.id, state: 'running', percent: 25,
      step_label: `Read 5/20 pages for ${audience}. Reserved up to $0.10 of $2.00.`, started_at: active.started_at};
    const message = (id, state = 'running') => ({id, role: 'assistant', kind: 'browse_progress', body_md: audience,
      citations: [{browse_progress: {...progress, state}}]});
    const messages = [message('queued', 'queued'), message('running'), {...message('invalid'), role: 'user'},
      {...message('broken'), citations: [{browse_progress: {...progress, percent: NaN}}]}];
    const valid = browseProgressFor(messages);
    assert.equal(valid.length, 1);
    assert.equal(valid[0].state, 'running');
    const props = {jobs: [active], progress: valid, now: Date.now(), onDismiss() {}};
    let tree = trayHarness.render(AgentsTray, props);
    nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
    tree = trayHarness.render(AgentsTray, props);
    assert.match(text(tree), /Browsing public pages/);
    assert.ok(text(tree).includes(progress.step_label));
    const citation = {label: audience, href: `https://${venture}.example/details`};
    const result = {...message('result', 'ready'), kind: 'browse_result', body_md: `Your cited notes for ${audience}.`,
      citations: [{browse_progress: {...progress, state: 'ready', percent: 100}}, citation]};
    const transcript = [message('queued', 'queued'), message('running'), result];
    const before = JSON.stringify(transcript), collapsed = collapseAgentStatuses(transcript);
    assert.deepEqual(collapsed.map(message => message.id), ['result']);
    assert.equal(collapsed[0].citations[1], citation);
    assert.equal(JSON.stringify(transcript), before);
    assert.equal(browseProgressFor(transcript)[0].state, 'ready');
    tree = trayHarness.render(AgentsTray, {...props, jobs: [{...active, status: 'succeeded'}]});
    assert.match(text(tree), /Browse notes ready/);
    assert.doesNotMatch(text(tree), /—/);
  }
});

test('empty assistant bubbles disappear without hiding answers, diagrams, cards or approval controls', () => {
  const {collapseAgentStatuses} = harness().exports;
  for (const [venture, audience, instruction] of domains) {
    const messages = [
      {id: 'empty', role: 'assistant'},
      {id: 'whitespace', role: 'assistant', body_md: ' \n\t'},
      {id: 'stamp', role: 'assistant', body_md: '<!--employee:manager-->'},
      {id: 'answer', role: 'assistant', body_md: ' \n', text: `Your next move for ${audience}.`},
      {id: 'founder', role: 'user', body_md: instruction},
      {id: 'diagram', role: 'assistant', parts: [{type: 'diagram', title: audience, svg: '<svg />'}]},
      {id: 'invalid-diagram', role: 'assistant', parts: [{type: 'diagram'}]},
      {id: 'unrendered', role: 'assistant', parts: [{type: 'unknown'}]},
      {id: 'actions', role: 'assistant', actions: [{label: 'Choose your next step', action: 'next_moves'}]},
      {id: 'confirmation', role: 'assistant', actions: [{confirmation: {}}]},
      {id: 'citations', role: 'assistant', citations: [{label: audience, href: '#source'}]},
      {id: 'references', role: 'assistant', founder_references: {startups: [], yc_advice: [{title: audience}]}},
      {id: 'review', role: 'assistant', kind: 'startup_review'},
      {id: 'beliefs', role: 'assistant', kind: 'market_beliefs'},
      {id: 'preview', role: 'assistant', kind: 'site_preview', actions: [{label: 'Open your preview', action: 'site.preview', payload: {job_id: venture}}]},
      {id: 'invalid-preview', role: 'assistant', kind: 'site_preview'},
      {id: 'question-recovery', role: 'assistant', kind: 'idea_clarification_ready'},
      {id: 'missing-question', role: 'assistant', actions: [{payload: {question_id: 'question_1'}}]},
    ];
    const before = JSON.stringify(messages), visible = collapseAgentStatuses(messages);
    assert.deepEqual(visible.map(message => message.id), ['answer', 'founder', 'diagram', 'actions', 'confirmation',
      'citations', 'references', 'review', 'beliefs', 'preview', 'question-recovery', 'missing-question']);
    assert.equal(visible[0].body_md, `Your next move for ${audience}.`);
    assert.equal(JSON.stringify(messages), before);
  }
});

test('payload-only inbox briefings survive message filtering only with valid data and explicit consent flag across domains', () => {
  const {collapseAgentStatuses} = harness().exports;
  for (const [venture, audience] of domains) {
    const message = {id: venture, role: 'assistant', kind: 'inbox_briefing', citations: [{inbox_briefing: {
      items: [{id: venture, from: 'member@example.org', subject: audience, reason: 'You received a question.', category: 'important', actions: []}],
      filtered: {newsletters: 0, notifications: 0}}}]};
    assert.deepEqual(collapseAgentStatuses([message]), []);
    assert.deepEqual(collapseAgentStatuses([message], false), []);
    assert.deepEqual(collapseAgentStatuses([message], true), [message]);
    assert.deepEqual(collapseAgentStatuses([{...message, citations: undefined}], true), []);
    assert.deepEqual(collapseAgentStatuses([{...message, citations: [{inbox_briefing: {...message.citations[0].inbox_briefing, items: [{}]}}]}], true), []);
  }
});

test('failed questions point you to chat without a competing retry in unrelated domains', async testContext => {
  for (const [venture, audience] of domains) {
    await testContext.test(audience, () => {
      for (const status of ['failed', 'failed_dead', 'poison']) {
        const trayHarness = harness(), calls = [], failed = {...job(venture, 'idea.clarify_turn', 0, status), label: audience};
        const props = {jobs: [failed], now: Date.now(), onDismiss() {},
          onAction: (action, payload) => calls.push([action, payload]),
          progress: [{...failed.progress, job_id: failed.id, step_label: `Still preparing ${audience}`}]};
        let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
        nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
        tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
        assert.match(text(tree), /Your questions need you/);
        assert.match(text(tree), /Continue in the chat to choose your next step\./);
        assert.doesNotMatch(text(tree), /Still preparing|Review the saved result|confirm a safe retry|Questions needs you/);
        nodes(tree).find(node => node.props?.className === 'chip').props.onClick();
        tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
        assert.match(text(tree), /Next: Continue in the chat to choose your next step\./);
        assert.doesNotMatch(text(tree), /Now:/);
        assert.equal(nodes(tree).filter(node => node.type === 'button' && text(node) === 'Retry').length, 0);
        assert.deepEqual(calls, []);
      }
    });
  }
});

test('two ventures render concurrent agents, elapsed time, real stages, completion, failure, dismissal and guarded retry', async () => {
  for (const [venture, audience, instruction] of domains) {
    const h = harness(), calls = [], dismissed = [];
    const kinds = ['research.deep', 'plan.compile', 'gtm.journey.prepare', 'pagebuilder.build', 'experiment.run', 'device.work_order'];
    const jobs = kinds.map((kind, index) => job(venture, kind, index));
    const props = {jobs, now: Date.parse('2026-10-27T10:01:05Z'), onDismiss: id => dismissed.push(id),
      onAction: async (action, payload) => {calls.push([action, payload]); await tick(); return false;},
      progress: [{job_id: jobs[2].id, state: 'running', percent: 60, step_label: `Checking fit for ${audience}`,
        started_at: jobs[2].started_at, steps: {planning: {completed: 1, planned: 1}, searching: {completed: 2, planned: 4},
          future_step_id: {completed: 0, planned: 1}}}]};
    let tree = h.render(h.exports.AgentsTray, props);
    assert.equal(nodes(tree).find(node => node.type === 'aside').props['aria-label'], 'Your tasks');
    nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
    tree = h.render(h.exports.AgentsTray, props);
    assert.equal(nodes(tree).filter(node => node.props?.className === 'chip').length, 6);
    assert.equal(nodes(tree).filter(node => node.props?.['aria-label'] === 'Running').length, 6);
    assert.equal(nodes(tree).filter(node => node.type === 'time').length, 6);
    assert.match(text(tree), /1:05/); assert.ok(text(tree).includes(audience));
    nodes(tree).filter(node => node.props?.className === 'chip')[2].props.onClick();
    tree = h.render(h.exports.AgentsTray, props);
    assert.match(text(tree), /Searching public sources: 2\/4 steps processed/);
    assert.match(text(tree), /Next: Searching public sources/);
    assert.match(text(tree), /Search step: 0\/1 steps processed/);
    assert.doesNotMatch(text(tree), /future_step_id/);
    assert.equal(nodes(tree).filter(node => node.props?.className === 'chip' && node.props['aria-expanded'] === true).length, 1);
    jobs[0] = {...jobs[0], status: 'done', finished_at: '2026-10-27T10:00:45Z'};
    jobs[1] = {...jobs[1], status: 'failed', label: instruction};
    tree = h.render(h.exports.AgentsTray, props);
    assert.match(text(tree), /Research ready/); assert.match(text(tree), /Your planning needs you/);
    assert.equal(nodes(tree).filter(node => node.props?.['aria-label'] === 'Running').length, 4);
    assert.ok(nodes(tree).some(node => node.props?.className?.includes('failed')));
    assert.match(text(tree), /0:45/);
    const retry = nodes(tree).find(node => node.type === 'button' && text(node) === 'Retry');
    const first = retry.props.onClick(); await retry.props.onClick(); await first;
    assert.deepEqual(calls, [['job.retry', {kind: 'plan.compile', job_id: jobs[1].id}]]);
    assert.ok(nodes(h.render(h.exports.AgentsTray, props)).some(node => node.type === 'error-notice' && node.props?.error));
    nodes(tree).filter(node => node.props?.className === 'chip')[0].props.onClick();
    tree = h.render(h.exports.AgentsTray, props);
    nodes(tree).find(node => node.type === 'button' && text(node) === 'Dismiss after review').props.onClick();
    assert.deepEqual(dismissed, [jobs[0].id]);
    jobs[1] = {...jobs[1], status: 'cancelled'};
    tree = h.render(h.exports.AgentsTray, props);
    assert.match(text(tree), /Planning stopped/);
    assert.equal(nodes(tree).some(node => node.type === 'button' && text(node) === 'Retry'), false);
    assert.equal(h.render(h.exports.AgentsTray, {...props, jobs: []}), null);
  }
});

test('existing job and Mac endpoints retain concurrent progress, reconcile results once, and reject stale venture responses', async t => {
  const previous = {window: global.window, document: global.document};
  t.after(() => Object.assign(global, previous));
  for (const [venture, audience, instruction] of domains) {
    let poll, phase = 0, finished = 0, blockedResolve;
    const calls = [], jobs = [job(venture, 'research.deep', 0), job(venture, 'plan.compile', 1), job(venture, 'pagebuilder.build', 2)];
    global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
    const storage = new Map();
    global.window = {setInterval: callback => {poll = callback; return 1;}, clearInterval() {}, addEventListener() {}, removeEventListener() {},
      sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)}};
    const h = harness({'@/lib/client': {verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async url => {
        calls.push(url);
        if (url === `/api/ventures/${venture}`) return {ok: true, data: {workspace_id: 'workspace'}};
        if (url.includes('/workspaces/')) {
          assert.equal(new URL(url, 'http://local').searchParams.get('venture_id'), venture);
          if (phase === 3) return await new Promise(resolve => {blockedResolve = resolve;});
          if (phase === 2) return {ok: false, data: null};
          return {ok: true, data: {items: phase ? [] : jobs}};
        }
        if (url.startsWith('/api/jobs/')) {
          const original = jobs.find(item => url.endsWith(item.id)); assert.ok(original);
          return {ok: true, data: {...original, status: original.kind === 'plan.compile' ? 'failed' : 'done'}};
        }
        if (url.includes('/pagebuilder/jobs/')) return {ok: true, data: {job_id: jobs[2].id, stage: 'checking_layout', progress: .8}};
        if (url.endsWith('/device-work-orders')) return {ok: true, data: {orders: [{id: 'mac_order', state: phase ? 'stopped' : 'claimed'}]}};
        if (url.endsWith('/device-work-orders/mac_order')) return {ok: true, data: {id: 'mac_order', venture_id: venture,
          state: phase ? 'stopped' : 'claimed', claimed_at: jobs[0].started_at, expires_at: '2026-10-28T10:00:00Z', manifest: {title: instruction}}};
        throw new Error(url);
      }}, '@/lib/desktop': {isDesktopShell: () => true, desktopInvoke: async () => ({order_id: 'mac_order',
        state: phase ? 'stopped' : 'running', task_verified: true, effect_outcome_unconfirmed: phase === 1,
        events: [{at: Date.now(), text: `Checking the result for ${audience}`}], message: instruction})}});
    const complete = async () => {finished++;};
    h.hook(venture, true, complete); h.start(); await tick();
    let result = h.hook(venture, true, complete);
    assert.equal(result.jobs.length, 4); assert.equal(result.error, '');
    assert.equal(result.jobs.find(item => item.kind === 'pagebuilder.build').progress.step_label, 'Checking the page');
    assert.ok(result.jobs.find(item => item.kind === 'device.work_order').progress.step_label.includes(audience));
    phase = 1; poll(); await tick();
    result = h.hook(venture, true, complete);
    assert.equal(result.jobs.length, 4); assert.equal(result.jobs.find(item => item.id === 'mac_order').status, 'failed', 'uncertain native effects never become a success');
    assert.equal(finished, 2, 'durable and native transitions each trigger one saved-history refresh');
    poll(); await tick(); assert.equal(finished, 2);
    result.dismiss(jobs[0].id); assert.equal(h.hook(venture, true, complete).jobs.length, 3);
    phase = 2; poll(); await tick();
    result = h.hook(venture, true, complete); assert.equal(result.jobs.length, 3); assert.ok(result.error);
    phase = 3; poll(); await tick();
    assert.ok(blockedResolve);
    const other = domains.find(([id]) => id !== venture)[0];
    assert.equal(h.hook(other, false, complete).jobs.length, 0);
    h.start(); blockedResolve({ok: true, data: {items: jobs}}); await tick();
    assert.equal(h.hook(other, false, complete).jobs.length, 0);
    assert.equal(finished, 2); h.cleanup();
    const restored = harness({'@/lib/client': {verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async () => ({ok: false, data: null})}});
    restored.hook(venture, true, complete); restored.start(); await tick();
    assert.equal(restored.hook(venture, true, complete).jobs.length, 3, 'unseen chips survive reopening while acknowledged chips stay cleared');
    restored.cleanup();
    assert.equal(calls.some(url => /retry|confirm|stop/.test(url)), false, 'polling never admits paid work');
  }
});

test('two unrelated domains keep one started line per task, real result messages, useful reports and confirmation safety', () => {
  const {collapseAgentStatuses} = harness().exports;
  for (const [venture, audience, instruction] of domains) {
    const messages = [
      {id: 'research-start', role: 'assistant', body_md: 'Research is queued. You can return shortly.'},
      {id: 'founder', role: 'user', body_md: instruction},
      {id: 'plan-start', role: 'assistant', body_md: 'Plan generation is running. You can continue here.'},
      {id: 'research-status', role: 'assistant', body_md: 'Research status: running.'},
      {id: 'saved-research-status', role: 'assistant', body_md: 'Your saved research:\nResearch status: running.'},
      {id: 'research-result', role: 'assistant', body_md: `Research is ready. Review the findings for ${audience}.`, actions: [{href: `/v/${venture}/research`, label: 'Review your research'}]},
      {id: 'plan-status', role: 'assistant', body_md: 'Plan generation is running. You can continue here.'},
      {id: 'report', role: 'assistant', body_md: `Your saved research:\nThe teams described running workshops for ${audience}.`},
      {id: 'confirmation', role: 'assistant', body_md: 'Research is queued after you confirm.', actions: [{confirmation: {}}]},
      {id: 'plan-result', role: 'assistant', body_md: 'Your plan is ready. Review it before approving any work.'},
    ];
    const before = JSON.stringify(messages), visible = collapseAgentStatuses(messages);
    assert.deepEqual(visible.map(message => message.id), ['research-start', 'founder', 'plan-start', 'research-result', 'report', 'confirmation', 'plan-result']);
    assert.equal(visible.filter(message => message.body_md.startsWith('Started ')).length, 2);
    assert.equal(JSON.stringify(messages), before);
    assert.equal(visible.find(message => message.id === 'research-result'), messages[5]);
    assert.equal(visible.find(message => message.id === 'confirmation'), messages[8]);
  }
  const dock = readFileSync(__dirname + '/ChatDock.tsx', 'utf8');
  assert.match(dock, /useAgentJobs\(ventureId \|\| "", watchWork, onReloadHistory\)/);
  assert.match(dock, /<AgentsTray/);
  assert.match(dock, /const blocked = !!disabled \|\| composer.pending/);
  assert.doesNotMatch(dock, /disabled=\{[^}]*agents\./);
});

test('both ventures have no edit or video header buttons, while settings and composer voice stay reachable', () => {
  for (const [venture, audience, instruction] of domains) {
    const h = harness({'./useVentureChat': {useVentureChat: () => ({managerName: 'Guide', historyReady: true,
      messages: [{id: venture, role: 'assistant', body_md: instruction}], conversationItems: [], archivedConversationItems: []})},
      './useChatHistory': {useChatHistory() {}}, './CharlieDock': {CharlieMascot: 'face'}, './ChatDock': {ChatDock: 'dock'},
      '@/lib/founderUI': {FounderUIContext: {}}, '@/lib/chatPrefill': {PREFILL_ACTION: 'prefill'}});
    const rendered = nodes(h.render(h.load('VentureChatLauncher.tsx').VentureChatLauncher, {ventureId: venture, size: {}, onSizeChange() {}}));
    assert.equal(rendered.some(node => ['New chat', 'Talk to Charlie live'].includes(node.props?.['aria-label'])), false);
    assert.ok(rendered.some(node => node.props?.className === 'titleBtn'));
    assert.equal(rendered.find(node => node.type === 'dock').props.ventureId, venture);
    assert.ok(audience);
    const launcher = readFileSync(__dirname + '/VentureChatLauncher.tsx', 'utf8');
    assert.doesNotMatch(launcher, /styles\.toolbar|Talk to Charlie live|M3\.2 11\.6|<rect x="2"/);
    assert.match(launcher, /supymem:open-settings/);
    assert.match(readFileSync(__dirname + '/ChatDock.tsx', 'utf8'), /<VoiceMic/);
    const css = readFileSync(__dirname + '/AgentsTray.module.css', 'utf8');
    assert.match(css, /position: relative/); assert.match(css, /background: #fff/);
    assert.match(css, /prefers-reduced-motion/); assert.match(css, /focus-visible/);
  }
});

test('the tray starts as one collapsed pill, including mixed terminal states and status errors', () => {
  for (const [venture, audience] of domains) {
    const trayHarness = harness(), jobs = ['research.deep', 'plan.compile', 'pagebuilder.build', 'idea.clarify_turn', 'experiment.run']
      .map((kind, index) => ({...job(venture, kind, index, index < 2 ? 'running' : 'done'), label: audience}));
    const props = {jobs, now: Date.now(), onDismiss() {}};
    let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    const pill = nodes(tree).find(node => node.type === 'button');
    assert.equal(pill.props['aria-expanded'], false);
    assert.equal(nodes(tree).filter(node => node.type === 'button').length, 1);
    assert.match(text(pill), /^2 running · 3 done/);
    assert.equal(nodes(tree).some(node => node.props?.className === 'items' || node.props?.className === 'chip'), false);
    jobs[2].status = 'failed'; jobs[3].status = 'cancelled';
    tree = trayHarness.render(trayHarness.exports.AgentsTray, {...props, error: `Could not check the task for ${audience}`});
    assert.match(text(tree), /2 running · 1 done · 1 needs you · 1 stopped · Check status/);
    assert.equal(trayHarness.render(trayHarness.exports.AgentsTray, {...props, jobs: []}), null);
    tree = trayHarness.render(trayHarness.exports.AgentsTray, {...props, jobs: [], error: 'Could not check your tasks'});
    assert.match(text(tree), /Check your tasks · Check status/);
  }
});

test('expanded progress is capped inside the header with its own accessible scroll region', () => {
  for (const [venture, audience] of domains) {
    const trayHarness = harness(), props = {jobs: ['research.deep', 'plan.compile', 'pagebuilder.build', 'experiment.run', 'device.work_order']
      .map((kind, index) => ({...job(venture, kind, index), label: audience})), now: Date.now(), onDismiss() {}};
    let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    const region = nodes(tree).find(node => node.props?.className === 'items');
    assert.equal(region.props.role, 'region'); assert.equal(region.props.tabIndex, 0);
    assert.equal(region.props['aria-label'], 'Task progress');
    assert.equal(nodes(region).filter(node => node.props?.className === 'chip').length, 5);
    const css = readFileSync(__dirname + '/AgentsTray.module.css', 'utf8');
    const tray = css.match(/\.tray \{([^}]+)\}/)[1], items = css.match(/\.items \{([^}]+)\}/)[1];
    assert.match(tray, /position: relative/); assert.match(tray, /margin: 0/);
    assert.match(tray, /max-height: 320px/);
    assert.doesNotMatch(tray, /align-self|flex: none/);
    assert.match(items, /max-height: 280px/); assert.match(items, /min-height: 0/);
    assert.match(items, /overflow-y: auto/); assert.match(items, /overscroll-behavior: contain/);
    assert.match(css, /\.summary:focus-visible/); assert.match(css, /prefers-reduced-motion/);
  }
});

test('successful chips clear 30 seconds after first shown, not completion, and timers survive polling then clean up', testContext => {
  testContext.mock.timers.enable({apis: ['Date', 'setTimeout'], now: Date.parse('2026-10-27T11:00:00Z')});
  const previous = global.window;
  global.window = {setTimeout, clearTimeout};
  testContext.after(() => {global.window = previous;});
  for (const [venture, audience] of domains) {
    const trayHarness = harness(), dismissed = [], jobs = ['done', 'failed', 'cancelled', 'running']
      .map((status, index) => ({...job(venture, 'idea.clarify_turn', index, status), label: audience,
        finished_at: '2026-10-27T10:00:01Z'}));
    const props = {jobs, now: Date.now(), onDismiss: id => dismissed.push(id)};
    trayHarness.render(trayHarness.exports.AgentsTray, props); trayHarness.start();
    testContext.mock.timers.tick(29_000); assert.deepEqual(dismissed, []);
    trayHarness.render(trayHarness.exports.AgentsTray, {...props, jobs: jobs.map(item => ({...item}))}); trayHarness.start();
    testContext.mock.timers.tick(999); assert.deepEqual(dismissed, []);
    testContext.mock.timers.tick(1); assert.deepEqual(dismissed, [jobs[0].id]);
    const next = {...props, jobs: [jobs[3], {...job(venture, 'plan.compile', 10, 'done'), label: audience}]};
    trayHarness.render(trayHarness.exports.AgentsTray, next); trayHarness.start();
    testContext.mock.timers.tick(10_000);
    trayHarness.render(trayHarness.exports.AgentsTray, {...next, jobs: []}); trayHarness.start();
    testContext.mock.timers.tick(30_000); assert.equal(dismissed.length, 1, 'removed chips do not dismiss a new scope');
    trayHarness.render(trayHarness.exports.AgentsTray, next); trayHarness.start(); trayHarness.cleanup();
    testContext.mock.timers.tick(30_000); assert.equal(dismissed.length, 1, 'unmount cancels outstanding dismissals');
  }
});

test('failed and stopped tasks retain retry and dismissal after successful tasks auto-clear', async testContext => {
  testContext.mock.timers.enable({apis: ['Date', 'setTimeout'], now: Date.parse('2026-10-27T11:00:00Z')});
  const previous = global.window;
  global.window = {setTimeout, clearTimeout};
  testContext.after(() => {global.window = previous;});
  for (const [venture, audience, instruction] of domains) {
    const trayHarness = harness(), dismissed = [], calls = [];
    const jobs = ['done', 'succeeded', 'failed', 'failed_dead', 'poison', 'cancelled', 'running']
      .map((status, index) => ({...job(venture, 'plan.compile', index, status), label: `${audience}: ${instruction}`}));
    const props = {jobs, now: Date.now(), onDismiss: id => {
      dismissed.push(id); props.jobs = props.jobs.filter(item => item.id !== id);
    }, onAction: async (action, payload) => {calls.push([action, payload]); return true;}};
    let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props); trayHarness.start();
    testContext.mock.timers.tick(60_000);
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props); trayHarness.start();
    assert.deepEqual(dismissed, jobs.slice(0, 2).map(item => item.id));
    assert.deepEqual(props.jobs.map(item => item.id), jobs.slice(2).map(item => item.id));
    assert.equal(nodes(tree).filter(node => node.type === 'button' && text(node) === 'Retry').length, 3);
    const changed = {...job(venture, 'plan.compile', 10, 'done'), label: instruction};
    props.jobs = [...props.jobs, changed];
    trayHarness.render(trayHarness.exports.AgentsTray, props); trayHarness.start();
    testContext.mock.timers.tick(10_000);
    props.jobs = props.jobs.map(item => item.id === changed.id ? {...item, status: 'failed'} : item);
    trayHarness.render(trayHarness.exports.AgentsTray, props); trayHarness.start();
    testContext.mock.timers.tick(60_000);
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    assert.deepEqual(dismissed, jobs.slice(0, 2).map(item => item.id), 'a changed outcome cancels its successful-task timer');
    assert.equal(nodes(tree).filter(node => node.type === 'button' && text(node) === 'Retry').length, 4);
    await nodes(tree).find(node => node.type === 'button' && text(node) === 'Retry').props.onClick();
    assert.deepEqual(calls, [['job.retry', {kind: 'plan.compile', job_id: jobs[2].id}]]);
    nodes(tree).find(node => node.props?.className === 'chip' && text(node).includes('Planning stopped')).props.onClick();
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    nodes(tree).find(node => node.type === 'button' && text(node) === 'Dismiss after review').props.onClick();
    assert.deepEqual(dismissed, [...jobs.slice(0, 2).map(item => item.id), jobs[5].id]);
    assert.ok(props.jobs.some(item => item.id === jobs[2].id));
    trayHarness.cleanup();
  }
});

test('equal labels merge into one chip without dropping per-task details, review links or dismissal', () => {
  for (const [venture, audience, instruction] of domains) {
    const trayHarness = harness(), dismissed = [], jobs = [0, 1].map(index => ({...job(venture, 'idea.clarify_turn', index, 'done'),
      label: `${audience}: ${instruction} ${index}`, steps: [`Checking ${audience} ${index}`], reviewHref: `/v/${venture}/results/${index}`}));
    jobs.push(job(venture, 'idea.clarify_turn', 2));
    const before = JSON.stringify(jobs), props = {jobs, now: Date.now(), onDismiss: id => dismissed.push(id)};
    let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    assert.equal(nodes(tree).filter(node => node.props?.className === 'chip').length, 2);
    assert.equal(nodes(tree).filter(node => node.type === 'strong' && text(node) === 'Questions ready').length, 1);
    nodes(tree).find(node => node.props?.className === 'chip').props.onClick();
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    for (const item of jobs.slice(0, 2)) {
      assert.ok(text(tree).includes(item.label)); assert.ok(text(tree).includes(item.steps[0]));
      assert.ok(nodes(tree).some(node => node.type === 'a' && node.props.href === item.reviewHref));
    }
    nodes(tree).filter(node => node.type === 'button' && text(node) === 'Dismiss after review').forEach(node => node.props.onClick());
    assert.deepEqual(dismissed, jobs.slice(0, 2).map(item => item.id)); assert.equal(JSON.stringify(jobs), before);
    const progress = jobs.slice(0, 2).map((item, index) => ({job_id: item.id,
      steps: {[`checking ${audience} ${index}`]: {completed: index + 1, planned: 3}}}));
    tree = trayHarness.render(trayHarness.exports.AgentsTray, {...props, progress});
    assert.ok(text(tree).includes(`checking ${audience} 0: 1/3 steps processed`));
    assert.ok(text(tree).includes(`checking ${audience} 1: 2/3 steps processed`));
  }
});

test('kind labels and progress use plain words without leaking private details in either domain', () => {
  for (const [venture, audience] of domains) {
    const trayHarness = harness(), jobs = ['chat.recovery', 'content.draft', 'simulation.rehearse', 'gtm.god.run', 'unknown.kind']
      .map((kind, index) => ({...job(venture, kind, index, 'queued'), label: `${audience} company job`,
        steps: ['Waiting for a runner', 'Company job started'], progress: {step_label: 'Waiting for a worker'}}));
    jobs[1].steps.push('api_key=secret_value');
    const props = {jobs, now: Date.now(), onDismiss() {}};
    let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    assert.match(text(tree), /Queued: reply/); assert.match(text(tree), /Queued: content/);
    assert.match(text(tree), /Queued: message check/); assert.match(text(tree), /Queued: customer search/);
    assert.match(text(tree), /Queued/);
    for (const chip of nodes(tree).filter(node => node.props?.className === 'chip')) {
      chip.props.onClick(); tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
      assert.doesNotMatch(text(tree), /\b(?:jobs?|workers?|runners?)\b|—|api_key=secret_value/i);
    }
    tree = trayHarness.render(trayHarness.exports.AgentsTray, {...props, jobs: jobs.map(item => ({...item, status: 'running'}))});
    assert.match(text(tree), /Preparing your reply/); assert.match(text(tree), /Preparing your content/);
    assert.match(text(tree), /Checking your message/); assert.match(text(tree), /Finding customers/);
  }
});

test('queued work explains its wait without claiming stale progress or elapsed running time in either domain', () => {
  for (const [venture, audience] of domains) {
    const waits = [
      ['queued', 'Starting soon. Your work is saved but has not started yet.'],
      ['retry_backoff', 'Starting soon. Waiting before trying again after a temporary problem.'],
      ['usage_limit', 'Starting soon. Your usage limit needs time to reset.'],
      ['scheduled', 'Starting soon. Your work is scheduled for later.'],
      ['cancel_requested', 'Stopping. You asked to stop this work.'],
      ['paused', 'Paused. Resume this work when you are ready.'],
    ];
    for (const [reason, label] of waits) {
      const trayHarness = harness(), waiting = {...job(venture, 'research.deep', 0, reason === 'retry_backoff' || reason === 'usage_limit' ? 'retry_wait' : 'scheduled'),
        label: audience, wait: {reason, label, until: new Date(Date.now() + 600_000).toISOString()}};
      const active = job(venture, 'research.deep', 1), props = {jobs: [waiting, active], now: Date.now(), onDismiss() {},
        progress: [{job_id: waiting.id, step_label: 'Searching stale sources', started_at: '2026-10-27T10:00:00Z'}]};
      let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
      assert.match(text(tree), reason === 'paused' ? /1 running · 1 paused/ : reason === 'cancel_requested' ? /1 running · 1 stopping/ : /1 running · 1 scheduled/);
      nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
      tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
      const chips = nodes(tree).filter(node => node.props?.className === 'chip');
      assert.equal(chips.length, 2);
      assert.ok(text(chips[0]).includes(label.replace('Starting soon', 'Queued')));
      assert.equal(nodes(chips[0]).filter(node => node.type === 'time').length, 0);
      assert.equal(nodes(chips[1]).filter(node => node.type === 'time').length, 1);
      if (reason === 'paused') assert.equal(nodes(chips[0]).filter(node => node.props?.role === 'status').length, 0);
      else assert.match(nodes(chips[0]).find(node => node.props?.role === 'status').props['aria-label'], reason === 'cancel_requested' ? /^Stopping$/ : /^Scheduled for /);
      assert.doesNotMatch(text(chips[0]), /Searching stale sources|Checking sources/);
      chips[0].props.onClick(); tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
      assert.match(text(tree), /Earlier: Reading saved inputs/);
      assert.ok(nodes(tree).some(node => node.type === 'p' && node.props?.role === 'status' && text(node) === label.replace('Starting soon', 'Queued')));
      assert.doesNotMatch(text(tree), /worker|runner|—/i);
    }
  }
});

test('idea work shows its real schedule and never promises an overdue start in either domain', () => {
  for (const [venture, audience] of domains) {
    const now = Date.now(), due = now + 3_600_000;
    const scheduled = new Date(due).toISOString(), old = new Date(now - 10_800_000).toISOString();
    for (const kind of ['idea.recon', 'idea.clarify_turn', 'idea.generate_candidates', 'idea.validate', 'idea.page_refresh']) {
      const trayHarness = harness(), waiting = {...job(venture, kind, 0, 'scheduled'), label: audience,
        created_at: old, scheduled_for: scheduled,
        wait: {reason: 'scheduled', label: 'Starting soon. Your work is scheduled for later.', until: scheduled}};
      const props = {jobs: [waiting], now, onDismiss() {}};
      let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
      assert.match(text(tree), /1 scheduled/);
      nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
      tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
      assert.ok(text(tree).includes(`Scheduled for ${new Date(due).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'})}`));
      tree = trayHarness.render(trayHarness.exports.AgentsTray, {...props, now: due});
      assert.match(text(tree), /1 queued/);
      tree = trayHarness.render(trayHarness.exports.AgentsTray, {...props, now: due + 10_800_000});
      assert.match(text(tree), /1 delayed/); assert.match(text(tree), /Delayed: /);
      assert.doesNotMatch(text(tree), /Starting soon|Scheduled for|running/i);
      assert.equal(nodes(tree).find(node => node.props?.className === 'spinner').props['aria-label'], 'Delayed');
    }
  }
});

test('overdue eligible work is distinct from intentional future waits in both domains', () => {
  for (const [venture, audience] of domains) {
    const now = Date.now(), old = new Date(now - 600_000).toISOString(), future = new Date(now + 600_000).toISOString();
    const trayHarness = harness(), waiting = {...job(venture, 'research.deep', 0, 'scheduled'), label: audience,
      created_at: old, scheduled_for: old, wait: {reason: 'queued', label: 'Starting soon. Your work is saved but has not started yet.', until: old}};
    const props = {jobs: [waiting], now, onDismiss() {}};
    let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    assert.match(text(tree), /queued longer than expected/);
    waiting.status = 'retry_wait'; waiting.wait = {reason: 'retry_backoff',
      label: 'Starting soon. Waiting before trying again after a temporary problem.', until: future};
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    assert.match(text(tree), /temporary problem/); assert.doesNotMatch(text(tree), /longer than expected/);
    assert.doesNotMatch(text(tree), /worker|runner|—/i);
  }
});

test('the overlay mounts into the dock or company header, never the transcript, and stays hidden without a header', () => {
  for (const [venture, audience] of domains) {
    for (const mode of ['dock', 'company', 'none']) {
      const header = {tagName: 'DIV'}, dock = {querySelector: selector => {
        assert.equal(selector, ':scope > .header'); return mode === 'dock' ? header : null;
      }, closest: selector => {
        assert.equal(selector, '#supymem-chat-panel');
        return mode === 'company' ? {querySelector: query => {assert.equal(query, ':scope > div'); return header;}} : null;
      }};
      const trayHarness = harness({}, dock), props = {jobs: [{...job(venture, 'research.deep', 0), label: audience}], now: Date.now(), onDismiss() {}};
      const tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
      assert.ok(nodes(tree).some(node => node.props?.hidden));
      assert.equal(nodes(tree).some(node => node.type === 'aside'), mode !== 'none');
      assert.deepEqual(trayHarness.portalTargets, mode === 'none' ? [] : [header]);
    }
  }
});

test('later scoped success dismisses superseded failures permanently in both domains', testContext => {
  const previous = global.window;
  global.window = {setTimeout: () => 1, clearTimeout() {}};
  testContext.after(() => {global.window = previous;});
  for (const [venture, audience] of domains) {
    for (const replacement of ['succeeded', 'done', 'running', 'failed', 'foreign-venture', 'foreign-workspace', 'other-kind', 'older']) {
      const trayHarness = harness(), dismissed = [];
      const failed = {...job(venture, 'experiment.exploration.prepare', 0, 'failed_dead'), workspace_id: 'workspace', label: audience};
      const later = {...job(venture, replacement === 'other-kind' ? 'plan.compile' : failed.kind, 1,
        ['succeeded', 'done', 'running', 'failed'].includes(replacement) ? replacement : 'succeeded'),
        venture_id: replacement === 'foreign-venture' ? 'another-venture' : venture,
        workspace_id: replacement === 'foreign-workspace' ? 'another-workspace' : 'workspace',
        created_at: replacement === 'older' ? '2026-10-27T09:00:00Z' : '2026-10-27T11:00:00Z'};
      const props = {jobs: [failed, later], now: Date.now(), onDismiss: id => {
        dismissed.push(id); props.jobs = props.jobs.filter(item => item.id !== id);
      }};
      const resolved = ['succeeded', 'done'].includes(replacement);
      let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
      if (resolved) assert.doesNotMatch(text(tree), /needs you/);
      else assert.match(text(tree), /needs you/);
      trayHarness.start();
      assert.deepEqual(dismissed, resolved ? [failed.id] : []);
      if (resolved) {
        props.onDismiss(later.id);
        tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
        assert.equal(tree, null);
      }
    }
  }
});

test('exploration retries target the exact saved task and suppress duplicate clicks in both domains', async () => {
  for (const [venture, audience] of domains) {
    const trayHarness = harness(), calls = [];
    const failed = {...job(venture, 'experiment.exploration.prepare', 0, 'failed_dead'), label: audience};
    const props = {jobs: [failed], now: Date.now(), onDismiss() {},
      onAction: async (action, payload) => {calls.push([action, payload]); await tick(); return true;}};
    let tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    nodes(tree).find(node => node.props?.className === 'summary').props.onClick();
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    nodes(tree).find(node => node.props?.className === 'chip').props.onClick();
    tree = trayHarness.render(trayHarness.exports.AgentsTray, props);
    const button = nodes(tree).find(node => node.type === 'button' && text(node) === 'Retry');
    const first = button.props.onClick(); await button.props.onClick(); await first;
    assert.deepEqual(calls, [['job.retry', {kind: failed.kind, job_id: failed.id}]]);
  }
});

test('resolved failures disappear from saved tray state even when success finished between polls in both domains', async testContext => {
  const previous = {window: global.window, document: global.document};
  testContext.after(() => Object.assign(global, previous));
  for (const [venture, audience] of domains) {
    const failed = {...job(venture, 'experiment.exploration.prepare', 0, 'failed_dead'), label: audience};
    const storage = new Map(), finished = [];
    global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
    global.window = {setInterval: () => 1, clearInterval() {}, addEventListener() {}, removeEventListener() {},
      sessionStorage: {getItem: key => storage.get(key) || JSON.stringify([failed]), setItem: (key, value) => storage.set(key, value)}};
    const trayHarness = harness({'@/lib/client': {verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async url => {
        if (url === `/api/ventures/${venture}`) return {ok: true, data: {workspace_id: 'workspace'}};
        if (url.includes('/workspaces/')) return {ok: true, data: {items: [], resolved_ids: [failed.id]}};
        if (url.endsWith('/device-work-orders')) return {ok: true, data: {orders: []}};
        throw new Error(url);
      }}});
    trayHarness.hook(venture, true, () => finished.push(true)); trayHarness.start(); await tick();
    const result = trayHarness.hook(venture, true);
    assert.deepEqual(result.jobs, []);
    assert.deepEqual(finished, []);
    assert.equal(storage.size, 1);
    assert.equal([...storage.values()][0], '[]');
    trayHarness.cleanup();
  }
});
