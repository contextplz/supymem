const assert = require('node:assert/strict'), test = require('node:test');
const {readFileSync} = require('node:fs'), vm = require('node:vm'), ts = require('typescript');
const source = readFileSync(__dirname + '/ChatDock.tsx', 'utf8');
const tree = ts.createSourceFile('ChatDock.tsx', source, ts.ScriptTarget.Latest, true);
const compile = source => ts.transpileModule(source, {compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
}}).outputText;
let helpers = '', polling, rendering, visibleMessages, allMessages, linkActions;
function find(node) {
  if (ts.isFunctionDeclaration(node) && ['collapseSearchProgress', 'searchProgressFor'].includes(node.name?.text)) helpers += node.getText(tree) + '\n';
  if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect' && node.getText(tree).includes('/chat/work')) polling = node.arguments[0].getText(tree);
  if (ts.isConditionalExpression(node) && node.condition.getText(tree) === 'searchProgress') rendering = node.getText(tree);
  if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'visibleMessages') visibleMessages = node.initializer.getText(tree);
  if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'allMessages') allMessages = node.initializer.getText(tree);
  if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'linkActions') linkActions = node.initializer.getText(tree);
  ts.forEachChild(node, find);
}
find(tree);
const jsx = (type, props) => typeof type === 'function' ? type(props) : ({type, props});
const agentSource = ts.createSourceFile('AgentsTray.tsx', readFileSync(__dirname + '/AgentsTray.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
const collapse = agentSource.statements.find(node => ts.isFunctionDeclaration(node) && node.name.text === 'collapseAgentStatuses').getText(agentSource);
const context = vm.createContext({exports: {}, parseEmployeeStamp: text => ({text}), require: name => {
  if (name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
  if (name.endsWith('.css')) return {default: {}};
  throw new Error(name);
}});
context.latestCodingAttempts = jobs => jobs;
context.codingTaskKey = () => null;
context.inboxBriefing = {enabled: false};
context.agents = {jobs: []};
context.disabled = false;
context.local = false;
context.InboxBriefing = props => props.children;
context.validInboxBriefing = require('../../lib/inboxBriefing.ts').validInboxBriefing;
vm.runInContext(compile(helpers), context);
vm.runInContext(compile(collapse), context);
vm.runInContext(compile(readFileSync(__dirname + '/../primitives/ResearchProgress.tsx', 'utf8')), context);
context.ResearchProgress = context.exports.ResearchProgress;
const domains = [['warehouse supervisors', 'Schedule loading bays'], ['weekend hikers', 'Find accessible trails']];
const message = (id, job, run, audience, text) => ({id, role: 'assistant', kind: 'gtm_search_progress',
  body_md: text, gtm_search_progress: {job_id: job, run_id: run, audience}});

test('only the newest progress card per job or run renders, without losing results or unrelated history', () => {
  for (const [audience, idea] of domains) {
    const queued = message('queued', 'job-one', 'run-one', audience, idea);
    const runOnly = message('run-only', null, 'run-one', audience, idea);
    const current = message('current', 'job-one', 'run-one', audience, idea);
    const next = message('next', 'job-two', 'run-two', audience, idea);
    const result = {...current, id: 'result', kind: 'gtm_results'};
    const founder = {...current, id: 'founder', role: 'user'};
    const unbound = {...current, id: 'unbound', gtm_search_progress: undefined};
    const messages = [queued, runOnly, founder, current, result, unbound, next];
    const before = JSON.stringify(messages);
    assert.deepEqual(Array.from(context.collapseSearchProgress(messages), item => item.id), ['founder', 'current', 'result', 'unbound', 'next']);
    context.ventureId = 'venture';
    context.messages = messages; context.visibleCount = 4;
    context.allMessages = vm.runInContext(allMessages, context); context.firstVisibleIndex = -1;
    assert.deepEqual(Array.from(vm.runInContext(visibleMessages, context), item => item.id), ['current', 'result', 'unbound', 'next']);
    assert.equal(JSON.stringify(messages), before);
    const jobOnly = message('job-only', 'job-one', null, audience, idea);
    assert.deepEqual(Array.from(context.collapseSearchProgress([queued, jobOnly]), item => item.id), ['job-only']);
  }
});

test('scoped polling shows current customer search work and terminal next steps in chat', async () => {
  for (const [audience, idea] of domains) {
    const queued = message('saved-card', 'job-one', 'run-one', audience, idea + ': Waiting for a worker');
    let response = {items: [{id: 'job-one'}], research_progress: {job_id: 'job-one', run_id: 'run-one',
      state: 'queued', percent: 0, step_label: 'Waiting for a worker', started_at: null}};
    let snapshots = {scope: '', items: []}, timer, cleanup;
    const scope = JSON.stringify(['you', 'venture', 'conversation']);
    Object.assign(context, {local: false, watchWork: true, ventureId: 'venture', onAction() {}, workKey: scope,
      voiceConversationId: null, conversationId: 'conversation', stopEpoch: {current: 0}, activeJobs: {current: {}},
      setSearchSnapshots: update => {snapshots = update(snapshots);},
      api: async path => {assert.equal(path, '/api/ventures/venture/chat/work?conversation_id=conversation'); return {ok: true, data: JSON.parse(JSON.stringify(response))};},
      window: {setTimeout: (poll, interval) => {assert.ok([2000, 15000].includes(interval)); timer = poll; return 1;}, clearTimeout: id => {assert.equal(id, 1);}},
      styles: {}, splitFences: text => [{text}], ChatMessageText: ({text}) => ({type: 'div', props: {dangerouslySetInnerHTML: {__html: text}}}), shownText: queued.body_md,
    });
    cleanup = vm.runInContext(compile(`(${polling})();`), context);
    await new Promise(resolve => setImmediate(resolve));
    const tick = async () => {timer(); await new Promise(resolve => setImmediate(resolve));};
    const render = (card = queued, scopeKey = scope) => {
      context.m = card;
      context.searchProgress = context.searchProgressFor(card, snapshots.scope === scopeKey ? snapshots.items : []);
      vm.runInContext(`(() => {${compile(`exports.renderCard = () => (${rendering});`)}})();`, context);
      return context.exports.renderCard();
    };
    const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
    assert.equal(nodes(render()).some(node => node.type === 'progress'), true);
    assert.match(JSON.stringify(render()), /check progress below/i);
    response.research_progress = {...response.research_progress, state: 'running', percent: 85, step_label: 'Finding contact routes'};
    await tick();
    const live = render(), serialized = JSON.stringify(live);
    assert.equal(snapshots.items[0].step_label, 'Finding contact routes');
    assert.equal(snapshots.items[0].percent, 85);
    assert.match(serialized, /Finding contact routes/);
    assert.match(serialized, /85%/);
    assert.doesNotMatch(serialized, /Waiting for a worker/);
    assert.equal(live.props['aria-live'], 'polite');
    assert.equal(nodes(live).some(node => node.type === 'progress'), true);
    assert.equal(context.activeJobs.current.scope, scope);
    assert.deepEqual(Array.from(context.activeJobs.current.ids), ['job-one']);
    for (const state of ['done', 'failed']) {
      response = {items: [], research_progress: {...response.research_progress, state,
        percent: state === 'done' ? 100 : 85, step_label: state === 'done' ? 'Ready to review' : 'Search stopped'}};
      await tick();
      assert.deepEqual(Array.from(context.activeJobs.current.ids), []); assert.equal(snapshots.items[0].step_label, response.research_progress.step_label);
      assert.match(JSON.stringify(render()), state === 'done' ? /finished.*saved results/ : /stopped.*retry/);
    }
    response = {items: [], research_progress: null}; await tick();
    assert.equal(snapshots.items[0].step_label, 'Search stopped');
    assert.equal(context.searchProgressFor({...queued, gtm_search_progress: {job_id: 'job-one', run_id: 'other-run'}}, snapshots.items), undefined);
    assert.equal(context.searchProgressFor({...queued, kind: 'gtm_results'}, snapshots.items), undefined);
    assert.match(JSON.stringify(render(queued, 'another-scope')), /Waiting for a worker/);
    response = {items: [{id: 'job-two'}], research_progress: {job_id: 'job-two', run_id: 'run-two',
      state: 'queued', percent: 0, step_label: 'Waiting for a worker', started_at: null}};
    await tick();
    assert.equal(snapshots.items.find(item => item.job_id === 'job-one').step_label, 'Search stopped');
    cleanup(); response.research_progress.percent = 95; await tick();
    assert.equal(snapshots.items.find(item => item.job_id === 'job-two').percent, 0);
  }
});

test('hosted work polling waits for each reply and slows down when idle or refused across domains', async () => {
  for (const [audience] of domains) {
    let resolve, calls = 0, timer, cleanup;
    const delays = [];
    Object.assign(context, {local: false, watchWork: true, ventureId: 'venture', onAction() {}, workKey: audience,
      voiceConversationId: null, conversationId: 'conversation', stopEpoch: {current: 0}, activeJobs: {current: {}},
      setSearchSnapshots() {},
      api: () => {calls++; return new Promise(done => {resolve = done;});},
      window: {setTimeout: (callback, delay) => {timer = callback; delays.push(delay); return 1;}, clearTimeout() {},
        setInterval: callback => {timer = callback; return 1;}, clearInterval() {}},
    });
    cleanup = vm.runInContext(compile(`(${polling})();`), context);
    assert.equal(calls, 1);
    timer?.(); await new Promise(done => setImmediate(done));
    assert.equal(calls, 1, 'A slow request must not pile up more work reads');
    resolve({ok: true, data: {items: []}}); await new Promise(done => setImmediate(done));
    assert.equal(delays.at(-1), 15000);
    timer(); resolve({ok: false, status: 403}); await new Promise(done => setImmediate(done));
    assert.equal(delays.at(-1), 15000);
    timer(); resolve({ok: true, data: {items: [{id: 'saved-work'}]}}); await new Promise(done => setImmediate(done));
    assert.equal(delays.at(-1), 2000);
    cleanup(); timer(); assert.equal(calls, 3);
  }
});

test('customer search progress actions request a chat answer, not silent navigation', () => {
  for (const [audience] of domains) {
    Object.assign(context, {m: {body_md: audience, actions: [{label: 'Check progress', action: 'gtm.status'}]},
      home: true, ventureId: 'venture', isQuestionReply: () => false, chatRouteAction: () => [],
      inChatActions: actions => actions, isChatNavigationAction: () => true});
    vm.runInContext(compile(`exports.actions = ${linkActions};`), context);
    const actions = context.exports.actions;
    assert.equal(actions.length, 1);
    assert.equal(actions[0].action, 'gtm.status');
    assert.notEqual(actions[0].navigation, true);
  }
});
