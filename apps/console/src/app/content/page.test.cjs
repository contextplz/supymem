const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('your local content route renders the existing studio in venture selection across unrelated domains', () => {
  const source = ts.createSourceFile('page.tsx', readFileSync(__dirname + '/page.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = source.statements.filter(statement => !ts.isImportDeclaration(statement)).map(statement => statement.getText(source)).join('\n');
  for (const domain of ['Manufacturing schedules', 'Family walking routes']) {
    const context = {exports: {}, React: {createElement: (type, props, ...children) => ({type, props: {...props, children}})},
      isLocalConsole: () => true, Suspense: 'suspense', LocalCompanyPage: 'company-selection', ContentStudio: 'studio', AppShell: 'shell'};
    vm.runInNewContext(ts.transpileModule(body, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React}}).outputText, context);
    const page = context.exports.default();
    assert.equal(page.props.children[0].type, 'company-selection', domain);
    assert.equal(page.props.children[0].props.children[0].type, 'studio');
  }
});
