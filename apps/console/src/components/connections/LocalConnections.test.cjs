const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const {capabilityView, STATE_TITLES} = require('../../app/settings/local/capabilities/capabilities.ts');

const domains = [{id: 'ven_shipping', name: 'Shipping schedules', workspace_id: 'wsp_shipping'},
  {id: 'ven_walking', name: 'Family walking routes', workspace_id: 'wsp_walking'}];
const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
const text = node => typeof node === 'string' ? node : !node || typeof node !== 'object' ? '' : [node.props?.children].flat(8).map(text).join('');
const flush = () => new Promise(resolve => setImmediate(resolve));

function component(filename, name, overrides = {}) {
  const slots = [], effects = [], listeners = new Map();
  let cursor = 0;
  const context = {
    exports: {}, URL, URLSearchParams, React: {createElement: (type, props, ...children) => ({type, props: {...props, children}})},
    Link: 'a', AppShell: 'shell', CodingAgentTiles: 'coding-tools', ErrorNotice: 'error', styles: {}, capabilityView, STATE_TITLES,
    useState(initial) {const index = cursor++; if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => {slots[index] = typeof value === 'function' ? value(slots[index]) : value;}];},
    useRef(initial) { return slots[cursor++] ??= { current: initial }; },
    useEffect(callback, deps) {const index = cursor++, prior = slots[index];
      if (!prior || deps.some((value, position) => value !== prior.deps[position])) {
        prior?.cleanup?.(); const slot = {deps}; slots[index] = slot; effects.push(() => {slot.cleanup = callback();});
      }},
    window: {location: {href: 'https://console.example/connections', pathname: '/connections'}, addEventListener: (name, callback) => listeners.set(name, callback),
      removeEventListener: name => listeners.delete(name)}, ...overrides,
  };
  const source = ts.createSourceFile(filename, readFileSync(__dirname + '/' + filename, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = source.statements.filter(statement => !ts.isImportDeclaration(statement)).map(statement => statement.getText(source)).join('\n');
  vm.runInNewContext(ts.transpileModule(body, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React}}).outputText, context);
  return {exports: context.exports, render(props) {cursor = 0; const tree = context.exports[name](props); effects.splice(0).forEach(effect => effect()); return tree;},
    stop() {slots.forEach(slot => slot?.cleanup?.());}, retire() {listeners.get('supymem-session-changed')?.();}};
}

test('your AI provider and coding tools lead your connections across domains', async () => {
  for (const company of domains) {
    const page = component('LocalConnections.tsx', 'LocalConnections', {api: async path => ({ok: true,
      data: {values: {provider: 'openai', model: company.name}, configured: path.endsWith('/providers'), verified: true, secrets_set: {api_key: true}}})});
    page.render(); await flush();
    const tree = page.render();
    const cards = nodes(tree).filter(node => node.type === 'article');
    assert.match(text(cards[0]), /Your AI provider.*Ready/);
    const ordered = nodes(tree).filter(node => node.type === 'article' || node.type === 'coding-tools');
    assert.equal(ordered[1].type, 'coding-tools');
    page.stop();
  }
});

test('local connections read capability receipts and show keyless search and exact setup steps across domains', async () => {
  for (const company of domains) {
    const calls = [], capabilities = ['send_email', 'research', 'publish_pages', 'connected_apps'].map(name => ({
      name, title: name, state: name === 'research' ? 'ready' : 'needs_setup', missing: [],
      next_step: name === 'research' ? 'You can use built-in search.' : 'Open Keys and connections to finish setup.',
    }));
    const page = component('LocalConnections.tsx', 'LocalConnections', {api: async path => {
      calls.push(path); return {ok: true, data: path.endsWith('/capabilities') ? {capabilities} : {configured: false}};
    }});
    page.render(); await flush();
    const tree = page.render();
    assert.deepEqual(calls.sort(), ['/api/local/capabilities', '/api/local/providers']);
    const cards = nodes(tree).filter(node => node.type === 'article');
    assert.equal(cards.length, 5, company.name);
    assert.match(text(cards[2]), /SearchReady.*built-in search/);
    for (const card of [cards[0], cards[1], cards[3], cards[4]]) assert.match(text(card), /Needs setup.*Keys and connections/);
    assert.deepEqual(nodes(tree).filter(node => node.type === 'a').map(node => node.props.href),
      ['providers', 'smtp', 'search', 'publishing', 'composio'].map(section => `/settings/local#${section}-title`));
    page.stop();
  }
});

test('company pages choose the current or only venture, offer a picker, and never start work across domains', async () => {
  for (const company of domains) {
    for (const [items, workspace, selected] of [[[company], '', company], [domains, company.workspace_id, company], [domains, '', null], [[], '', null]]) {
      const calls = [], routes = [], params = new URLSearchParams();
      const page = component('LocalCompanyPage.tsx', 'LocalCompanyPage', {useSearchParams: () => params,
        useRouter: () => ({replace: href => routes.push(href), push: href => routes.push(href)}), getOpsWorkspace: () => workspace,
        api: async path => {calls.push(path); return {ok: true, data: {items}};}});
      const props = {title: 'Connections', subtitle: company.name, children: company.name};
      page.render(props); await flush();
      assert.deepEqual(calls, ['/api/ventures?summary=true']);
      if (selected) assert.deepEqual(routes, [`/connections?venture=${selected.id}`]);
      else if (!items.length) assert.ok(nodes(page.render(props)).some(node => node.type === 'a' && node.props.href === '/new'));
      else {
        assert.deepEqual(routes, []);
        const picker = nodes(page.render(props)).find(node => node.type === 'select');
        assert.ok(picker);
        picker.props.onChange({target: {value: 'foreign'}}); assert.deepEqual(routes, []);
        picker.props.onChange({target: {value: company.id}});
        assert.deepEqual(routes, [`/connections?venture=${company.id}`]);
      }
      page.stop();
    }
    const params = new URLSearchParams({venture: company.id}), calls = [];
    const selectedPage = component('LocalCompanyPage.tsx', 'LocalCompanyPage', {useSearchParams: () => params, useRouter: () => ({}), api: async path => {calls.push(path);}});
    assert.equal(selectedPage.render({title: 'Content', subtitle: company.name, children: company.name}).type, 'shell');
    assert.deepEqual(calls, []); selectedPage.stop();
  }
});

