const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const visibility = require('../../lib/localFeatureVisibility.ts');
const capabilities = [{state: 'not_in_this_version', surfaces: {paths: ['/admin', '/operator', '/ops', '/billing', '/credits', '/top-up', '/ads', '/setup/permissions', '/voice']}}];

test('redirect recovery does not add a banner outside your local shell across domains', () => {
  const source = readFileSync(__dirname + '/LocalModeBoundary.tsx', 'utf8');
  for (const [ventureId, domain] of [['ven_inventory', 'Factory stock planning'], ['ven_balcony', 'Balcony gardening']]) {
    for (const local of [true, false]) {
      const exports = {}, jsx = (type, props) => ({type, props});
      vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {
        module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
      }}).outputText, {exports, require(name) {
        if (name === 'react') return {createContext: () => ({Provider: 'mode'}), useEffect() {},
          useState: initial => [initial === null ? capabilities : initial, () => {}]};
        if (name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
        if (name === 'next/navigation') return {usePathname: () => `/v/${ventureId}`,
          useSearchParams: () => new URLSearchParams({local_notice: '1'}), useRouter: () => ({})};
        if (name.endsWith('/LocalSetup')) return {LocalProviderGate: 'setup'};
        if (name === '@/lib/localMode') return {...require('../../lib/localMode.ts'), isLocalConsole: () => false};
        if (name === '@/lib/appResume') return require('../../lib/appResume.ts');
        if (name === '@/lib/localFeatureVisibility') return visibility;
        if (name === '@/lib/client') return {api: () => assert.fail('Rendering recovery must not start work')};
        throw Error(name);
      }});
      const children = {type: 'workspace', props: {domain}};
      const boundary = exports.LocalModeBoundary({children, local});
      const content = boundary.props.children.props.children;
      if (local) {
        assert.ok(!content.props.children[0]);
        assert.equal(content.props.children[1].props.children.props.children, children, domain);
      } else assert.equal(content, children, domain);
    }
  }
});

test('local chat opens while venture validation runs, but deep links still wait and missing ventures redirect', async () => {
  const source = readFileSync(__dirname + '/LocalModeBoundary.tsx', 'utf8');
  const resume = require('../../lib/appResume.ts');
  for (const [ventureId, domain] of [['ven_inventory', 'Factory stock planning'], ['ven_balcony', 'Balcony gardening']]) {
    for (const status of [404, 200, 401, 500, 0]) for (const saved of [`/v/${ventureId}`, `/new?venture=${ventureId}`, `/v/${ventureId}/research`, `/gtm?venture=${ventureId}`]) {
      const values = new Map();
      const storage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
      resume.resumeAppRoute(storage, 'owner', saved, false);
      resume.resumeAppRoute(storage, 'other-owner', saved, false);
      const routes = [], requests = [], effects = [], exports = {};
      let checked = false;
      const jsx = (type, props) => ({type, props});
      vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {
        module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
      }}).outputText, {exports, window: {localStorage: storage}, require(name) {
        if (name === 'react') return {createContext: () => ({Provider: 'mode'}),
          useState: initial => [initial === null ? capabilities : typeof initial === 'string' ? initial : checked, value => {checked = value;}], useEffect: callback => effects.push(callback)};
        if (name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
        if (name === 'next/navigation') return {usePathname: () => saved.split('?')[0], useSearchParams: () => new URL(saved, 'https://console.invalid').searchParams, useRouter: () => ({replace: href => routes.push(href)})};
        if (name.endsWith('/LocalSetup')) return {LocalProviderGate: 'setup'};
        if (name === '@/lib/localMode') return {...require('../../lib/localMode.ts'), isLocalConsole: () => false};
        if (name === '@/lib/appResume') return resume;
        if (name === '@/lib/localFeatureVisibility') return visibility;
        if (name === '@/lib/client') return {ensureSession: async () => ({ok: true}), verifiedSessionSubject: () => 'owner',
          api: async path => {requests.push(path); return {ok: status === 200, status};}};
        throw Error(name);
      }});
      assert.equal(typeof exports.LocalVentureGate, 'function');
      const children = {type: 'workspace', props: {domain}};
      const boundary = exports.LocalModeBoundary({children, local: true});
      const gate = boundary.props.children.props.children.props.children[1].props.children;
      assert.equal(gate.type, exports.LocalVentureGate);
      assert.equal(gate.props.ventureId, ventureId);
      effects.length = 0;
      const render = () => exports.LocalVentureGate(gate.props);
      const loading = render();
      const firstChat = saved === `/v/${ventureId}`;
      assert.equal(loading === children, firstChat);
      const cleanup = effects.shift()();
      await new Promise(resolve => setImmediate(resolve));
      assert.deepEqual(requests, [`/api/ventures/${ventureId}`]);
      if (status === 404) {
        assert.deepEqual(routes, ['/']);
        assert.equal(resume.resumeAppRoute(storage, 'owner', '/', true), null);
        assert.notEqual(render(), children);
      } else {
        assert.deepEqual(routes, []);
        assert.equal(render(), children);
        assert.equal(resume.resumeAppRoute(storage, 'owner', '/', true), saved);
      }
      assert.equal(resume.resumeAppRoute(storage, 'other-owner', '/', true), saved);
      resume.resumeAppRoute(storage, 'owner', '/v/another', false);
      resume.forgetAppRoute(storage, 'owner', ventureId);
      assert.equal(resume.resumeAppRoute(storage, 'owner', '/', true), '/v/another');
      cleanup();
    }
  }
});

