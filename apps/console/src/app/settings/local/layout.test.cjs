const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('your settings use the same sidebar shell as your workspace across domains', () => {
  for (const domain of ['Supply planning', 'Home gardening']) {
    const exported = {};
    vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/layout.tsx', 'utf8'), {
      compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
    }).outputText, {exports: exported, require: name => name === 'react/jsx-runtime'
      ? {jsx: (type, props) => ({type, props})} : name.endsWith('LocalModeBoundary')
        ? {useLocalMode: () => true} : {AppShell: 'shared-shell'}});
    const tree = exported.default({children: domain});
    assert.equal(tree.type, 'shared-shell');
    assert.equal(tree.props.children, domain);
  }
});
