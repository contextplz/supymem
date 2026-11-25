const assert = require('node:assert/strict');
const test = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

test('your Mac setup notice appears only for requested, supported access and never on an idle local page', async () => {
  for (const domain of ['warehouse teams', 'weekend walkers']) for (const local of [false, true]) {
    let kind = null, listener, supported = false, supportChecks = 0;
    const navigator = {platform: 'Linux x86_64'};
    const effects = [], exported = {};
    const jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/AttentionToast.tsx', 'utf8'), {
      compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
    }).outputText, {exports: exported, navigator, window: {addEventListener: (_, callback) => {listener = callback;}, removeEventListener() {}}, require(id) {
      if (id === 'react') return {useState: () => [kind, value => {kind = value;}], useEffect: effect => effects.push(effect)};
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id.endsWith('/macPermissions')) return {nativePermissionsSupported: async () => {supportChecks++; return supported;}};
      if (id === './LocalModeBoundary') return {useLocalMode: () => local};
      return {default: {toast: 'toast', actions: 'actions'}, ChatAnchor: 'a'};
    }});
    const render = () => exported.AttentionToast({ventureId: domain});
    const idle = render();
    if (local) assert.equal(idle, null, domain);
    else { assert.equal(idle.type, 'aside'); assert.equal(idle.props.hidden, true); assert.equal(idle.props.inert, true); }
    const cleanup = effects[0]();
    const requested = listener({detail: {kind: 'mac_permission'}});
    if (!local) assert.equal(requested, undefined, 'Hosted attention handling remains synchronous');
    await requested;
    assert.equal(kind, local ? null : 'mac_permission');
    kind = null; supported = true; await listener({detail: {kind: 'mac_permission'}});
    assert.equal(kind, local ? null : 'mac_permission');
    navigator.platform = 'MacIntel';
    kind = null; supported = true; await listener({detail: {kind: 'mac_permission'}});
    assert.equal(kind, 'mac_permission'); assert.ok(render());
    kind = null; await listener({detail: {kind: 'provider_connection'}});
    assert.equal(kind, 'provider_connection'); assert.ok(render());
    assert.equal(supportChecks, local ? 1 : 0, 'Hosted attention never probes native permissions');
    kind = null;
    const pending = listener({detail: {kind: 'mac_permission'}});
    cleanup(); await pending;
    assert.equal(kind, local ? null : 'mac_permission');
  }
});