test('local hidden deep links never mount hosted screens and redirect with a notice across domains', () => {
  const source = readFileSync(__dirname + '/LocalModeBoundary.tsx', 'utf8');
  const mode = require('../../lib/localMode.ts');
  for (const ventureId of ['ven_procurement', 'ven_family_recipes']) {
    for (const pathname of ['/admin/login', '/operator', '/lab/diagrams', '/pagebuilder-lab', '/auth/device/approve', '/billing', '/top-up', '/invitations',
      '/ads', '/setup/permissions', '/voice', `/v/${ventureId}/import`,
      `/v/${ventureId}/credits`, `/v/${ventureId}/settings`]) {
      const routes = [], effects = [], exports = {};
      const jsx = (type, props) => ({type, props});
      vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {
        module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
      }}).outputText, {exports, window: {}, require(name) {
        if (name === 'react') return {createContext: value => ({Provider: 'mode', value}),
          useState: initial => [initial === null ? capabilities : initial, () => {}],
          useContext: context => context.value, useEffect: callback => effects.push(callback)};
        if (name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
        if (name === 'next/navigation') return {usePathname: () => pathname,
          useRouter: () => ({replace: href => routes.push(href)}),
          useSearchParams: () => new URLSearchParams({venture: ventureId})};
        if (name.endsWith('/LocalSetup')) return {LocalProviderGate: 'setup'};
        if (name === '@/lib/localMode') return mode;
        if (name === '@/lib/appResume') return require('../../lib/appResume.ts');
        if (name === '@/lib/localFeatureVisibility') return visibility;
      if (name === '@/lib/client') return {ensureSession: async () => ({ok: true}), api: async () => ({ok: true, data: {capabilities}})};
        throw Error(name);
      }});
      const children = {type: 'hosted-screen', props: {}};
      const local = exports.LocalModeBoundary({children, local: true});
      assert.ok(mode.isHostedOnlyPath(pathname) || !visibility.surfaceVisible(capabilities, 'paths', pathname, true), pathname);
      assert.equal(JSON.stringify(local).includes('hosted-screen'), false, pathname);
      effects.splice(0).forEach(callback => callback());
      assert.deepEqual(routes, [pathname.endsWith('/settings') ? '/settings/local' : `/v/${ventureId}?local_notice=1`], pathname);
      assert.equal(JSON.stringify(local).includes('That screen is not in this version'), false);
      exports.LocalModeBoundary({children, local: false});
      effects.splice(0).forEach(callback => callback());
      assert.equal(routes.length, 1, 'Hosted navigation stays unchanged');
    }
    for (const pathname of [`/v/${ventureId}/site`, `/v/${ventureId}/research`, `/v/${ventureId}/builds`,
      `/v/${ventureId}/device-work-orders/order`, '/content', '/connections', '/settings/local', '/setup']) {
      assert.equal(mode.isHostedOnlyPath(pathname), false, pathname);
    }
  }
});

