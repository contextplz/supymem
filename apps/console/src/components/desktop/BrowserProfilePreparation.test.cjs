const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const {readFileSync} = require('node:fs'), ts = require('typescript');
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : tree?.props ? [tree, ...nodes(tree.props.children)] : [];

test('10: your matching entry route can check browser tasks while mismatched routes remain fenced', async () => {
  const {ventureQueryId} = await import('../../lib/operationsScope.ts');
  for (const domain of ['warehouse', 'cycling']) for (const route of ['entry', 'venture', 'wrong', 'duplicate', 'missing']) for (const local of [false, true]) {
    const ventureId = `ven_${domain}`, calls = [], states = [], refs = [], effects = [], exports = {};
    let cursor = 0, refCursor = 0, wireCalls = 0;
    const location = {pathname: route === 'venture' ? `/v/${ventureId}/settings` : '/new',
      search: route === 'wrong' ? '?venture=ven_elsewhere' : route === 'duplicate' ? `?venture=${ventureId}&venture=ven_elsewhere`
        : route === 'missing' ? '' : `?venture=${ventureId}`};
    const jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/BrowserProfilePreparation.tsx`, 'utf8'), {
      compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
    }).outputText, {exports, URLSearchParams, window: {location, addEventListener() {}, removeEventListener() {}}, require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {useState(initial) {const index = cursor++; if (!(index in states)) states[index] = initial;
        return [states[index], value => {states[index] = value;}];}, useRef: initial => refs[refCursor++] ??= {current: initial},
        useEffect: callback => effects.push(callback)};
      if (id.endsWith('/client')) return {verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
        api: async path => {calls.push(path); return {ok: true};}};
      if (id.endsWith('/operationsScope')) return {ventureQueryId};
      if (id.endsWith('/localMode')) return {isLocalConsole: () => local};
      if (id.endsWith('/browserProfileWireAvailability')) return {browserProfileWireAvailable: async () => {wireCalls++; return true;}};
      if (id.endsWith('/browserProfileCatalog')) return {browserProfileCatalog: async () => ({digest: 'a'.repeat(64), profile: {version: 'text'}})};
      return {ErrorNotice: 'error'};
    }});
    const render = () => {cursor = refCursor = 0; effects.length = 0; return exports.BrowserProfilePreparation({ventureId});};
    const initial = render(); effects[0](); await new Promise(resolve => setImmediate(resolve));
    if (local) {assert.equal(initial, null); assert.deepEqual(calls, []); assert.equal(wireCalls, 0); continue;}
    const retry = nodes(render()).find(node => node.props.children === 'Check availability again');
    const admitted = route === 'entry' || route === 'venture';
    assert.equal(calls.length, admitted ? 1 : 0); assert.equal(retry.props.disabled, !admitted);
    if (admitted) assert.equal(calls[0], `/api/ventures/${ventureId}/device-browser-ai-profile`);
  }
});
