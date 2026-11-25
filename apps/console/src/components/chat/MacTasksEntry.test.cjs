const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('your local task guidance avoids hosted downloads and extra approvals across domains', () => {
  for (const domain of ['Invoice scheduling', 'Garden planning']) for (const desktop of [false, true]) for (const local of [false, true]) {
    const exports = {}, jsx = (type, props) => ({type, props});
    let owner;
    vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/MacTasksEntry.tsx', 'utf8'), {
      compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
    }).outputText, {exports, require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {useEffect() {}, useRef: initial => (owner = {current: initial}), useState: () => [{owner: owner.current, status: 'available'}, () => {}]};
      if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => local, useFeatureVisibility: () => () => !local};
      if (id.endsWith('/client')) return {verifiedSessionEpoch: () => 1, api: () => assert.fail('Rendering must not start work')};
      if (id.endsWith('/desktop')) return {isDesktopShell: () => desktop};
      if (id.endsWith('/reviewableDeviceRequest')) return {SUPPORTED_MAC_TASK_EXAMPLES: []};
      return {default: {}};
    }});
    const tree = exports.MacTasksEntry({ventureId: domain, onSelect() {}});
    if (local) assert.equal(tree, null);
    else assert.match(JSON.stringify(tree), desktop ? /before it runs/ : /Supymem for Mac/);
  }
});
