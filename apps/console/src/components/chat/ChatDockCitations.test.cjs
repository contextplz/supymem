const assert = require('node:assert/strict'), test = require('node:test');
const { readFileSync } = require('node:fs'), vm = require('node:vm'), ts = require('typescript');
const source = readFileSync(`${__dirname}/ChatDock.tsx`, 'utf8');
const tree = ts.createSourceFile('ChatDock.tsx', source, ts.ScriptTarget.Latest, true);
let expression;
function visit(node) {
  if (ts.isJsxExpression(node) && node.expression?.getText(tree).startsWith('m.citations?.some')) expression = node.expression.getText(tree);
  ts.forEachChild(node, visit);
}
visit(tree);
const jsx = (type, props) => ({ type, props });
const compiled = ts.transpileModule(`exports.rendered = (${expression});`, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
function render(citations) {
  const context = { exports: {}, m: { citations }, styles: { citations: 'citations', citation: 'citation' }, ChatAnchor: 'a',
    require: () => ({ jsx, jsxs: jsx }) };
  vm.runInNewContext(compiled, context);
  return context.exports.rendered;
}

test('native briefing metadata never becomes an empty source link and your legacy links stay unchanged across domains', () => {
  assert.ok(expression);
  for (const [audience, domain] of [['Invoice audits for finance teams', 'audits.example'], ['Pottery classes for families', 'pottery.example']]) {
    const briefing = { inbox_briefing: { items: [], filtered: { newsletters: 0, notifications: 0 } } };
    const link = { label: audience, href: `https://${domain}/source` };
    assert.equal(render([briefing, { next_moves: { body_md: audience } }]), false);
    const legacy = render([link]);
    const mixed = render([briefing, link]).props.children.filter(Boolean);
    assert.equal(mixed.length, 1);
    assert.deepEqual(JSON.parse(JSON.stringify(mixed[0])), JSON.parse(JSON.stringify(legacy.props.children[0])));
    assert.equal(mixed[0].type, 'a');
    assert.equal(mixed[0].props.href, link.href);
    assert.equal(mixed[0].props.children, audience);
    assert.equal(render(undefined), undefined);
  }
});
