const assert = require('node:assert/strict');
const { existsSync, readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');
const hostedAuthTest = existsSync(join(__dirname, '../../app/auth/consume/page.tsx')) ? test : test.skip;

function harness({ component = 'boundary', startupDomain, updateResult, purpose = 'magic_link', device = null, deviceParams = {}, authOk = true, authPromise = null, approvalResult = { ok: true, data: { redirect: 'supymem://auth/callback?code=one-time&state=fixture', code: 'one-time' } }, storage = new Map(), storageDenied = null, deviceSignedIn = true, now = Date.now(), genuineShellCustody = false, local = false } = {}) {
  let cursor = 0, effects = [], tree, shell = false, throwLaunch = false;
  const hooks = [], listeners = new Map(), pending = [], launches = [], routes = [], exchanges = [], timers = new Map();
  let timerId = 0;
  let clock = now, lastShellPath;
  class ClockDate extends Date { static now() { return clock; } }
  const location = { origin: 'https://app.example.invalid', hostname: 'app.example.invalid', pathname: component === 'boundary' ? '/v/ven_a/builds' : component === 'consume' ? '/auth/consume' : component === 'device' ? '/auth/device' : '/auth/cognito/callback', search: '?app=app_a&run=run_a', hash: '#build-review',
    assign(url) { if (throwLaunch) throw Error('blocked'); launches.push(url); } };
  const window = { location, sessionStorage: { getItem: key => { if (storageDenied === 'get') throw Error('storage unavailable'); return storage.get(key) || null; }, setItem: (key, value) => { if (storageDenied === 'set') throw Error('storage unavailable'); storage.set(key, value); }, removeItem: key => storage.delete(key) },
    addEventListener: (name, fn) => listeners.set('window:' + name, fn), removeEventListener: name => listeners.delete('window:' + name),
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; }, clearTimeout: id => timers.delete(id), history: { replaceState(_, __, path) { routes.push('history:' + path); } } };
  if (storageDenied === 'access') Object.defineProperty(window, 'sessionStorage', { get() { throw Error('storage unavailable'); } });
  const document = { visibilityState: 'visible', addEventListener: (name, fn) => listeners.set('document:' + name, fn), removeEventListener: name => listeners.delete('document:' + name) };
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const react = {
    useState(initial) { const index = cursor++, hook = hooks[index] ||= { value: typeof initial === 'function' ? initial() : initial }; return [hook.value, next => { hook.value = typeof next === 'function' ? next(hook.value) : next; }]; },
    useRef(initial) { const index = cursor++; return hooks[index] ||= { current: initial }; },
    useMemo(fn, deps) { const index = cursor++, prior = hooks[index]; if (!prior || !same(prior.deps, deps)) hooks[index] = { deps, value: fn() }; return hooks[index].value; },
    useCallback(fn, deps) { const index = cursor++, prior = hooks[index]; if (!prior || !same(prior.deps, deps)) hooks[index] = { deps, value: fn }; return hooks[index].value; },
    useEffect(fn, deps) { const index = cursor++, prior = hooks[index]; if (!prior || !same(prior.deps, deps)) effects.push(() => { prior?.cleanup?.(); hooks[index] = { deps, fn, cleanup: fn() }; }); },
    Suspense: props => props.children,
    Fragment: 'fragment',
  };
  const jsx = (type, props, key) => typeof type === 'function'
    ? type.name === 'SignedInDeviceContinue' && !deviceSignedIn ? { type: 'synthetic-signed-out-shell', props: {} } : type(props || {})
    : { type, props: props || {}, key };
  function compile(path, requireModule) {
    const exports = {};
    const output = ts.transpileModule(readFileSync(path, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    vm.runInNewContext(output, { exports, require: requireModule, window, document, URL, URLSearchParams, AbortController, Date: ClockDate, Number });
    return exports;
  }
  const lib = compile(join(__dirname, '../../lib/desktopVentureLink.ts'), () => { throw Error('unexpected link import'); });
  const deviceLib = compile(join(__dirname, '../../lib/deviceIntent.ts'), () => { throw Error('unexpected device import'); });
  const signInLib = compile(join(__dirname, '../../lib/signInReturn.ts'), () => { throw Error('unexpected sign-in import'); });
  const portalLib = compile(join(__dirname, '../../lib/webPortal.ts'), () => { throw Error('unexpected portal import'); });
  const shellSource = ts.createSourceFile('AppShell.tsx', readFileSync(join(__dirname, '../layout/AppShell.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let shellCustody;
  function findCustody(node) {
    if (ts.isCallExpression(node) && node.expression.getText(shellSource) === 'useEffect'
      && node.arguments[1]?.getText(shellSource) === '[local, pathname]' && node.arguments[0]?.getText(shellSource).includes('readDeviceIntent()')) {
      shellCustody = node.arguments[0].getText(shellSource);
    }
    ts.forEachChild(node, findCustody);
  }
  if (genuineShellCustody) { findCustody(shellSource); assert.ok(shellCustody, 'use the genuine AppShell pathname effect'); }
  if (device) deviceLib.persistDeviceIntent(device);
  let query = new URLSearchParams(component === 'consume' ? { token: 'synthetic-single-use', purpose } : component === 'device' ? deviceParams : { code: 'synthetic-code', state: 'synthetic-state' });
  const nativeCalls = [];
  const desktop = { isDesktopShell: () => shell, waitForDesktopShell: (_, __, signal) => new Promise(resolve => pending.push({ resolve, signal })),
    desktopInvokeOrThrow: async (command, args) => { nativeCalls.push({ command, args }); return updateResult; },
    desktopListen: async (name, fn) => { listeners.set('native:' + name, fn); return () => listeners.delete('native:' + name); } };
  const file = component === 'boundary' ? join(__dirname, 'DesktopProductBoundary.tsx') : component === 'update' ? join(__dirname, 'DesktopUpdateGate.tsx') : join(__dirname, '../../app/auth', component === 'consume' ? 'consume/page.tsx' : component === 'device' ? 'device/page.tsx' : 'cognito/callback/page.tsx');
  const source = compile(file, id => {
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
    if (id === 'next/navigation') return { usePathname: () => location.pathname, useSelectedLayoutSegments: () => location.pathname === '/missing' ? ['_not-found'] : [], useSearchParams: () => query, useRouter: () => ({ replace: path => routes.push(path) }) };
    if (id === '@/lib/desktop') return component === 'boundary' ? desktop : { ...desktop, waitForDesktopShell: async () => shell };
    if (id === '@/lib/signInReturn') return signInLib;
    if (id === '@/lib/desktopVentureLink') return lib;
    if (id === '@/lib/webPortal') return portalLib;
    if (id === '@/lib/deviceIntent') return deviceLib;
    if (id === '@/lib/invitationRecovery') return { invitationRecoveryMessage: () => null };
    if (id === '@/lib/passwordPolicy') return { PASSWORD_REQUIREMENTS: 'source fixture', passwordSetError: () => null };
    if (id === '@/components/layout/AppShell') return { AppShell: props => {
      if (genuineShellCustody && lastShellPath !== location.pathname) {
        lastShellPath = location.pathname;
        // React runs child passive effects before the DeviceApprovalReturn parent.
        effects.unshift(() => vm.runInNewContext(ts.transpileModule(`(${shellCustody})()`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText,
          { local, readDeviceIntent: deviceLib.readDeviceIntent, pathname: location.pathname }));
      }
      return props.children;
    } };
    if (id === '@/components/primitives/ErrorNotice') return { ErrorNotice: props => ({ type: 'notice', props: { ...props, children: props.error } }) };
    if (id === '@/lib/client') return { api: async (...args) => { exchanges.push(args); return approvalResult; }, resolveGateway: () => 'https://gateway-fixture.invalid', verifyEmail: async token => { exchanges.push(token); return authPromise || { ok: authOk }; }, consumeMagicLink: async token => { exchanges.push(token); return authPromise || { ok: authOk }; }, consumePasswordReset: async (...args) => { exchanges.push(args); return authPromise || { ok: authOk }; }, completeCognitoLogin: async (...args) => { exchanges.push(args); return authPromise || { ok: authOk }; } };
    if (id.endsWith('/LocalModeBoundary')) return { useLocalMode: () => local };
    throw Error('unexpected import ' + id);
  });
  function render() { cursor = 0; tree = component === 'boundary' ? source.DesktopProductBoundary({ children: { type: 'venture-workflows', props: { startupDomain } } }) : component === 'update' ? source.DesktopUpdateGate() : source.default(); const active = effects; effects = []; active.forEach(fn => fn()); return tree; }
  return { render, get tree() { return tree; }, launches, routes, exchanges, pending, location, storage, get query() { return query; }, timers,
    setTime(value) { clock = value; }, signIn() { deviceSignedIn = true; },
    changeQuery(params) { query = new URLSearchParams(params); },
    replayEffects() { hooks.forEach(hook => hook?.cleanup?.()); hooks.forEach(hook => { if (hook?.fn) hook.cleanup = hook.fn(); }); },
    runTimers() { const ready = [...timers.values()]; timers.clear(); ready.forEach(timer => timer.callback()); },
    persistDevice: intent => deviceLib.persistDeviceIntent(intent),
    retireDevice: () => deviceLib.readDeviceIntent(),
    dispose() { hooks.forEach(hook => hook?.cleanup?.()); },
    nativeCalls, ready() { listeners.get('window:supymem-desktop-ready')?.(); },
    shell(value) { shell = value; }, block() { throwLaunch = true; }, focus() { listeners.get('window:focus')?.(); },
    nativeError(value) { listeners.get('native:supymem-link-error')?.(value); }, remember(path) { lib.rememberDesktopLink(location.origin, path); } };
}
function nodes(tree) { if (!tree || typeof tree !== 'object') return []; if (Array.isArray(tree)) return tree.flatMap(nodes); return [tree, ...nodes(tree.props?.children)]; }
function text(tree) { if (typeof tree === 'string') return tree; if (Array.isArray(tree)) return tree.map(text).join(' '); return tree && typeof tree === 'object' ? text(tree.props?.children) : ''; }
const settle = () => new Promise(resolve => setImmediate(resolve));
function button(h) { return nodes(h.tree).find(node => node.type === 'button' && node.props.className === 'primary'); }

test('local business and consumer workspaces open in your browser without native commands', () => {
  for (const startupDomain of ['dispatch-software.invalid', 'garden-club.invalid']) {
    const app = harness({startupDomain, local: true});
    const tree = app.render();
    assert.ok(nodes(tree).some(node => node.type === 'venture-workflows'));
    assert.equal(app.nativeCalls.length, 0);
    assert.equal(app.exchanges.length, 0);
    app.dispose();
  }
});

test('a late bridge opens the same startup without focus or navigation', async () => {
  for (const startupDomain of ['linear.app', 'alltrails.com']) {
    const h = harness({ startupDomain }); h.render();
    h.pending[0].resolve(false); await settle(); h.render();
    assert.match(text(h.tree), /Supymem runs on your Mac/);
    h.shell(true); h.ready(); h.render();
    const workspace = nodes(h.tree).find(node => node.type === 'venture-workflows');
    assert.ok(workspace, startupDomain);
    assert.equal(workspace.props.startupDomain, startupDomain);
    assert.equal(h.launches.length, 0); assert.equal(h.exchanges.length, 0);
    h.dispose();
  }
});

test('Mac approval pages never mount a browser-only auth request for either startup', () => {
  for (const startupDomain of ['linear.app', 'alltrails.com']) for (const pathname of ['/auth/device', '/auth/device/approve']) {
    const h = harness({ startupDomain }); h.location.pathname = pathname;
    h.location.href = `${h.location.origin}${pathname}?device_name=${encodeURIComponent(startupDomain)}`; h.shell(true);
    h.render(); h.render();
    assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), false, startupDomain);
    assert.match(text(h.tree), /browser/);
    assert.equal(nodes(h.tree).find(node => node.type === 'a').props.href, '/');
    assert.equal(nodes(h.tree).find(node => node.type === 'input').props.value, h.location.href);
    assert.equal(h.nativeCalls.length, 0); assert.equal(h.exchanges.length, 0);
    h.dispose();
  }
});

test('a late bridge still offers optional and mandatory updates for both startups', async () => {
  for (const startupDomain of ['linear.app', 'alltrails.com']) for (const force of [false, true]) {
    const h = harness({ component: 'update', startupDomain, updateResult: {
      available: true, current_version: '0.1.108', version: '0.1.109', mandatory: force,
      force, can_defer: !force, endpoint: 'https://updates.invalid/latest.json', check_id: startupDomain,
    } });
    h.render(); assert.equal(h.nativeCalls.length, 0);
    h.shell(true); h.ready(); h.render(); await settle(); h.render();
    assert.match(text(h.tree), force ? /Update required/ : /Update available/);
    assert.equal(nodes(h.tree).some(node => node.type === 'button' && text(node) === 'Later'), !force);
    assert.deepEqual(h.nativeCalls.map(call => call.command), ['check_for_update']);
    button(h).props.onClick(); await settle();
    assert.deepEqual(JSON.parse(JSON.stringify(h.nativeCalls.at(-1))), { command: 'install_update', args: { checkId: startupDomain } });
    h.dispose();
  }
});

test('ordinary browsers never mount product children, before or after startup detection', async () => {
  for (const pathname of ['/', '/new', '/v/x', '/v/x/research', '/v/ven_a/idea', '/v/ven_a/research', '/v/ven_a/site', '/gtm', '/account', '/operator', '/ops', '/authentication', '/download/extra', '/administrator', '/invite']) {
    const h = harness(); h.location.pathname = pathname;
    h.render(); h.render();
    assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), false, pathname);
    h.pending.at(-1).resolve(false); await settle(); h.render();
    assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), false, pathname);
    assert.match(text(h.tree), /Supymem runs on your Mac/);
    assert.deepEqual(h.routes, []); assert.deepEqual(h.launches, []); assert.deepEqual(h.exchanges, []);
    assert.ok(button(h)); assert.ok(nodes(h.tree).some(node => node.type === 'a' && text(node) === 'Install Supymem for Mac'));
    h.focus(); h.render();
    assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), false, 'focus keeps product unmounted');
  }
});

