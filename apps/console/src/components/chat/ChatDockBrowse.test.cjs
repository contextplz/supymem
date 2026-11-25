const assert = require('node:assert/strict'), test = require('node:test');
const {readFileSync} = require('node:fs'), vm = require('node:vm'), ts = require('typescript');

test('the existing tray receives browse progress alongside search progress in unrelated domains', () => {
  const source = readFileSync(`${__dirname}/ChatDock.tsx`, 'utf8');
  const tree = ts.createSourceFile('ChatDock.tsx', source, ts.ScriptTarget.Latest, true);
  let expression;
  function visit(node) {
    if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(tree) === 'AgentsTray') {
      expression = node.attributes.properties.find(attribute => attribute.name?.getText(tree) === 'progress').initializer.expression.getText(tree);
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(expression);
  for (const [venture, audience] of [['procurement', 'Purchasing teams'], ['gardens', 'Home gardeners']]) {
    const search = {job_id: `search-${venture}`, state: 'running'}, browse = {job_id: `browse-${venture}`, step_label: audience};
    const messages = [{id: venture, body_md: audience}];
    const context = {messages, searchSnapshots: {scope: venture, items: [search]}, workKey: venture,
      browseProgressFor: received => {assert.equal(received, messages); return [browse];}};
    vm.createContext(context);
    assert.deepEqual(Array.from(vm.runInContext(expression, context)), [search, browse]);
    context.searchSnapshots.scope = 'other-venture';
    assert.deepEqual(Array.from(vm.runInContext(expression, context)), [browse]);
  }
});
