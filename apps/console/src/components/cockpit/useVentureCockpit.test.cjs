const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const text = readFileSync(__dirname + '/useVentureCockpit.ts', 'utf8');
const source = ts.createSourceFile('useVentureCockpit.ts', text, ts.ScriptTarget.Latest, true);
function actual(name, context) {
  let method;
  function find(node) {if (ts.isFunctionDeclaration(node) && node.name?.text === name) method = node.getText(source); ts.forEachChild(node, find);}
  find(source); assert.ok(method);
  vm.runInNewContext(ts.transpileModule(method + `;globalThis.run = ${name};`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
  return context.run;
}

test('your local builder choice sends your saved page brief to one connected tool', async () => {
  for (const domain of ['Fleet purchasing', 'Home baking']) for (const adapter of ['codex', 'claude', 'cursor', 'opencode', 'cloud']) for (const ready of [true, false]) {
    const calls = [], routes = [], errors = [];
    const context = {ventureId: domain, builder: {prompt: `Build a landing page for ${domain}.`},
      isLocalConsole: () => true, builderDispatch: {current: false}, builderOrder: {current: null},
      builderOwner: {current: domain}, verifiedSessionEpoch: () => 1, verifiedSessionSubject: () => 'owner',
      setSubmitting() {}, setError: value => errors.push(value), setBuilder() {}, routeInChat: path => {routes.push(path); return true;},
      apiErrorCode: result => result.data?.detail?.code,
      window: {location: {assign: assert.fail}, setTimeout() {}}, loadBuilder: assert.fail, refreshOnboarding: assert.fail,
      api: async (path, init) => {calls.push({path, body: JSON.parse(init.body)}); return ready
        ? {ok: true, data: {id: 'order-a', venture_id: domain}}
        : {ok: false, status: 409, data: {detail: {code: 'build_tool_needs_setup'}}};}};
    const run = actual('chooseBuilder', context); await run(adapter); if (ready) await run(adapter);
    assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/coding/dispatch');
    assert.equal(calls[0].body.prompt, context.builder.prompt);
    assert.deepEqual(calls[0].body.prefs, adapter === 'cloud' ? undefined : [adapter]);
    assert.equal(routes.length, ready ? 1 : 0);
    if (!ready) assert.match(errors.at(-1), /Settings > Keys and connections > Coding tools/);
  }
});

test('your local completion checks the coding receipt rather than approving arbitrary page files', async () => {
  for (const domain of ['Fleet purchasing', 'Home baking']) for (const verified of [true, false]) for (const processesStopped of [true, false]) {
    const calls = [], errors = [], updates = [];
    const context = {ventureId: domain, isLocalConsole: () => true, finishingRef: {current: false},
      builderOrder: {current: {id: 'order-a', ventureId: domain, epoch: 1, subject: 'owner'}}, builderOwner: {current: domain},
      verifiedSessionEpoch: () => 1, verifiedSessionSubject: () => 'owner',
      title: domain, setSubmitting() {}, setError: value => errors.push(value), setBuilder: () => updates.push('checked'),
      refreshOnboarding: async () => updates.push('refresh'), uploadLocalLanding: assert.fail,
      window: {setTimeout() {}}, api: async path => {calls.push(path); return {ok: true, data: {
        id: 'order-a', venture_id: domain, state: 'stopped', native_stop: {task_verified: verified, processes_stopped: processesStopped}}};}};
    await actual('finishBuilderSession', context)();
    assert.deepEqual(calls, [`/api/ventures/${encodeURIComponent(domain)}/device-work-orders/order-a`]);
    assert.equal(updates.length, verified && processesStopped ? 2 : 0);
    if (!verified || !processesStopped) assert.match(errors.at(-1), /Open Builds/);
  }
});

test('your local hydration never imports unchecked legacy tool files', async () => {
  for (const domain of ['Fleet purchasing', 'Home baking']) {
    await actual('uploadLocalLanding', {isLocalConsole: () => true})(domain);
  }
  assert.ok(/if \(isLocalConsole\(\)\) return;/.test(text), 'Local hydration must leave ingestion to the checked order');
});
