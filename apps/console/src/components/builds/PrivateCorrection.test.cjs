const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('your connected correction keeps its exact source quote and never sends a managed budget', async () => {
  const source = ts.createSourceFile('PrivateCorrection.tsx', readFileSync(__dirname + '/PrivateCorrection.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
  let method;
  function find(node) {if (ts.isFunctionDeclaration(node) && node.name?.text === 'startCorrection') method = node.getText(source); ts.forEachChild(node, find);}
  find(source);
  for (const domain of ['Fleet purchasing', 'Home baking']) for (const valid of [true, false]) {
    const owner = {app: domain}, quote = {quote_id: 'source-bound', input_digest: 'checked-inputs', ready: true, mode: 'connected_tool', ceiling_micro: 10000};
    const calls = [], updates = [];
    const context = {current: () => true, renderedOwner: owner, approved: {current: {owner, quote}},
      dispatchCustody: {current: null}, quoteCustody: {current: {owner, value: quote}}, inFlight: {current: false},
      setDispatchHeld() {}, setConfirmed() {}, setBusy() {}, setError() {}, setQueued() {}, setQuote() {},
      knownPreflightRefusal: () => false, apiFailure: String, onQueued: () => updates.push('queued'),
      api: async (path, init) => {calls.push({path, body: JSON.parse(init.body), headers: init.headers});
        return {ok: true, data: valid ? {build_run_id: 'correction', device_work_order_id: 'order-a'} : {}};}};
    vm.runInNewContext(ts.transpileModule(method + ';globalThis.run = startCorrection;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    await context.run(); await context.run();
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].body, {quote_id: quote.quote_id, input_digest: quote.input_digest});
    assert.equal(calls[0].headers['Idempotency-Key'], 'private-correction:source-bound');
    assert.equal(updates.length, valid ? 1 : 0);
  }
});
