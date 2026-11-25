const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const {readFileSync} = require('node:fs'), ts = require('typescript');
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : tree?.props ? [tree, ...nodes(tree.props.children)] : [];
const tick = () => new Promise(resolve => setImmediate(resolve));

function fixture(domain, layout = 'home', section, local = false, component = 'VentureSettings', catalog = {providers: {openai: ['gpt-6-sol']}}) {
  const slots = [], effects = [], requests = [], exports = {}, listeners = new Map();
  let cursor = 0, phrasePromise, settingsPromise, writePromise;
  const settings = {source: 'founder_override', provider: 'openai', model: 'gpt-6-sol', models: ['gpt-6-sol'], badge: domain};
  const same = (before, after) => before && before.length === after.length && before.every((value, index) => Object.is(value, after[index]));
  const jsx = (type, props) => typeof type === 'function' ? type(props) : {type, props};
  const react = {useContext: () => layout,
    useState(initial) {const index = cursor++; if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => {slots[index] = typeof value === 'function' ? value(slots[index]) : value;}];},
    useRef(initial) {const index = cursor++; return slots[index] ??= {current: initial};},
    useCallback(callback, deps) {const index = cursor++; if (!same(slots[index]?.deps, deps)) slots[index] = {callback, deps}; return slots[index].callback;},
    useEffect(callback, deps) {const index = cursor++; if (!same(slots[index]?.deps, deps)) {
      slots[index]?.cleanup?.(); slots[index] = {deps}; effects.push(() => {slots[index].cleanup = callback();});}}};
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/${component}.tsx`, 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText, {exports, window: {location: {hash: ''}, setInterval: () => 1, clearInterval() {}, setTimeout: () => 1,
    addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: name => listeners.delete(name)}, require(id) {
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    if (id === 'next/navigation') return {useRouter: () => ({push() {}})};
    if (id === '@/app/setup/localProviders') {
      const parser = {};
      vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/../../app/setup/localProviders.ts`, 'utf8'), {
        compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
      }).outputText, {exports: parser, require: () => ({})});
      return parser;
    }
    if (id.endsWith('/client')) return {ensureSession: async () => ({ok: true}), verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async (path, options) => {
        requests.push({path, options});
        if (options) return writePromise || {ok: true, data: settings};
        if (path.endsWith('/delete-phrase')) return phrasePromise || {ok: true, data: {phrase: 'DELETE'}};
        if (path.endsWith('/llm-settings')) return settingsPromise || {ok: true, data: settings};
        if (path.endsWith('/llm-catalog')) return {ok: true, data: catalog};
        return {ok: true, data: {name: domain, workspace_id: 'workspace'}};
      }};
    if (id.endsWith('/format')) return {shortTitle: value => value};
    if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => local, useFeatureVisibility: () => () => !local};
    return new Proxy({}, {get: (_target, name) => String(name)});
  }});
  const render = () => {cursor = 0; const tree = exports[component]({ventureId: 'ven_domain', section, workspaceId: 'workspace', intent: 'byok'}); effects.splice(0).forEach(effect => effect()); return tree;};
  return {render, requests,
    deferPhrase() {let resolve; phrasePromise = new Promise(done => {resolve = done;}); return value => {phrasePromise = null; resolve(value);};},
    deferSettings() {let resolve; settingsPromise = new Promise(done => {resolve = done;}); return value => {settingsPromise = null; resolve(value);};},
    deferWrite() {let resolve; writePromise = new Promise(done => {resolve = done;}); return () => {writePromise = null; resolve({ok: true, data: settings});};},
  };
}

test('both model controls read local choices rather than provider status objects', async () => {
  for (const domain of ['fleet-dispatch', 'trail-planning']) for (const [component, id] of [['VentureSettings', 'llm-model'], ['LlmSetupCard', 'onboard-llm-model']]) {
    const models = ['gpt-6-sol', `${domain}-model`];
    const app = fixture(domain, 'classic', undefined, false, component, {
      providers: {openai: {configured: true, models: {chat: models[0]}}}, default_models: {openai: models},
    });
    app.render(); await tick();
    const node = nodes(app.render()).find(node => node.props.id === id);
    assert.equal(node?.type, 'select', component);
    assert.deepEqual(Array.from(nodes(node.props.children), option => option.props.children), models);
  }
});

test('local venture settings never mount purchases or referrals in either layout across domains', async () => {
  for (const domain of ['Fleet maintenance', 'Home gardening']) for (const layout of ['home', 'classic']) {
    const app = fixture(domain, layout, 'credits', true);
    app.render(); await tick();
    const tree = app.render();
    assert.equal(nodes(tree).some(node => ['TopUpCard', 'ReferralCard'].includes(node.type)), false, domain);
    assert.equal(nodes(tree).some(node => node.type === 'h2' && node.props.children === 'Buy more'), false, domain);
  }
});

test('computer-use settings disappear locally in both layouts without removing your AI service', async () => {
  for (const domain of ['Factory planning', 'Family recipes']) for (const layout of ['home', 'classic']) for (const local of [true, false]) {
    const app = fixture(domain, layout, 'mac', local);
    app.render(); await tick();
    const tree = app.render();
    assert.equal(nodes(tree).some(node => node.props?.id === 'computer-use' || node.props?.id === 'settings-mac'), !local, domain);
  }
});