test('browser auth, download, invites, admin, legal and not-found remain accessible', async () => {
  for (const pathname of ['/auth', '/auth/device/approve', '/auth/cognito/callback', '/download', '/invite/token', '/admin', '/admin/users', '/privacy', '/terms', '/missing']) {
    const h = harness(); h.location.pathname = pathname;
    h.render(); assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), true, pathname);
    h.pending[0].resolve(false); await settle(); h.render();
    assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), true, pathname);
  }
});

test('desktop renders product children except browser-only Mac approval', () => {
  for (const pathname of ['/', '/v/x', '/v/x/research', '/account', '/download']) {
    const h = harness(); h.location.pathname = pathname; h.shell(true); h.render(); h.render();
    assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), true, pathname);
  }
});

test('late native discovery on account and founder screens remounts auth subscriptions and retains native link errors', async () => {
  for (const pathname of ['/account', '/v/ven_a/idea']) {
  const h = harness(); h.location.pathname = pathname; h.render(); h.render();
  h.pending[0].resolve(false); await settle(); h.render();
  assert.match(text(h.tree), /Supymem runs on your Mac/);
  h.focus(); const old = h.pending.at(-1); h.shell(true); h.focus(); h.render(); h.render(); await settle();
  assert.equal(h.tree.key, 'mac', 'mode change remounts the real shell and native auth subscriptions');
  h.nativeError('not_available'); h.render();
  assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), true);
  assert.match(text(h.tree), /account/);
  old.resolve(false); await settle(); h.render();
  assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), true);
  assert.match(text(h.tree), /account/);
  assert.deepEqual(h.routes, []); assert.deepEqual(h.launches, []);
  }
});