test('the venture home opens before optional features load and retries a failed read', async () => {
  const compiled = ts.transpileModule(readFileSync(__dirname + '/LocalModeBoundary.tsx', 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText;
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(5).flatMap(nodes)];
  for (const [ventureId, failure] of [['ven_inventory', {ok: false}], ['ven_balcony', new Error('Network failure')]]) {
    const slots = [], effects = [], contexts = new Map(), requests = [], exported = {};
    const jsx = (type, props) => ({type, props});
    const router = {replace: href => assert.fail(`Unexpected redirect to ${href}`)};
    let cursor = 0, pathname = `/v/${ventureId}`, ownerVerified = false, readBeforeSession = false;
    vm.runInNewContext(compiled, {exports: exported, require(name) {
      if (name === 'react') return {
        createContext: value => ({Provider: Symbol(), value}), useContext: context => contexts.get(context.Provider) ?? context.value,
        useState(initial) {
          const index = cursor++;
          if (!(index in slots)) slots[index] = initial;
          return [slots[index], value => {slots[index] = typeof value === 'function' ? value(slots[index]) : value;}];
        },
        useEffect(callback, deps) {
          const index = cursor++, prior = slots[index];
          if (!prior || deps.some((value, dep) => prior[dep] !== value)) {
            slots[index] = deps;
            effects.push(callback);
          }
        },
      };
      if (name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (name === 'next/navigation') return {usePathname: () => pathname, useRouter: () => router, useSearchParams: () => new URLSearchParams()};
      if (name.endsWith('/LocalSetup')) return {LocalProviderGate: 'setup'};
      if (name === '@/lib/localMode') return require('../../lib/localMode.ts');
      if (name === '@/lib/appResume') return require('../../lib/appResume.ts');
      if (name === '@/lib/localFeatureVisibility') return visibility;
      if (name === '@/lib/client') return {ensureSession: async () => {ownerVerified = true; return {ok: true};}, api: async path => {
        readBeforeSession ||= !ownerVerified;
        requests.push(path);
        if (requests.length > 1) return {ok: true, data: {capabilities}};
        if (failure instanceof Error) throw failure;
        return failure;
      }};
      throw Error(name);
    }});
    const render = () => {
      cursor = 0;
      const tree = exported.LocalModeBoundary({children: jsx('workspace', {}), local: true});
      for (const node of [tree, tree.props.children]) contexts.set(node.type, node.props.value);
      return nodes(tree);
    };
    const flush = async () => {
      effects.splice(0).forEach(effect => effect());
      await new Promise(resolve => setImmediate(resolve));
    };
    pathname = '/';
    assert.ok(render().some(node => node.type === 'workspace'), 'The venture list opens before optional features finish loading');
    pathname = `/v/${ventureId}`;
    assert.ok(render().some(node => node.type === 'workspace'), 'Venture home can open before optional features finish loading');
    await flush();
    assert.ok(render().some(node => node.type === 'button' && node.props.children === 'Try again'), 'Show an in-place retry');
    pathname = '/settings/local';
    assert.ok(render().some(node => node.type === 'workspace'));
    const visible = exported.useFeatureVisibility();
    for (const section of ['providers', 'search', 'smtp', 'publishing', 'composio', 'spend', 'data']) {
      assert.equal(visible('settings', section), true, section);
    }
    assert.equal(visible('settings', 'admin'), false);
    assert.equal(visible('paths', '/ads'), false);
    assert.equal(visible('actions', 'social_post'), false);
    pathname = `/v/${ventureId}`;
    const blocked = render();
    assert.ok(blocked.some(node => node.type === 'workspace'), 'A failed optional read leaves the venture home usable');
    pathname = `/v/${ventureId}/now`;
    assert.ok(!render().some(node => node.type === 'workspace'), 'Other screens stay closed while feature visibility is unknown');
    pathname = `/v/${ventureId}`;
    blocked.find(node => node.type === 'button').props.onClick();
    const loading = render();
    assert.ok(!loading.some(node => node.type === 'button'));
    assert.ok(loading.some(node => node.type === 'workspace'));
    await flush();
    assert.deepEqual(requests, ['/api/local/capabilities', '/api/local/capabilities']);
    assert.equal(readBeforeSession, false, 'A capability read must capture the verified browser session');
    assert.ok(render().some(node => node.type === 'workspace'));
    assert.equal(exported.useFeatureVisibility()('actions', 'status'), true);
    assert.equal(exported.useFeatureVisibility()('paths', '/ads'), false);
  }
});