test('provider reset is hosted-only in both settings layouts across domains', async () => {
  for (const domain of ['Fleet maintenance', 'Home gardening']) for (const layout of ['home', 'classic']) {
    for (const local of [true, false]) {
      const app = fixture(domain, layout, 'models', local);
      app.render(); await tick();
      const tree = app.render();
      const reset = nodes(tree).find(node => node.props['data-usage-control'] === 'llm_settings_use_ours');
      assert.equal(Boolean(reset), !local, `${domain}: ${layout}, local=${local}`);
      assert.equal(JSON.stringify(tree).includes('Back to Supymem hosted'), !local);
      assert.equal(nodes(tree).some(node => node.props['data-usage-control'] === 'llm_settings_save'), !local);
      assert.equal(nodes(tree).some(node => node.props['data-usage-control'] === 'llm_settings_test'), !local);
      if (reset) {
        reset.props.onClick(); await tick();
        const request = app.requests.at(-1);
        assert.equal(request.path, '/api/workspaces/workspace/llm-settings');
        assert.equal(request.options.method, 'DELETE');
      } else {
        assert.ok(app.requests.every(request => !request.options));
        assert.doesNotMatch(JSON.stringify(tree), /Using platform keys/);
      }
    }
  }
});

test('your local model settings keep hosted model and cloud copy out of both layouts across domains', async () => {
  for (const domain of ['Invoice scheduling', 'Garden planning']) for (const layout of ['home', 'classic']) {
    const app = fixture(domain, layout, 'models', true);
    app.render(); await tick();
    const tree = app.render();
    assert.doesNotMatch(JSON.stringify(tree), /cloud proxy|platform key|platform default|hosted connection key|Using Supymem|coming soon in beta/);
    assert.ok(nodes(tree).some(node => node.props.href === '/settings/local#providers-title'));
    assert.ok(app.requests.every(request => !request.options));
  }
});

test('saved task review appears only after verified settings load and never starts work', async () => {
  for (const domain of ['Fleet maintenance', 'Home gardening']) {
    for (const layout of ['home', 'classic']) {
      const app = fixture(domain, layout, 'mac');
      assert.equal(nodes(app.render()).some(node => node.type === 'MacTaskList'), false);
      await tick();
      const tasks = nodes(app.render()).find(node => node.type === 'MacTaskList');
      assert.equal(tasks.props.ventureId, 'ven_domain');
      assert.ok(app.requests.every(request => !request.options));
      assert.ok(app.requests.every(request => !/\/approve|\/start|\/confirm/.test(request.path)));
    }
  }
});

test('22: independent settings reads start together while workspace reads wait for verified metadata', async () => {
  for (const domain of ['Supply planning', 'Community art']) {
    const app = fixture(domain), finishPhrase = app.deferPhrase(), finishSettings = app.deferSettings();
    app.render(); await tick();
    assert.ok(app.requests.some(request => request.path === '/api/ventures/ven_domain'));
    assert.equal(app.requests.some(request => request.path.includes('/workspaces/')), false);
    finishPhrase({ok: false}); await tick();
    assert.ok(app.requests.some(request => request.path.endsWith('/llm-settings')));
    assert.ok(app.requests.some(request => request.path.endsWith('/llm-catalog')));
    finishSettings({ok: false}); await tick();
  }
});

test('25: pending model settings are not labeled as failed and a failed read has a working retry', async () => {
  for (const domain of ['Supply planning', 'Community art']) {
    const app = fixture(domain, 'home', 'models'), finish = app.deferSettings(); app.render(); await tick();
    let tree = app.render(); assert.match(JSON.stringify(tree), /Loading model settings/);
    assert.doesNotMatch(JSON.stringify(tree), /Model settings could not be loaded|Refresh saved settings/);
    finish({ok: false}); await tick(); tree = app.render(); assert.match(JSON.stringify(tree), /Model settings could not be loaded/);
    nodes(tree).find(node => node.props.children === 'Refresh saved settings').props.onClick(); await tick();
    assert.match(JSON.stringify(app.render()), new RegExp(domain));
    assert.equal(app.requests.filter(request => request.path.endsWith('/llm-settings')).length, 2);
  }
});

test('26: a shared settings mutation visibly disables your name and model controls in both layouts', async () => {
  for (const domain of ['Supply planning', 'Community art']) for (const layout of ['home', 'classic']) {
    const app = fixture(domain, layout); app.render(); await tick(); const finish = app.deferWrite();
    nodes(app.render()).find(node => node.props['data-usage-control'] === 'llm_settings_save').props.onClick();
    const tree = app.render(), controls = nodes(tree).filter(node => ['Venture name', 'Manager name'].includes(node.props['aria-label'])
      || node.props.id?.startsWith('llm-') || ['llm_settings_save', 'llm_settings_test', 'llm_settings_use_ours', 'venture_rename', 'settings_manager_rename'].includes(node.props['data-usage-control']));
    assert.ok(controls.length >= 8); assert.ok(controls.every(node => node.props.disabled === true));
    assert.match(JSON.stringify(tree), /Updating or checking your settings/);
    finish(); await tick(); assert.equal(nodes(app.render()).find(node => node.props.id === 'llm-provider').props.disabled, false);
  }
});

test('27: shared venture settings do not load hosted billing or show credit usage', async () => {
  for (const domain of ['Supply planning', 'Community art']) {
    const app = fixture(domain, 'home'); app.render(); await tick(); const tree = app.render();
    assert.equal(nodes(tree).some(node => ['TopUpCard', 'ReferralCard', 'CompanyCreditSummary', 'SpendSection', 'TokenMeter'].includes(node.type)), false);
    assert.doesNotMatch(JSON.stringify(tree), /Credit details/);
  }
});
