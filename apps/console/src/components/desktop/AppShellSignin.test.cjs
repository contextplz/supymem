const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const { renderToStaticMarkup } = require('react-dom/server');
const root = join(__dirname, '../..');
const signIn = {};
vm.runInNewContext(ts.transpileModule(readFileSync(join(root, 'lib/signInReturn.ts'), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText, { exports: signIn });
const shell = readFileSync(join(root, 'components/layout/AppShell.tsx'), 'utf8');

test('venture header keeps readable names and accessible, equal-size icon controls', () => {
  const switcher = readFileSync(join(root, 'components/office/WorkspaceSwitcher.tsx'), 'utf8');
  const css = readFileSync(join(root, 'components/office/office.module.css'), 'utf8');
  const tokens = readFileSync(join(root, 'styles/tokens.css'), 'utf8');
  assert.match(switcher, /className=\{styles.switcherName\} title=\{label\}/);
  assert.match(switcher, /aria-label="Rename venture"\s+title="Rename venture"/);
  assert.match(css, /\.switcherName\s*\{[^}]*-webkit-line-clamp:\s*2/s);
  assert.match(css, /\.switcherRename\s*\{[^}]*width:\s*32px;[^}]*height:\s*32px;/s);
  assert.match(shell, /aria-label="Venture settings"\s+title="Venture settings"/);
  assert.match(shell, /className="ghost sm sidebar-gear sidebar-settings"/);
  assert.match(tokens, /\.sidebar-gear\s*\{[^}]*width:\s*32px;[^}]*height:\s*32px;[^}]*padding:\s*0;/s);
  assert.match(tokens, /@media \(max-width: 1023px\) \{ \.sidebar-gear\.sidebar-pin \{ display: none; \} \}/);
});