test('local settings and venture failures offer a named next step and discard retired reads across domains', async () => {
  for (const company of domains) {
    const results = [];
    const page = component('LocalConnections.tsx', 'LocalConnections', {api: () => new Promise(resolve => {results.push(resolve);})});
    page.render(); page.retire(); results.forEach(resolve => resolve({ok: false})); await flush();
    assert.doesNotMatch(text(page.render()), /could not be loaded/); page.stop();
    let resolve;
    const routes = [], params = new URLSearchParams();
    const picker = component('LocalCompanyPage.tsx', 'LocalCompanyPage', {useSearchParams: () => params,
      useRouter: () => ({replace: href => routes.push(href)}), getOpsWorkspace: () => '', api: () => new Promise(done => {resolve = done;})});
    picker.render({title: 'Connections', subtitle: company.name}); picker.retire();
    resolve({ok: true, data: {items: [company]}}); await flush();
    assert.deepEqual(routes, []); picker.stop();
    const malformed = component('LocalCompanyPage.tsx', 'LocalCompanyPage', {useSearchParams: () => params, useRouter: () => ({}),
      getOpsWorkspace: () => '', api: async () => ({ok: true, data: {items: [{id: company.id, name: {invalid: true}}]}})});
    malformed.render({title: 'Connections', subtitle: company.name}); await flush();
    assert.match(text(malformed.render({title: 'Connections', subtitle: company.name})), /Reload your ventures/); malformed.stop();
    for (const [file, name, overrides] of [['LocalConnections.tsx', 'LocalConnections', {}], ['LocalCompanyPage.tsx', 'LocalCompanyPage',
      {useSearchParams: () => new URLSearchParams(), useRouter: () => ({}), getOpsWorkspace: () => ''}]]) {
      const failed = component(file, name, {...overrides, api: async () => ({ok: false, status: 503})});
      failed.render({title: 'Connections', subtitle: company.name}); await flush();
      const tree = failed.render({title: 'Connections', subtitle: company.name});
      assert.match(text(tree), /Keys and connections.*[Rr]eload/);
      const notice = nodes(tree).find(node => node.type === 'error');
      assert.match(notice.props.error, /Keys and connections/);
      assert.equal(notice.props.source, name === 'LocalConnections' ? 'connections.local.settings' : 'connections.local.ventures');
      failed.stop();
    }
  }
});

test('connection cards show the saved capability state and exact next step', async () => {
  for (const {name} of domains) {
    const capabilities = ['send_email', 'research', 'publish_pages', 'connected_apps'].map(capability => ({
      name: capability, title: name, state: capability === 'research' ? 'off' : capability === 'publish_pages' ? 'needs_setup' : 'ready',
      missing: capability === 'publish_pages' ? ['Public site URL'] : [],
      next_step: capability === 'publish_pages' ? 'Save your public site URL in Keys and connections.' : 'Use Settings > Capabilities.',
    }));
    const page = component('LocalConnections.tsx', 'LocalConnections', {api: async path => ({ok: true,
      data: path.endsWith('/capabilities') ? {capabilities} : {configured: true, values: {}, secrets_set: {}}})});
    page.render(); await flush();
    const cards = nodes(page.render()).filter(node => node.type === 'article');
    assert.match(text(cards[0]), /Your AI providerSavedYou can use your saved provider\./);
    assert.doesNotMatch(text(cards[0]), /not tested|Needs setup|Ready/);
    assert.match(text(cards[2]), /SearchOff.*Settings > Capabilities/);
    assert.match(text(cards[3]), /PublishingNeeds setup.*public site URL/);
    page.stop();
  }
});

test('Connections offers a neutral label when a company has no name', async () => {
  for (const company of domains) {
    let workspace = '';
    const gate = component('../ops/OpsWorkspaceGate.tsx', 'OpsWorkspaceGate', {
      createContext: () => ({ Provider: 'scope' }), useSearchParams: () => new URLSearchParams(),
      verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => 1,
      ensureSession: async () => ({ok: true, data: {authenticated: true, subject: 'owner'}}),
      apiSwr: async () => ({ok: true, data: {items: [{id: company.workspace_id, name: ''}]}}),
      getOpsWorkspace: () => workspace, setOpsWorkspace: id => { workspace = id; },
    });
    gate.render({}); await flush();
    const tree = gate.render({});
    assert.equal(text(nodes(tree).find(node => node.type === 'label')), 'Company');
    assert.equal(text(nodes(tree).find(node => node.type === 'option' && node.props.value === company.workspace_id)), 'Unnamed company');
    gate.stop();
  }
});