test('auth token pages keep a stable subtree during bridge detection', async () => {
  const h = harness(); h.location.pathname = '/auth/consume'; h.render(); h.render();
  assert.equal(h.tree.key, 'auth'); assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), true);
  h.pending[0].resolve(false); await settle(); h.render(); assert.equal(h.tree.key, 'auth');
  h.shell(true); h.focus(); h.render(); assert.equal(h.tree.key, 'auth');
});

test('browser Mac reviews keep device controls unmounted and offer only an explicit exact native handoff', async () => {
  const h = harness(); h.location.pathname = '/v/ven_a/device-work-orders/dwo_a'; h.location.search = ''; h.location.hash = '';
  h.render(); h.pending[0].resolve(false); await settle(); h.render(); h.render();
  assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), false);
  assert.match(text(h.tree), /Supymem runs on your Mac/); assert.deepEqual(h.exchanges, []);
  assert.deepEqual(h.launches, []); button(h).props.onClick(); h.render();
  assert.equal(new URL(h.launches[0]).searchParams.get('path'), '/v/ven_a/device-work-orders/dwo_a');
  assert.match(text(h.tree), /cannot confirm whether the app opened/);
  h.location.search = '?next=https://evil.test'; button(h).props.onClick(); h.render();
  assert.equal(h.launches.length, 2); assert.equal(new URL(h.launches[1]).searchParams.get('path'), '/');
  h.block(); button(h).props.onClick(); h.render(); assert.match(text(h.tree), /could not launch/);
  h.shell(true); h.focus(); h.render();
  assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), true);
});

