const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('local settings do not request or show computer-use tasks across unrelated ventures', async () => {
  const source = ts.createSourceFile('MacTaskList.tsx', readFileSync(__dirname + '/MacTaskList.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = source.statements.filter(statement => !ts.isImportDeclaration(statement)).map(statement => statement.getText(source)).join('\n');
  for (const ventureId of ['ven_factory_scheduling', 'ven_home_gardening']) {
    for (const local of [true, false]) {
      const calls = [], context = {exports: {}, useFeatureVisibility: () => () => !local, useState: () => [null, () => {}], useEffect: callback => callback(),
        api: async path => {calls.push(path); return {ok: true, data: {orders: []}};}};
      vm.runInNewContext(ts.transpileModule(body, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React}}).outputText, context);
      assert.equal(context.exports.MacTaskList({ventureId}), null);
      await Promise.resolve();
      assert.deepEqual(calls, local ? [] : [`/api/ventures/${ventureId}/device-work-orders`]);
    }
  }
});
