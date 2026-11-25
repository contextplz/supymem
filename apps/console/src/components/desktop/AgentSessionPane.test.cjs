const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const {safeLoopText} = require('../../lib/loopProgress.ts');
const source = ts.createSourceFile('AgentSessionPane.tsx', fs.readFileSync(__dirname + '/AgentSessionPane.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
let method;
function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'stop') method = node.getText(source);
  ts.forEachChild(node, visit);
}
visit(source);

test('your local coding prompt uses the shared checked order for all four tools', async () => {
  let start;
  function find(node) {if (ts.isFunctionDeclaration(node) && node.name?.text === 'startOrSend') start = node.getText(source); ts.forEachChild(node, find);}
  find(source);
  for (const domain of ['Fleet purchasing', 'Home baking']) for (const provider of ['codex', 'claude', 'cursor', 'opencode']) for (const ready of [true, false]) {
    const owner = {ventureId: domain, epoch: 1, generation: 1}, calls = [], routes = [], errors = [];
    const context = {ventureId: domain, provider, toolLabel: provider, renderedPreparationOwner: owner, preparationOwner: {current: owner},
      verifiedSessionEpoch: () => 1, sendingRef: {current: false}, turnRef: {current: 0}, known: {protocol: 'print'},
      isLocalConsole: () => true, isComputerUse: false, setErr: value => errors.push(value), setBusy() {}, setItems() {}, setInput() {}, setPhase() {},
      clientRef: {current: null}, acpSession: {current: null}, setSessionId() {}, setPermissions() {}, setIdeNote() {},
      setOldApp() {}, onFailedRef: {current: null}, invokeErrorMessage: () => 'Open Builds.', isUnknownDesktopCommand: () => false,
      desktopInvokeOrThrow: assert.fail, desktopInvoke: assert.fail, apiErrorCode: result => result.data?.detail?.code,
      routeInChat: path => {routes.push(path); return true;}, window: {location: {assign: assert.fail}},
      api: async (path, init) => {calls.push({path, body: JSON.parse(init.body)}); return ready
        ? {ok: true, data: {id: 'order-a', venture_id: domain}}
        : {ok: false, status: 409, data: {detail: {code: 'build_tool_needs_setup'}}};}};
    vm.runInNewContext(ts.transpileModule(start + ';globalThis.run = startOrSend;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    await context.run(`Fix the saved tool for ${domain}.`);
    assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/coding/dispatch');
    assert.deepEqual(calls[0].body.prefs, [provider]); assert.equal(routes.length, ready ? 1 : 0);
    if (!ready) assert.match(errors.at(-1), /Settings > Keys and connections > Coding tools/);
  }
});
function harness(sessionId, reply) {
  const calls = [], state = {phase: 'running', error: '', stopping: false}, exports = {};
  const context = {exports, sessionId, stopping: false, clientRef: {current: null}, turnRef: {current: 0},
    controlIdRef: {current: sessionId}, acpSession: {current: sessionId}, sendingRef: {current: true},
    setStopping: value => {context.stopping = state.stopping = value;}, setErr: value => {state.error = value;},
    setSessionId: value => {context.sessionId = value;}, setPermissions() {}, setBusy() {},
    setPhase: value => {state.phase = value;}, setItems() {},
    desktopInvokeOrThrow: async (command, args) => {calls.push({command, args}); return reply();}};
  vm.runInNewContext(ts.transpileModule(method + ';exports.stop = stop;', {compilerOptions: {
    target: ts.ScriptTarget.ES2022,
  }}).outputText, context);
  return {calls, state, stop: exports.stop};
}

test('you cannot mistake an untracked tool for a confirmed Stop in unrelated domains', async () => {
  for (const domain of ['Warehouse orders', 'Dog walking']) {
    const app = harness(null, () => {});
    await app.stop(domain);
    assert.equal(app.calls.length, 0);
    assert.equal(app.state.phase, 'running');
    assert.match(app.state.error, /Open Builds.*stop it/);
  }
});

test('you see Stop pending until native exit confirmation, and failure keeps the session running', async () => {
  for (const domain of ['Warehouse orders', 'Dog walking']) {
    for (const failed of [true, false]) {
      let finish;
      const app = harness(domain, () => new Promise((resolve, reject) => {finish = () => failed ? reject(new Error('private-fixture')) : resolve();}));
      const pending = app.stop();
      assert.equal(app.state.stopping, true);
      assert.equal(app.state.phase, 'running');
      finish(); await pending;
      assert.equal(app.state.stopping, false);
      assert.equal(app.state.phase, failed ? 'running' : 'idle');
      assert.equal(app.calls[0].command, 'kill_coding_control');
      if (failed) {
        assert.match(app.state.error, /not confirmed Stop.*Choose Stop again/);
        assert.doesNotMatch(app.state.error, /private-fixture/);
      }
    }
  }
});

test('your progress callback cannot expose credentials to another screen across domains', () => {
  let assignment;
  function find(node) {
    if (ts.isBinaryExpression(node) && node.left.getText(source) === 'onProgressRef.current') assignment = node.getText(source);
    ts.forEachChild(node, find);
  }
  find(source); assert.ok(assignment);
  for (const domain of ['Warehouse orders', 'Dog walking']) {
    const received = [], onProgressRef = {current: undefined};
    vm.runInNewContext(ts.transpileModule(assignment + ';', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText,
      {onProgressRef, safeLoopText, onProgress: text => received.push(text)});
    onProgressRef.current(`${domain}: npm test`);
    onProgressRef.current(`${domain}: password=private-fixture`);
    assert.equal(received[0], `${domain}: npm test`);
    assert.doesNotMatch(received[1], /private-fixture/);
  }
});
