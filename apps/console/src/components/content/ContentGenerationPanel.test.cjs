const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('your saved content request offers recovery, not another confirmation, across domains', () => {
  const {draftGate} = require('../../lib/contentFounder.ts');
  for (const audience of ['factory purchasing teams', 'home pottery learners']) {
    const gate = draftGate({hasVenture: !!audience, hasBrief: true, busy: false, running: false, pending: true});
    assert.equal(gate.disabled, true);
    assert.match(gate.reason, /Recover submitted request/);
    assert.doesNotMatch(gate.reason, /confirmation|approval/);
  }
});

test('your writing continues locally after interrupted status polling across domains', async () => {
  const source = readFileSync(__dirname + '/ContentGenerationPanel.tsx', 'utf8');
  const file = ts.createSourceFile('panel.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let poll;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'poll') poll = node.getText(file);
    ts.forEachChild(node, visit);
  }
  visit(file);
  for (const venture of ['ven_factory_buyers', 'ven_pottery_learners']) {
    for (const interrupted of [false, true]) {
      const messages = [];
      const context = {local: true, cancelled: false, current: () => true, count: 119, venture,
        run: {run_id: 'saved-run'}, active: () => true, setRun() {}, setTimeout() {},
        setError: message => messages.push(message), opsApi: {async get() {if (interrupted) throw Error('offline'); return {};}}};
      vm.runInNewContext(ts.transpileModule(poll + ';globalThis.pollStatus = poll;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
      await context.pollStatus();
      assert.equal(messages.length, 1);
      assert.match(messages[0], /your laptop.*app.*running/i);
      assert.match(messages[0], /Refresh generation status/);
      assert.doesNotMatch(messages[0], /server|credits|job_/);
    }
  }
});

test('requested topics and drafts start with the checked quote in unrelated domains', async () => {
  const source = ts.createSourceFile('ContentGenerationPanel.tsx', readFileSync(__dirname + '/ContentGenerationPanel.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
  let callback;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'preview') callback = node.getText(source);
    ts.forEachChild(node, visit);
  }
  visit(source);
  for (const venture of ['Factory dispatch', 'Family gardening']) {
    for (const kind of ['topics', 'draft']) {
      const started = [], request = {venture_id: venture, kind}, quote = {sufficient_now: true, input_digest: 'input', resource_digest: 'resource', ceiling_micro: 23000};
      const context = {useCallback: value => value, fence: {current: {capture: () => () => true}}, venture,
        briefId: 'saved-brief', busy: false, run: null, pending: null, scopeKey: venture, active: () => false,
        setBusy() {}, setError() {}, setQuote() {}, setRequest() {}, setRun() {}, setPlatform() {}, chainDraft: {current: false},
        quoteContentWithTopicRecovery: async () => ({request, quote}), opsApi: {post() {}}, newKey: () => 'one-request',
        begin: async envelope => started.push(envelope), opsErrorMessage: String};
      vm.runInNewContext(ts.transpileModule('const ' + callback + ';globalThis.runPreview = preview;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
      await context.runPreview(kind);
      assert.equal(started.length, 1);
      assert.deepEqual(JSON.parse(JSON.stringify(started[0])), {...request, idempotency_key: 'one-request', input_digest: 'input', resource_digest: 'resource', ceiling_micro: 23000});
      quote.sufficient_now = false;
      await context.runPreview(kind);
      assert.equal(started.length, 1);
    }
  }
});
