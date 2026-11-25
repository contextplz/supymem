const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const {USAGE_LIMIT_MESSAGE} = require('../../../../lib/credits.ts');

test('checking an uncertain build only unlocks a safe retry or the exact saved receipt', async () => {
  const source = ts.createSourceFile('page.tsx', readFileSync(__dirname + '/page.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
  const methods = [];
  function find(node) {if (ts.isFunctionDeclaration(node) && ['buildReceiptMatches', 'reconcileBuild'].includes(node.name?.text)) methods.push(node.getText(source)); ts.forEachChild(node, find);}
  find(source);
  assert.equal(methods.length, 2);
  for (const ventureId of ['ven_fleet', 'ven_baking']) {
    const saved = {id: 'dwo_attempt', venture_id: ventureId};
    for (const [result, outcome] of [[{ok: true, data: {...saved, venture_id: 'other'}}, null],
      [{ok: true, data: {...saved, id: 'dwo_unrelated'}}, null], [{ok: false, status: 404}, 'retry'],
      [{ok: false, status: 500}, null], [{ok: true, data: saved}, 'saved']]) {
      const attempt = {id: saved.id, epoch: 1, subject: 'owner'};
      const context = {ventureId, attempt: {current: attempt}, dispatch: {current: false},
        currentOwner: () => true, setRetryReady: value => {if (value) context.outcome = 'retry';},
        setRecovered: () => {context.outcome = 'saved';}, setHeld() {}, setInstruction() {}, setError() {},
        api: async path => {assert.equal(path, `/api/ventures/${ventureId}/device-work-orders/${saved.id}`); return result;}};
      vm.runInNewContext(ts.transpileModule(methods.join('\n') + ';globalThis.run = reconcileBuild;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
      await context.run(() => true);
      assert.equal(context.outcome || null, outcome);
      assert.equal(context.attempt.current, outcome === 'saved' ? null : attempt);
    }
  }
});

test('your local build request starts one scoped coding order, not an internal build', async () => {
  const source = ts.createSourceFile('page.tsx', readFileSync(__dirname + '/page.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
  let method;
  let matches;
  function find(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'requestBuild') method = node.getText(source);
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'buildReceiptMatches') matches = node.getText(source);
    ts.forEachChild(node, find);
  }
  find(source); assert.ok(method, 'Your local build screen needs a working build action');
  const key = 'dwo_12345678123442348234123456789012';
  for (const domain of ['Fleet purchasing', 'Home baking']) for (const result of [
    {ok: true, data: {id: key, venture_id: domain}},
    {ok: false, status: 409, data: {detail: {code: 'build_tool_needs_setup'}}},
    {ok: true, data: {id: 'order-a', venture_id: 'another-venture'}},
  ]) {
    const calls = [], routes = [], errors = [];
    const context = {ventureId: domain, instruction: `Build my app for ${domain}.`, dispatch: {current: false}, attempt: {current: null},
      retryReady: false, currentOwner: () => true, crypto: {randomUUID: () => '12345678-1234-4234-8234-123456789012'},
      owner: {current: domain}, ensureSession: async () => {}, verifiedSessionEpoch: () => 1,
      verifiedSessionSubject: () => 'owner', apiErrorCode: value => value.data?.detail?.code,
      apiFailure: () => 'Open Builds and refresh your saved work.', safeLoopText: value => value,
      setBusy() {}, setHeld() {}, setRetryReady() {}, setRecovered() {}, setError: value => errors.push(value), setInstruction() {},
      routeInChat: path => {routes.push(path); return true;}, window: {location: {assign: assert.fail}},
      api: async (path, init) => {calls.push({path, body: JSON.parse(init.body)}); return result;}};
    vm.runInNewContext(ts.transpileModule(`${matches}\n${method};globalThis.run = requestBuild;`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    await context.run();
    assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/coding/dispatch');
    assert.equal(calls[0].body.prompt, context.instruction);
    assert.equal(calls[0].body.kind, 'product');
    assert.equal(calls[0].body.order_id, key);
    assert.equal(routes.length, result.ok && result.data.venture_id === domain ? 1 : 0);
    if (!result.ok) assert.match(errors.at(-1), /Settings > Keys and connections > Coding tools/);
    if (result.ok && result.data.venture_id !== domain) assert.equal(context.attempt.current.id, key);
  }
});

test('your local build screen lists venture-scoped coding orders without hosted builder calls', async () => {
  const source = ts.createSourceFile('page.tsx', readFileSync(__dirname + '/page.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
  let workspace, matches;
  function find(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'LocalBuildWorkspace') workspace = node.getText(source);
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'buildReceiptMatches') matches = node.getText(source);
    ts.forEachChild(node, find);
  }
  find(source);
  assert.ok(workspace, 'The local build screen must not use the hosted workspace');
  for (const domain of ['Factory procurement', 'Family gardening']) {
    let cursor = 0;
    const state = [], effects = [], calls = [], exports = {};
    const context = {exports, AppShell: 'AppShell', Link: 'Link', ErrorNotice: 'ErrorNotice',
      safeLoopText: value => value, codingToolLabel: value => value, ensureSession: async () => {},
      verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => 1,
      useState(initial) {const index = cursor++; if (!(index in state)) state[index] = initial;
        return [state[index], value => {state[index] = typeof value === 'function' ? value(state[index]) : value;}];},
      useRef: initial => ({current: initial}),
      useEffect: effect => effects.push(effect), window: {setInterval: () => 1, clearInterval() {}, addEventListener() {}, removeEventListener() {}},
      api: async url => {calls.push(url); return {ok: true, data: {orders: [
        {id: 'owned-order', state: 'failed', title: domain},
        {id: 'other-order', venture_id: 'another-venture', state: 'claimed', manifest: {title: 'private-fixture', coding_loop_verification: {provider: 'codex'}}},
      ]}};}, require};
    vm.runInNewContext(ts.transpileModule(`${matches}\n${workspace};exports.render = LocalBuildWorkspace;`, {compilerOptions: {
      target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS,
    }}).outputText, context);
    exports.render({ventureId: domain});
    const cleanup = effects.at(-1)();
    await new Promise(resolve => setImmediate(resolve));
    cursor = 0;
    const tree = JSON.stringify(exports.render({ventureId: domain}));
    cleanup();
    assert.deepEqual(calls, [`/api/ventures/${encodeURIComponent(domain)}/device-work-orders`]);
    assert.match(tree, /owned-order|Watch build and open result|Connect your coding tool/);
    assert.match(tree, /Build failed/);
    assert.doesNotMatch(tree, /Ready to start/);
    assert.doesNotMatch(tree, /other-order|private-fixture|credit|estimate|budget/i);
  }
});

test('your requested estimate continues without a second approval in unrelated domains', async () => {
  const source = ts.createSourceFile('page.tsx', readFileSync(__dirname + '/page.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
  let method;
  function visit(node) { if (ts.isFunctionDeclaration(node) && node.name?.text === 'estimate') method = node.getText(source); ts.forEachChild(node, visit); }
  visit(source);
  for (const domain of ['Factory procurement', 'Family gardening']) for (const ready of [true, false]) {
    let status = '';
    const calls = [], context = {USAGE_LIMIT_MESSAGE, appId: domain, approvedPlan: true, eligible: true, plan: {id: 'saved-plan'}, taskId: 'saved-task', active: () => true, currentApp: {current: domain}, setQuote() {}, setApproved() {}, setClock() {}, setStatus: value => {status = value;}, action: async work => work(), api: async () => ({ok: true, data: {quote_id: 'checked', reasons: ready ? [] : ['insufficient_credits'], ready}}), apiFailure: String};
    context.start = async (...args) => calls.push(args);
    vm.runInNewContext(ts.transpileModule(method + ';globalThis.runRequested = estimate;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    await context.runRequested(false);
    assert.equal(calls.length, ready ? 1 : 0);
    if (ready) assert.ok(calls[0][0]);
    else assert.equal(status, USAGE_LIMIT_MESSAGE);
  }
});
