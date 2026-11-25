const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('your settings stay inside their shell while AI settings load across domains', () => {
  for (const domain of ['Factory scheduling', 'Family gardening']) {
    for (const pathname of ['/settings/local', '/settings/local/capabilities', '/connections']) {
      const file = __dirname + '/LocalSetup.tsx';
      const tree = ts.createSourceFile(file, readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
      const gate = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'LocalProviderGate');
      const context = {exports: {}, React: {createElement: (type, props, ...children) => ({type, props: {...props, children}})},
        usePathname: () => pathname, useState: value => [value, () => {}], useRef: value => ({current: value}), useEffect() {}};
      vm.runInNewContext(ts.transpileModule(gate.getText(tree), {compilerOptions: {jsx: ts.JsxEmit.React}}).outputText, context);
      const view = context.LocalProviderGate({children: domain});
      if (pathname.startsWith('/settings/local')) assert.equal(view, domain);
      else { assert.equal(view.props.className, 'page-loading'); assert.equal(view.props.role, 'status'); }
    }
  }
});