test('global greeting and task shell wiring coexist without a duplicate intro', () => {
  const tree = ts.createSourceFile('AppShell.tsx', shell, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  assert.equal(tree.parseDiagnostics.length, 0);
  assert.doesNotMatch(shell, /CharlieIntro/);
  assert.match(readFileSync(join(root, 'app/layout.tsx'), 'utf8'), /<CharlieIntro \/>/);
  assert.match(shell, /<ChatOpenContext\.Provider value=\{chatAvailable && chatPanelOpen\}>/);
  assert.match(shell, /<\/ChatOpenContext\.Provider>/);
  assert.match(shell, /captureFounderTimezone\(effectiveVentureId\)/);
  assert.match(shell, /window\.addEventListener\("supymem-desktop-ready", installed\)/);
});

test('startup timings use closed fields and product paint does not wait on local tools', () => {
  assert.match(shell, /if \(!authed\) return;\s+const painted = \(\) => \{[\s\S]*?window\.requestAnimationFrame\(painted\)/);
  assert.match(shell, /console\.info\("desktop_startup", \{ phase: "product_painted", ms: Math\.round\(performance\.now\(\)\) \}\)/);
  assert.match(shell, /if \(isDesktopShell\(\)\) void desktopInvokeQuiet\("desktop_product_painted"\)/);
  assert.match(readFileSync(join(root, 'lib/client.ts'), 'utf8'), /phase: "session_checked", ms: Date\.now\(\) - started, ok: result\.ok/);
  assert.match(readFileSync(join(root, 'components/desktop/DesktopBoot.tsx'), 'utf8'), /phase: "shell_detected", ms: Date\.now\(\) - started/);
});

test('hosted sign-in has Google and email, while operator passphrase is isolated', () => {
  assert.doesNotMatch(shell, /Operator access/);
  assert.match(shell, /const operatorLogin = pathname === "\/operator"/);
  assert.match(shell, /if \(local && !authed\)/);
  assert.match(shell, /\{!hostedWeb && <div className="login-tabs"/);
  assert.match(shell, /\{operatorLogin \|\| \(!hostedWeb && loginMode === "passphrase"\) \? \(/);
  assert.match(shell, /data-usage-control="account_sign_in_google"/);
  assert.match(shell, /Sign in with Google/);
  assert.match(shell, /Sign in with email/);
  for (const color of ['#4285F4', '#34A853', '#FBBC05', '#EA4335']) assert.ok(shell.includes(color));
  assert.match(readFileSync(join(root, 'app/operator/page.tsx'), 'utf8'), /robots: \{ index: false, follow: false \}/);
});

test('authenticated browser auth and invite pages render their public content without workspace chrome', () => {
  const tree = ts.createSourceFile('AppShell.tsx', shell, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const guard = component.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(tree) === 'browserEntry');
  assert.ok(guard);
  const source = ts.transpileModule(`(function () { ${guard.getText(tree)}; return 'desktop-workspace'; })`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  for (const browserEntry of [true, false]) {
    const render = vm.runInNewContext(source, { React: require('react'), browserEntry, children: 'public-auth-or-invite-content' });
    const html = renderToStaticMarkup(render());
    assert.equal(html.includes('public-auth-or-invite-content'), browserEntry);
    assert.equal(html.includes('desktop-workspace'), !browserEntry);
    assert.doesNotMatch(html, /workspace-body|sidebar|WorkspaceSwitcher|VentureChatLauncher/);
  }
});

test('browser download is readable without signing in or fetching a session', () => {
  const tree = ts.createSourceFile('AppShell.tsx', shell, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const guard = component.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(tree) === 'browserEntry && pathname === "/download"');
  const effect = component.body.statements.find(node => node.getText(tree).includes('await ensureSession('));
  const context = { React: require('react'), browserEntry: true, pathname: '/download', children: 'download-instructions',
    local: false, nativeDesktop: false, sessionAttempt: 0, setChecking() {}, ensureSession() { assert.fail('download must not check a session'); },
    useEffect: run => run() };
  const source = ts.transpileModule(`(function () { ${effect.getText(tree)}; ${guard.getText(tree)} })`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  assert.match(renderToStaticMarkup(vm.runInNewContext(source, context)()), /download-instructions/);
});

test('every console sign-in mark and favicon uses the real Supymem asset', {
  skip: process.env.SUPYMEM_MODE === 'local' && 'Hosted-only branding. Run this test in your hosted checkout.',
}, () => {
  const css = readFileSync(join(root, 'styles/tokens.css'), 'utf8');
  assert.match(css, /background: url\("\/supymem-logo.svg"\) center \/ contain no-repeat/);
  assert.doesNotMatch(css, /%2393c5fd/);
  assert.equal(readFileSync(join(root, 'app/icon.svg'), 'utf8'), readFileSync(join(root, '../public/supymem-logo.svg'), 'utf8'));
});

test('route changes reset the main scroll container and details are scoped to reading pages', () => {
  assert.match(shell, /workspaceBodyRef\.current\.scrollTop = 0/);
  assert.match(shell, /ref=\{workspaceBodyRef\}/);
  assert.match(shell, /effectiveVentureId && showsDetailToggle\(pathname\) && <DetailToggle/);
});

test('home keeps reading controls visible and Mac permission recovery on account pages', () => {
  const tree = ts.createSourceFile('AppShell.tsx', shell, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const details = [];
  const permissionCards = [];
  const visit = node => {
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText(tree) === 'details') details.push(node);
    if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(tree) === 'DesktopTaskPermissionCard') permissionCards.push(node);
    ts.forEachChild(node, visit);
  };
  visit(tree);
  assert.ok(details.every(node => !node.getText(tree).includes('<DetailToggle')));
  assert.equal(permissionCards.length, 3);
  assert.match(shell, /!effectiveVentureId && !entryPage && !\(bare \|\| pathname === "\/"\) && <aside className="sidebar"/);
  const office = readFileSync(join(root, 'components/office/CompanyOffice.tsx'), 'utf8');
  assert.match(office, /className=\{`\$\{styles.canvas\} workspace-body`\} ref=\{scrollRef\}/);
  assert.match(office, /className="cos-row" onClick=\{onSettings\}>Settings<\/button>/);
});

test('primary fills and disabled states outrank frame button resets', () => {
  const tokens = readFileSync(join(root, 'styles/tokens.css'), 'utf8');
  assert.match(tokens, /\.app-shell button\.primary, \.app-shell a\.primary\s*\{\s*background: var\(--accent\)/);
  assert.match(tokens, /\.app-shell button\.primary:hover:not\(:disabled\), \.app-shell a\.primary:hover\s*\{\s*background: var\(--accent-hover\)/);
  assert.match(tokens, /\.app-shell button\.primary:disabled\s*\{\s*background: var\(--bg-3\)/);
});

test('session verification preserves the shell and Mac permission demands; account switches retire them', async () => {
  const tree = ts.createSourceFile('AppShell.tsx', shell, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellFrame');
  const cells = [], effects = [], events = new Map();
  let cursor = 0, pathname = '/account', subject = null, native = true, local = false, ventureId;
  const react = {
    createElement: (type, props, ...children) => ({type, props: {...props, children: children[0]}}),
    useState: initial => {
      const index = cursor++;
      if (!(index in cells)) cells[index] = typeof initial === 'function' ? initial() : initial;
      return [cells[index], value => { cells[index] = typeof value === 'function' ? value(cells[index]) : value; }];
    },
    useRef: current => { const index = cursor++; return cells[index] ??= {current}; },
    useIsoLayoutEffect: (run, dependencies) => {
      const index = cursor++, previous = cells[index];
      if (!previous || dependencies.some((value, offset) => value !== previous.dependencies[offset])) {
        effects.push(() => { previous?.cleanup?.(); cells[index] = {dependencies, cleanup: run()}; });
      }
    },
  };
  const window = {
    addEventListener: (name, handler) => { if (!events.has(name)) events.set(name, new Set()); events.get(name).add(handler); },
    removeEventListener: (name, handler) => events.get(name)?.delete(handler),
  };
  const renderShell = vm.runInNewContext(ts.transpileModule(`(${component.getText(tree)})`, {
    compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
  }).outputText, {
    React: react, ...react, window, usePathname: () => pathname, verifiedSessionSubject: () => subject,
    useLocalMode: () => local,
    isDesktopShell: () => native, MAC_TASK_PERMISSION_EVENT: 'supymem-mac-permissions-needed',
    OfficeProvider: 'OfficeProvider', Suspense: 'Suspense', AppShellInner: 'AppShellInner',
  });
  const render = () => {
    cursor = 0;
    const output = renderShell({children: 'workspace', ventureId});
    while (effects.length) effects.shift()();
    return {key: output.props.key, inner: output.props.children.props.children.props};
  };
  const dispatch = (name, detail) => { for (const handler of events.get(name) ?? []) handler({detail}); };
  const session = next => { subject = next; dispatch('supymem-session-changed', {subject}); };
  let resumed = 0;
  const demand = {resume: () => { resumed++; }};
  render();
  dispatch('supymem-mac-permissions-needed', {resume: 'invalid'});
  assert.equal(render().inner.permissionRequest, null);
  native = false;
  dispatch('supymem-mac-permissions-needed', demand);
  assert.equal(render().inner.permissionRequest, null);
  native = true;
  dispatch('supymem-mac-permissions-needed', demand);
  const restoring = render();
  assert.equal(restoring.key, 0);
  assert.equal(restoring.inner.permissionRequest, demand);
  session('usr_fixture');
  const connected = render();
  assert.equal(connected.key, restoring.key);
  assert.equal(connected.inner.permissionRequest, demand);
  assert.equal(events.get('supymem-mac-permissions-needed').size, 1);
  dispatch('supymem-mac-permissions-needed', demand);
  assert.ok(render().inner.permissionRequestKey > connected.inner.permissionRequestKey);
  session('usr_other');
  assert.notEqual(render().key, connected.key);
  assert.equal(render().inner.permissionRequest, null);
  await connected.inner.onMacTaskPermissionsGranted();
  assert.equal(resumed, 0);
  dispatch('supymem-mac-permissions-needed', demand);
  session(null);
  assert.equal(render().inner.permissionRequest, null);
  session('usr_fixture');
  dispatch('supymem-mac-permissions-needed', demand);
  const beforeNavigation = render();
  pathname = '/settings';
  render();
  assert.equal(render().inner.permissionRequest, null);
  await beforeNavigation.inner.onMacTaskPermissionsGranted();
  assert.equal(resumed, 0);
  dispatch('supymem-mac-permissions-needed', demand);
  const ready = render();
  await ready.inner.onMacTaskPermissionsGranted();
  await ready.inner.onMacTaskPermissionsGranted();
  assert.equal(resumed, 1);
  assert.equal(render().inner.permissionRequest, null);
  for (ventureId of ['ven_dispatch', 'ven_cooking']) {
    local = true;
    pathname = `/v/${ventureId}/tasks`;
    render();
    session(ventureId);
    dispatch('supymem-mac-permissions-needed', demand);
    const owner = render();
    assert.equal(owner.inner.permissionRequest, null);
    await owner.inner.onMacTaskPermissionsGranted();
    assert.equal(resumed, 1, 'local work never resumes a hosted Mac permission demand');
  }
  cells.forEach(value => value?.cleanup?.());
  assert.equal(events.get('supymem-mac-permissions-needed').size, 0);
});

test('actual shell session effect freshly verifies native auth while browser navigation retains its scoped memo', async () => {
  const tree = ts.createSourceFile('AppShell.tsx', shell, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect = '';
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect'
      && node.arguments[0]?.getText(tree).includes('await ensureSession(')) effect = node.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(effect);
  for (const [native, attempt, fresh] of [[true, 0, true], [false, 0, false], [false, 1, true]]) {
    const calls = [], actors = [];
    const run = vm.runInNewContext(ts.transpileModule(`(${effect})`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022 },
    }).outputText, {
      local: false, sessionAttempt: attempt, nativeDesktop: false, browserEntry: false, pathname: '/account', isDesktopShell: () => native,
      ensureSession: async (passphrase, force) => { calls.push([passphrase, force]); return {ok: true, data: {subject: native ? 'native-actor' : 'browser-actor'}}; },
      setChecking() {}, setConnectionError() {}, setAuthed() {}, setSessionSubject: actor => actors.push(actor),
    });
    const cleanup = run(); await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls, [[undefined, fresh]]); assert.deepEqual(actors, [native ? 'native-actor' : 'browser-actor']);
    cleanup();
  }
});

test('actual shell renders restoration, never Sign in, until native auth definitively rejects the session', () => {
  const tree = ts.createSourceFile('AppShell.tsx', shell, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const statements = component.body.statements;
  const gateStart = statements.findIndex(node => ts.isVariableStatement(node) && node.getText(tree).startsWith('const accountPending ='));
  const gateEnd = statements.findIndex((node, index) => index > gateStart && ts.isIfStatement(node) && node.expression.getText(tree) === '!authed');
  assert.ok(gateStart >= 0 && gateEnd > gateStart);
  const gate = statements.slice(gateStart, gateEnd + 1).map(node => node.getText(tree)).join('\n');
  const source = ts.transpileModule(`(function () { ${gate} })`, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React } }).outputText;
  const load = file => {
    const exports = {};
    vm.runInNewContext(ts.transpileModule(readFileSync(join(root, 'lib', file), 'utf8'), {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
    }).outputText, { exports });
    return exports;
  };
  const helpers = { ...load('nativeSessionGate.ts'), ...load('nativeAuthAvailability.ts') };
  for (const [state, checking] of [['restoring', true], ['restoring', false], ['checking', true], ['secure_store_pending', false], ['native_auth_busy', false], ['offline', false], ['keychain_unavailable', false], [undefined, false], ['signed_out', false], ['authorizing', false], ['reauth_required', false], ['account_mismatch', false], ['revocation_pending', false]]) {
    const render = vm.runInNewContext(source, { React: require('react'), ...helpers, nativeAccountState: state,
      local: false, nativeDesktop: true, browserEntry: false, authed: false, checking, connectionError: '', verificationEmail: '',
      DesktopAccountConnection: () => null, setSessionAttempt() {}, reconnectInPlace() {},
      UsageTracker: () => null, sessionSubject: undefined, pathname: '/', effectiveVentureId: undefined,
    });
    const html = renderToStaticMarkup(render());
    if (helpers.nativeSessionNeedsSignIn(state)) assert.match(html, /Sign in to Supymem/);
    else {
      assert.match(html, /Restoring your session…/);
      assert.doesNotMatch(html, /Sign in|Sign-in|Sign in with your browser/);
      assert.match(html, /role="status".*aria-live="polite"/);
      if (state === 'secure_store_pending') assert.match(html, /Approve the Keychain prompt to continue/);
    }
  }
});

test('shell reads restoration status after subscribing and explains denied Keychain access', async () => {
  const tree = ts.createSourceFile('AppShell.tsx', shell, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect'
      && node.arguments[0]?.getText(tree).includes('const applyStatus =')) effect = node.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(effect);
  for (const restoring of [true, false]) {
    const calls = [], states = [], errors = [];
    let subscribed = false;
    const run = vm.runInNewContext(ts.transpileModule(`(${effect})`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022 },
    }).outputText, {
      local: false, isDesktopShell: () => true, nativeAccountStateRef: { current: restoring ? undefined : 'authorizing' }, lastConnectedSubjectRef: { current: undefined },
      desktopListen: async event => { calls.push(event); subscribed = true; return () => {}; },
      desktopInvoke: async command => {
        assert.ok(subscribed); calls.push(command);
        return restoring ? { state: 'keychain_unavailable', restoring, error: 'Unlock your Keychain to reconnect.' }
          : { state: 'secure_store_pending', restoring };
      },
      nativeAuthActionPending: state => state === 'secure_store_pending',
      nativeSessionNeedsSignIn: state => state === 'authorizing',
      nativeAuthAvailabilityMessage: state => state === 'keychain_unavailable' ? 'Secure credential storage is unavailable.' : undefined,
      signInErrorMessage: signIn.signInErrorMessage,
      setNativeAccountState: state => states.push(state), setConnectionError: error => errors.push(error), setChecking() {},
    });
    const cleanup = run(); await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls, ['supymem-native-auth', 'desktop_auth_status']);
    assert.deepEqual(states, restoring ? ['keychain_unavailable'] : []);
    assert.deepEqual(errors, restoring ? ['Unlock your Keychain to reconnect.'] : []);
    cleanup();
  }
});

test('connected native status from the reload fixture exits restoration without an auth event', async () => {
  const tree = ts.createSourceFile('AppShell.tsx', shell, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect'
      && node.arguments[0]?.getText(tree).includes('const applyStatus =')) effect = node.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(effect);
  const states = [], actors = [], checking = [], authed = [];
  let subscribed = false;
  const run = vm.runInNewContext(ts.transpileModule(`(${effect})`, {
    compilerOptions: {target: ts.ScriptTarget.ES2022},
  }).outputText, {
    local: false, window: {location: {hostname: '127.0.0.1'}}, isDesktopShell: () => true,
    nativeAccountStateRef: {current: undefined}, lastConnectedSubjectRef: {current: undefined},
    desktopListen: async () => { subscribed = true; return () => {}; },
    desktopInvoke: async command => {
      assert.ok(subscribed); assert.equal(command, 'desktop_auth_status');
      return {state: 'connected', subject: 'usr_fixture', persisted: true};
    },
    nativeAuthActionPending: () => false, shouldResetPortfolio: () => false,
    setNativeAccountState: value => states.push(value), setSessionSubject: value => actors.push(value),
    setAuthed: value => authed.push(value), setChecking: value => checking.push(value),
    setConnectionError() {}, setSessionAttempt() {},
  });
  const cleanup = run(); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(states, ['connected']); assert.deepEqual(actors, ['usr_fixture']);
  assert.deepEqual(authed, [true]); assert.deepEqual(checking, [false]);
  cleanup();
});