hostedAuthTest('browser email and magic entry return to the gate; device approval takes precedence', async () => {
  for (const purpose of ['magic_link', 'email_verify']) {
    const h = harness({ component: 'consume', purpose }); h.remember('/v/ven_a/site?create=1'); h.render(); await settle(); h.render();
    assert.deepEqual(h.exchanges, ['synthetic-single-use']); assert.ok(h.routes.includes('history:/auth/consume')); assert.equal(h.storage.size, 0);
    button(h).props.onClick(); assert.equal(h.routes.at(-1), '/'); h.render(); assert.equal(h.launches.length, 0);
    if (purpose === 'magic_link') assert.match(text(h.tree), /You are signed in\. Return to the Supymem app\./);
  }
  const device = { version: 1, code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  const h = harness({ component: 'consume', device }); h.remember('/v/ven_a'); h.render(); await settle(); h.render(); button(h).props.onClick();
  assert.equal(h.launches.length, 0); assert.match(h.routes.at(-1), /^\/auth\/device\?/);
});

hostedAuthTest('browser SSO sends retained product routes to the gate', async () => {
  const h = harness({ component: 'cognito' }); h.remember('/v/ven_a/builds?app=app_a&run=run_a#build-review'); h.render(); await settle(); h.render();
  assert.equal(h.routes.at(-1), '/'); assert.equal(h.launches.length, 0); assert.equal(h.storage.size, 0);
});

hostedAuthTest('desktop SSO and magic links preserve the exact retained venture destination', async () => {
  for (const component of ['cognito', 'consume']) {
    const h = harness({ component }); h.shell(true); h.remember('/v/ven_a/site?create=1'); h.render(); await settle(); h.render();
    if (component === 'consume') h.runTimers();
    assert.equal(h.routes.at(-1), '/v/ven_a/site?create=1');
  }
});

hostedAuthTest('browser device approval finishes in auth with an app link and one-time fallback, never product navigation', async () => {
  const deviceParams = { code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  const h = harness({ component: 'device', deviceParams }); h.location.pathname = '/auth/device/approve';
  h.render(); h.render(); assert.equal(h.launches.length, 0); assert.equal(h.exchanges.length, 0);
  const approve = button(h); approve.props.onClick(); h.render(); approve.props.onClick(); await settle(); h.render();
  assert.equal(h.exchanges.length, 1); assert.equal(h.exchanges[0][0], '/auth/device/approve');
  assert.match(text(h.tree), /You are signed in\. Return to the Supymem app\./);
  assert.equal(nodes(h.tree).find(node => node.type === 'a').props.href, 'supymem://auth/callback?code=one-time&state=fixture');
  assert.equal(nodes(h.tree).find(node => node.type === 'input').props.value, 'one-time');
  assert.equal(h.storage.size, 0); assert.deepEqual(h.routes, []); assert.deepEqual(h.launches, []);
});

hostedAuthTest('failed browser approval leaves a retry action and cannot show an untrusted callback link', async () => {
  for (const approvalResult of [{ ok: false }, { ok: true, data: { redirect: 'https://evil.test', code: 'one-time' } }]) {
    const deviceParams = { code_challenge: 'c'.repeat(43), state: 's'.repeat(43) };
    const h = harness({ component: 'device', deviceParams, approvalResult }); h.location.pathname = '/auth/device/approve';
    h.render(); h.render(); button(h).props.onClick(); await settle(); h.render();
    assert.match(text(h.tree), /Try again/); assert.equal(button(h).props.disabled, false);
    assert.equal(nodes(h.tree).some(node => node.type === 'a'), false);
    button(h).props.onClick(); await settle(); h.render(); assert.equal(h.exchanges.length, 2);
    assert.deepEqual(h.routes, []); assert.deepEqual(h.launches, []);
  }
});

hostedAuthTest('browser SSO unavailable screen uses neutral Supymem copy', async () => {
  const h = harness({ component: 'cognito', authPromise: Promise.resolve({ ok: false, data: { error: { code: 'identity_unavailable' } } }) });
  h.render(); await settle(); h.render();
  assert.match(text(h.tree), /Supymem sign-in is temporarily unavailable/);
  assert.doesNotMatch(text(h.tree), /Cognito|amazoncognito|us-east-1|prod/);
});

hostedAuthTest('invalid and expired verification links offer one safe sign-in recovery and retire stale Mac intent', async () => {
  for (const incomplete of [false, true]) {
    const device = { version: 1, code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
    const h = harness({ component: 'consume', purpose: 'email_verify', device, authOk: false });
    h.remember('/v/ven_a');
    if (incomplete) h.query.delete('token');
    h.render(); await settle(); h.render();
    assert.equal(nodes(h.tree).filter(node => node.type === 'button' && node.props.className === 'primary').length, 1);
    assert.equal(text(button(h)), 'Return to sign in');
    button(h).props.onClick();
    assert.equal(h.routes.at(-1), '/auth'); assert.equal(h.storage.size, 0); assert.equal(h.launches.length, 0);
    assert.deepEqual(h.exchanges, incomplete ? [] : ['synthetic-single-use']);
  }
});

test('browser homepage has an explicit app launch without mounting the founder shell', async () => {
  const h = harness(); h.location.pathname = '/'; h.location.search = ''; h.location.hash = '';
  h.render(); h.pending[0].resolve(false); await settle(); h.render();
  assert.deepEqual(h.routes, []);
  assert.equal(nodes(h.tree).some(node => node.type === 'venture-workflows'), false);
  assert.equal(h.launches.length, 0);
  button(h).props.onClick(); assert.equal(new URL(h.launches[0]).searchParams.get('path'), '/');
});

hostedAuthTest('synthetic rendered Mac handoff retires intent before later ordinary Google and email callbacks', async () => {
  const device = { code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  for (const component of ['cognito', 'consume']) {
    const h = harness({ component: 'device', deviceParams: device });
    h.render(); assert.equal(h.storage.size, 1);
    h.render(); assert.equal(h.launches.length, 1); assert.equal(h.storage.size, 0);
    const url = new URL(h.launches[0]);
    assert.equal(url.pathname, '/auth/device/approve');
    assert.equal(url.searchParams.get('code_challenge'), device.code_challenge);
    assert.equal(url.searchParams.get('state'), device.state);
    const ordinary = harness({ component, storage: h.storage }); ordinary.remember('/v/ven_a/research');
    ordinary.render(); await settle(); ordinary.render();
    if (component === 'consume') button(ordinary).props.onClick();
    assert.equal(ordinary.routes.at(-1), '/'); assert.equal(ordinary.launches.length, 0);
  }
});

hostedAuthTest('synthetic abandoned or incomplete Mac requests cannot resume through later ordinary callbacks', async () => {
  const device = { version: 1, code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  for (const component of ['cognito', 'consume']) {
    const h = harness({ component: 'device', deviceParams: device }); h.render();
    h.location.pathname = '/account'; h.dispose(); assert.equal(h.storage.size, 0); assert.equal(h.launches.length, 0);
    const ordinary = harness({ component, storage: h.storage }); ordinary.remember('/v/ven_a/site');
    ordinary.render(); await settle(); ordinary.render();
    if (component === 'consume') button(ordinary).props.onClick();
    assert.equal(ordinary.routes.at(-1), '/'); assert.equal(ordinary.launches.length, 0);
  }
  for (const challenge of ['', 'incomplete']) {
    const h = harness({ component: 'device', device, deviceParams: { code_challenge: challenge } });
    h.render(); h.render(); assert.equal(h.storage.size, 0); assert.equal(h.launches.length, 0);
    assert.match(text(h.tree), /Start again from your Mac/);
  }
});

hostedAuthTest('synthetic auth-consume timers run while active and cannot reopen a Mac request after abandonment', async () => {
  const device = { version: 1, code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  for (const purpose of ['magic_link', 'email_verify']) {
    const active = harness({ component: 'consume', purpose, device }); active.render(); await settle(); active.render();
    assert.equal(active.timers.size, 1); active.runTimers(); assert.match(active.routes.at(-1), /^\/auth\/device\?/);
    const retired = harness({ component: 'consume', purpose, device }); retired.render(); await settle(); retired.render();
    assert.equal(retired.timers.size, 1);
    const deferred = [...retired.timers.values()][0].callback;
    retired.location.pathname = '/account'; retired.retireDevice(); retired.dispose();
    const newer = { ...device, state: 'n'.repeat(43) }; retired.persistDevice(newer);
    const preserved = [...retired.storage]; const routes = [...retired.routes];
    retired.runTimers(); deferred();
    assert.deepEqual(retired.routes, routes); assert.deepEqual([...retired.storage], preserved);
    assert.equal(retired.timers.size, 0);
  }
});

hostedAuthTest('synthetic delayed auth results cannot navigate or consume a newer intent after their page unmounts', async () => {
  const device = { version: 1, code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  for (const component of ['consume', 'cognito']) {
    for (const purpose of component === 'consume' ? ['magic_link', 'email_verify', 'password_reset'] : ['magic_link']) {
      let finish; const authPromise = new Promise(resolve => { finish = resolve; });
      const h = harness({ component, purpose, device, authPromise }); h.render();
      if (purpose === 'password_reset') {
        nodes(h.tree).find(node => node.type === 'input').props.onChange({ target: { value: 'Synthetic-password-123' } }); h.render();
        nodes(h.tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
      }
      h.location.pathname = '/account'; h.retireDevice(); h.dispose();
      h.persistDevice({ ...device, state: 'n'.repeat(43) }); h.remember('/v/ven_new/research');
      const preserved = [...h.storage]; const routes = [...h.routes];
      finish({ ok: true }); await settle(); h.runTimers();
      assert.deepEqual(h.routes, routes, component + ':' + purpose);
      assert.deepEqual([...h.storage], preserved, 'retired callback leaves the newer attempt and route intact');
    }
  }
});

hostedAuthTest('synthetic authenticated current URL reaches Gateway despite optional storage denial', () => {
  const device = { code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  for (const storageDenied of ['access', 'get', 'set']) {
    const h = harness({ component: 'device', deviceParams: device, storageDenied }); h.render(); h.render();
    assert.equal(h.launches.length, 1, storageDenied);
    const target = new URL(h.launches[0]); assert.equal(target.origin, 'https://gateway-fixture.invalid');
    assert.equal(target.searchParams.get('code_challenge'), device.code_challenge); assert.equal(target.searchParams.get('state'), device.state);
    assert.equal(h.storage.size, 0); assert.deepEqual(h.exchanges, []);
  }
});

hostedAuthTest('synthetic current query does not revive a known expired stored Mac attempt', () => {
  const device = { version: 1, code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  const storage = new Map([['supymem.device.connect.intent:v1', JSON.stringify({ ...device, issued_at: Date.now() - 300001 })]]);
  const h = harness({ component: 'device', deviceParams: device, storage }); h.render(); h.render();
  assert.equal(h.launches.length, 0); assert.equal(storage.size, 0); assert.match(text(h.tree), /Start again from your Mac/);
});

hostedAuthTest('synthetic StrictMode effect replay preserves one in-flight credential exchange and active continuation', async () => {
  const device = { version: 1, code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  for (const component of ['consume', 'cognito']) {
    let finish; const authPromise = new Promise(resolve => { finish = resolve; });
    const h = harness({ component, device, authPromise }); h.render(); h.replayEffects();
    assert.equal(h.exchanges.length, 1, 'single-use exchange is not replayed');
    finish({ ok: true }); await settle(); h.render(); h.runTimers();
    assert.match(h.routes.at(-1), /^\/auth\/device\?/); assert.equal(h.exchanges.length, 1);
  }
});

hostedAuthTest('synthetic same-route newer URL challenge keeps its own automatic Gateway continuation', () => {
  const device = { code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  const h = harness({ component: 'device', deviceParams: device }); h.render(); h.render();
  assert.equal(h.launches.length, 1);
  h.changeQuery({ ...device, state: 'n'.repeat(43) }); h.render(); h.render();
  assert.equal(h.launches.length, 2); assert.equal(new URL(h.launches[1]).searchParams.get('state'), 'n'.repeat(43));
  assert.equal(h.storage.size, 0);
});

hostedAuthTest('synthetic delayed authenticated mount refuses expired or future snapshots with available and denied storage', () => {
  const device = { code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  for (const storageDenied of [null, 'access', 'get', 'set']) {
    for (const now of [301001, 999]) {
      const h = harness({ component: 'device', deviceParams: device, storageDenied, deviceSignedIn: false, now: 1000 });
      h.render(); h.render(); assert.equal(h.launches.length, 0);
      h.setTime(now); h.retireDevice(); h.signIn(); h.render(); h.render();
      assert.equal(h.launches.length, 0, String(storageDenied) + ':' + now);
      assert.match(text(h.tree), /Start again from your Mac/);
    }
  }
});

hostedAuthTest('synthetic manual retry checks the captured snapshot age even without another render', () => {
  const device = { code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  for (const storageDenied of [null, 'access', 'get', 'set']) {
    for (const now of [301001, 999]) {
      const h = harness({ component: 'device', deviceParams: device, storageDenied, now: 1000 }); h.render(); h.render();
      assert.equal(h.launches.length, 1); const retry = button(h);
      h.setTime(now); retry.props.onClick(); h.render();
      assert.equal(h.launches.length, 1, String(storageDenied) + ':' + now);
      assert.match(text(h.tree), /Start again from your Mac/);
    }
  }
});

hostedAuthTest('synthetic same-attempt query rerenders cannot renew a denied-storage snapshot while authentication waits', () => {
  const device = { code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  const h = harness({ component: 'device', deviceParams: device, storageDenied: 'access', deviceSignedIn: false, now: 1000 });
  h.render(); h.render(); h.setTime(200000); h.changeQuery({ ...device, device_name: 'Renamed same Mac' }); h.render(); h.render();
  h.setTime(301001); h.signIn(); h.render(); h.render(); assert.equal(h.launches.length, 0);
  h.changeQuery({ ...device, state: 'n'.repeat(43) }); h.render(); h.render();
  assert.equal(h.launches.length, 1); assert.equal(new URL(h.launches[0]).searchParams.get('state'), 'n'.repeat(43));
});

hostedAuthTest('genuine AppShell child custody effect cannot erase an expired current-URL attempt before parent resolution', () => {
  for (const device_name of ['Dispatch workstation', 'Cooking laptop']) {
    const device = { version: 1, code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name, platform: 'macos' };
    const storage = new Map([['supymem.device.connect.intent:v1', JSON.stringify({ ...device, issued_at: 1000 })]]);
    const h = harness({ component: 'device', deviceParams: device, storage, now: 301001, genuineShellCustody: true });
    h.render(); h.render(); assert.equal(h.launches.length, 0);
    assert.match(text(h.tree), /Start again from your Mac/);
  }
});

hostedAuthTest('synthetic partial current intent URLs never borrow a previously stored Mac request', () => {
  const device = { version: 1, code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  for (const deviceParams of [{ state: 'incomplete' }, { device_name: 'Different Mac' }, { platform: 'macos' }, { state: '' }]) {
    const h = harness({ component: 'device', device, deviceParams, now: 1000 }); h.render(); h.render();
    assert.equal(h.launches.length, 0, JSON.stringify(deviceParams)); assert.equal(h.storage.size, 0);
    assert.match(text(h.tree), /Start again from your Mac/);
  }
  const bare = harness({ component: 'device', device, now: 1000 }); bare.render(); bare.render();
  assert.equal(bare.launches.length, 1, 'completely bare continuation retains its valid stored attempt');
});

hostedAuthTest('synthetic simultaneous query and authentication change never hands off the previous render snapshot', () => {
  const device = { code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  const h = harness({ component: 'device', deviceParams: device, deviceSignedIn: false, now: 1000 }); h.render(); h.render();
  h.changeQuery({ ...device, state: 'n'.repeat(43) }); h.signIn(); h.render(); h.render();
  assert.equal(h.launches.length, 1); assert.equal(new URL(h.launches[0]).searchParams.get('state'), 'n'.repeat(43));
});

hostedAuthTest('synthetic A to B to A navigation retains the first timestamp and leaves newer stored custody intact', () => {
  const device = { code_challenge: 'c'.repeat(43), state: 's'.repeat(43), device_name: 'Fixture Mac', platform: 'macos' };
  const h = harness({ component: 'device', deviceParams: device, deviceSignedIn: false, now: 1000 }); h.render(); h.render();
  h.setTime(200000); h.changeQuery({ ...device, state: 'n'.repeat(43) }); h.render(); h.render();
  const preserved = [...h.storage];
  h.setTime(301001); h.changeQuery(device); h.signIn(); h.render(); h.render();
  assert.equal(h.launches.length, 0); assert.deepEqual([...h.storage], preserved);
  assert.match(text(h.tree), /Start again from your Mac/);
});
