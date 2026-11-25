const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('a local Stripe link offers spending settings without opening the connection flow', () => {
  const source = readFileSync(__dirname + '/page.tsx', 'utf8');
  const compiled = ts.transpileModule(source, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS,
  }}).outputText;
  for (const ventureId of ['factory-procurement', 'family-gardening']) {
    const exports = {};
    const jsx = (type, props) => typeof type === 'function' ? type(props) : {type, props};
    vm.runInNewContext(compiled, {exports, require(name) {
      if (name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (name === 'next/navigation') return {useParams: () => ({ventureId, provider: 'stripe'}),
        useRouter: () => assert.fail('The connection flow must not open locally')};
      if (name === '@/lib/localMode') return {isLocalConsole: () => true};
      if (name === '@/components/layout/AppShell') return {AppShell: 'AppShell'};
      if (name === './connect.module.css') return {};
      return {};
    }});
    const tree = JSON.stringify(exports.default());
    assert.match(tree, /\/settings\/local#spend-title/);
    assert.doesNotMatch(tree, /Stripe secret key|Connect Stripe|credits/i);
  }
});
