const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('your local home cannot show hosted marketing even with stale marketing state across domains', () => {
  const source = readFileSync(__dirname + '/page.tsx', 'utf8');
  for (const domain of ['procurement.example', 'familyrecipes.example']) for (const local of [true, false]) {
    const exports = {}, probes = [];
    vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    }}).outputText, {exports, window: {location: {host: domain, pathname: '/', search: '?marketing=1'}}, require(name) {
      if (name === 'react') return {useEffect: callback => callback(), useState: () => [true, () => {}]};
      if (name === 'react/jsx-runtime') return {jsx: (type, props) => ({type, props})};
      if (name.endsWith('/AppShell')) return {AppShell: 'workspace'};
      if (name.endsWith('/VentureHub')) return {VentureHub: 'ventures'};
      if (name.endsWith('/WaitlistLanding')) return {WaitlistLanding: 'marketing'};
      if (name.endsWith('/LocalModeBoundary')) return {useLocalMode: () => local};
      if (name === '@/lib/desktop') return {isDesktopShell: () => false};
      if (name === '@/lib/hostMode') return {isMarketingSurface: () => {probes.push(domain); return true;}};
      throw Error(name);
    }});
    const home = exports.default();
    assert.equal(home.type, local ? 'workspace' : 'marketing', domain);
    assert.equal(probes.length, local ? 0 : 1, domain);
    if (local) assert.equal(home.props.children.props.local, true);
  }
});
