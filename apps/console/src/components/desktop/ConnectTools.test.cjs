const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('you can connect any supported coding tool, including OpenCode', () => {
  const source = readFileSync(__dirname + '/ConnectTools.tsx', 'utf8');
  for (const domain of ['Fleet purchasing', 'Home baking']) {
    const exports = {}, jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX}}).outputText, {exports, window: {}, require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {useState: initial => [initial, () => {}], useEffect() {}};
      if (id.endsWith('/desktop')) return {isDesktopShell: () => true};
      if (id.endsWith('/localMode')) return {isLocalConsole: () => true};
      return {};
    }});
    const tree = JSON.stringify(exports.ConnectTools());
    assert.match(tree, /To build, connect Claude Code, Codex, Cursor or OpenCode/, domain);
    assert.doesNotMatch(tree, /InstallLocalBuilder|hermes\/bootstrap/, domain);
    assert.match(tree, /verified.*subscription|subscription.*verified/);
    assert.doesNotMatch(tree, /You can build with your signed-in/);
  }
});
