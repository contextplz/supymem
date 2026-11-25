const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('local settings routing preserves hosted state updates across domains', () => {
  const source = readFileSync(__dirname + '/AppShell.tsx', 'utf8');
  const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const shell = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const setter = shell.body.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'setSettingsOpen');
  assert.ok(setter);
  for (const domain of ['warehouse teams', 'weekend walkers']) for (const local of [false, true]) {
    const routes = [], updates = [];
    const context = {local, settingsOpen: false, router: {push: path => routes.push(path)}, updateSettingsOpen: value => updates.push(value)};
    vm.runInNewContext(ts.transpileModule(setter.getText(tree), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    const toggle = open => !open;
    context.setSettingsOpen(true);
    context.setSettingsOpen(toggle);
    context.setSettingsOpen(false);
    assert.deepEqual(routes, local ? ['/settings/local', '/settings/local'] : [], domain);
    assert.deepEqual(updates, local ? [false] : [true, toggle, false], domain);
  }
});

test('local startup never renders sign-in while hosted startup keeps it across domains', async () => {
  const React = require('react');
  const {renderToStaticMarkup} = require('react-dom/server');
  const source = readFileSync(__dirname + '/AppShell.tsx', 'utf8');
  const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const nativeEffect = component.body.statements.find(node => node.getText(tree).includes('const applyStatus ='));
  const sessionEffect = component.body.statements.find(node => node.getText(tree).includes('await ensureSession('));
  const retry = component.body.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'reconnectInPlace');
  const nativeCode = ts.transpileModule(nativeEffect.getText(tree), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  vm.runInNewContext(nativeCode, {local: true, nativeDesktop: true, useEffect: callback => callback(), isDesktopShell: () => assert.fail('Local mode cannot subscribe to hosted account state')});
  const authRedirect = component.body.statements.find(node => node.getText(tree).includes('if (local && (pathname === "/auth"'));
  const redirectCode = ts.transpileModule(authRedirect.getText(tree), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  const guards = component.body.statements.filter(node => ts.isIfStatement(node) && node.expression.getText(tree).includes('!authed'));
  const render = ts.transpileModule(`(function () { ${guards.map(node => node.getText(tree)).join('\n')} return children; })`, {
    compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
  }).outputText;
  for (const domain of ['procurement.example', 'gardening.example']) {
    for (const status of [0, 401, 403, 503]) {
      let attempt = 0;
      const state = {React, local: true, nativeDesktop: true, browserEntry: false, pathname: '/', sessionAttempt: 0,
        authed: false, checking: false, connectionError: '', chatShown: false,
        isDesktopShell: () => true, ensureSession: async () => ({ok: false, status, data: null}), isInvalidSession: () => false,
        setAuthed: value => { state.authed = value; }, setChecking: value => { state.checking = value; },
        setConnectionError: value => { state.connectionError = value; }, setSessionAttempt: update => { attempt = update(attempt); },
        useEffect: callback => callback(), children: domain,
      };
      vm.runInNewContext(ts.transpileModule(sessionEffect.getText(tree), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, state);
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(state.checking, false);
      vm.runInNewContext(ts.transpileModule(retry.getText(tree), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, state);
      const view = vm.runInNewContext(render, state)();
      assert.match(renderToStaticMarkup(view), /Retry/);
      view.props.children.find(element => element?.type === 'button').props.onClick();
      assert.equal(attempt, 1, `${domain}: your Retry checks the session again`);
    }
    for (const local of [true, false]) for (const pathname of ['/', '/auth', '/auth/device']) {
      const routes = [];
      vm.runInNewContext(redirectCode, {local, pathname, router: {replace: path => routes.push(path)}, useEffect: callback => callback()});
      assert.deepEqual(routes, local && pathname.startsWith('/auth') ? ['/'] : [], domain);
    }
    for (const local of [true, false]) for (const nativeDesktop of [true, false]) {
      const context = {React, local, nativeDesktop, authed: false, checking: false, connectionError: '',
        verificationEmail: '', nativeAccountState: 'signed_out', hostedWeb: true, loginMode: 'account',
        operatorLogin: false, submitting: false, error: '', sessionSubject: undefined, pathname: '/',
        effectiveVentureId: undefined, accountPending: false, reconnecting: false,
        children: React.createElement('main', null, domain),
        nativeSessionNeedsSignIn: () => true, CharlieMascot: () => React.createElement('img', {src: '/charlie/poster.png', alt: ''}),
        DesktopAccountConnection: () => React.createElement('button', null, 'Connect account'),
        PasswordResetForm: () => null, ErrorNotice: () => null, UsageTracker: () => null,
        reconnectInPlace() {}, doCognitoLogin() {}, setSessionAttempt() {},
      };
      const renderGate = vm.runInNewContext(render, context);
      const html = renderToStaticMarkup(renderGate());
      if (local) {
        assert.doesNotMatch(html, /Sign in|Passphrase|Create account|Connect account/i, domain);
        assert.doesNotMatch(html, /Charlie|\/charlie\/poster\.png|login-card/);
        assert.match(html, /Retry/);
        assert.match(html, /Check that your app is running/);
        context.checking = true;
        assert.doesNotMatch(renderToStaticMarkup(renderGate()), /sign.in|Passphrase|Connect account/i);
        context.authed = true;
        assert.match(renderToStaticMarkup(renderGate()), new RegExp(domain.replaceAll('.', '\\.')));
      } else {
        assert.match(html, /Sign in to Supymem/, domain);
        assert.match(html, nativeDesktop ? /Connect account/ : /Sign in with Google/);
      }
    }
  }
});

test('production login initializes its account or passphrase tab without initializing local sign-in across domains', () => {
  const tree = ts.createSourceFile('AppShell.tsx', readFileSync(__dirname + '/AppShell.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const effect = component.body.statements.find(node => node.getText(tree).includes('setHostedWeb(hosted)'));
  const code = ts.transpileModule(effect.getText(tree), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  for (const domain of ['procurement.example', 'gardening.example']) for (const local of [false, true]) for (const loopback of [false, true]) {
    const modes = [], passwords = [], hostname = loopback ? '127.0.0.1' : domain;
    vm.runInNewContext(code, {local, pass: '', URL, window: {__SUPYMEM__: {mode: local ? 'local' : undefined},
      location: {hostname}, addEventListener() {}, removeEventListener() {}}, resolveGateway: () => `http://${hostname}`,
      setHostedWeb() {}, setLoginMode: value => modes.push(value), setPass: value => passwords.push(value),
      isDesktopShell: () => false, setNativeDesktop() {}, useEffect: callback => callback()});
    assert.deepEqual(modes, local ? [] : [loopback ? 'passphrase' : 'account'], domain);
    assert.deepEqual(passwords, !local && loopback ? ['changeme'] : [], domain);
  }
});

test('your hosted invitation selects the production account tab while local startup skips it across domains', () => {
  const tree = ts.createSourceFile('AppShell.tsx', readFileSync(__dirname + '/AppShell.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const effect = component.body.statements.find(node => node.getText(tree).includes('const value = normalizeInviteCode'));
  const code = ts.transpileModule(effect.getText(tree), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  for (const domain of ['procurement.example', 'gardening.example']) for (const local of [false, true]) {
    const remembered = [], modes = [];
    vm.runInNewContext(code, {local, URLSearchParams, window: {location: {search: '?invite=INVITE42'}},
      normalizeInviteCode: value => value, recallInvite: () => '', setInviteCode() {},
      setLoginMode: value => modes.push(value),
      rememberInvite: value => remembered.push(value), useEffect: callback => callback()});
    assert.deepEqual(remembered, local ? [] : ['INVITE42'], domain);
    assert.deepEqual(modes, local ? [] : ['account'], domain);
  }
});

test('your local home settings entry opens your local settings instead of the hosted dialog across domains', () => {
  const tree = ts.createSourceFile('AppShell.tsx', readFileSync(__dirname + '/AppShell.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let callback;
  function visit(node) {
    if (ts.isJsxOpeningElement(node) && node.attributes.getText(tree).includes('sidebar-gear sidebar-settings')) {
      callback = node.attributes.properties.find(item => item.name?.getText(tree) === 'onClick').initializer.expression.getText(tree);
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
  for (const domain of ['Industrial procurement', 'Family cooking']) for (const local of [true, false]) {
    const routes = [], dialogs = [];
    vm.runInNewContext(`(${callback})()`, {local, router: {push: path => routes.push(path)}, setSettingsOpen: value => dialogs.push(value)});
    assert.deepEqual(routes, local ? ['/settings/local'] : [], domain);
    assert.deepEqual(dialogs, local ? [] : [true], domain);
  }
});

test('hosted boundary never probes local services and local gates remain intact across domains', async () => {
  const source = readFileSync(__dirname + '/LocalModeBoundary.tsx', 'utf8');
  for (const domain of ['Industrial procurement', 'Family cooking']) for (const runtimeMode of [undefined, 'hosted', 'local']) {
    const states = [], effects = [], requests = [], exports = {};
    const listeners = new Map();
    let cursor = 0;
    const jsx = (type, props) => ({type, props});
    const react = {createContext: value => ({Provider: 'mode', value}), useContext: context => context.value,
      useState(initial) {const index = cursor++; if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
        return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value;}];}, useEffect: callback => effects.push(callback)};
    vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    }}).outputText, {exports, window: {__SUPYMEM__: runtimeMode ? {mode: runtimeMode} : undefined, location: {pathname: '/'},
      addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: name => listeners.delete(name)}, require(name) {
      if (name === 'react') return react;
      if (name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (name === 'next/navigation') return {usePathname: () => '/', useRouter: () => ({replace() {}}), useSearchParams: () => new URLSearchParams()};
      if (name.endsWith('/client')) return {ensureSession: async () => ({ok: true}),
        api: async path => {requests.push(path); throw Error('Local service is unavailable');}};
      if (name.endsWith('/LocalSetup')) return {LocalProviderGate: 'setup'};
      if (name === '@/lib/localMode') return {...require('../../lib/localMode.ts'), isLocalConsole: () => runtimeMode === 'local'};
      if (name === '@/lib/appResume') return require('../../lib/appResume.ts');
      if (name === '@/lib/localFeatureVisibility') return require('../../lib/localFeatureVisibility.ts');
      throw Error(name);
    }});
    const children = {type: 'workspace', props: {domain, children: [{type: 'sign-in'}, {type: 'updater'}]}};
    const configuredMode = runtimeMode === 'local';
    const content = tree => tree.props.children.props.children;
    const setup = tree => content(tree).props.children[1].type;
    let tree = exports.LocalModeBoundary({children, local: false});
    assert.equal(tree.props.value, configuredMode, `${domain}: ${runtimeMode}`);
    if (configuredMode) assert.equal(setup(tree), 'setup');
    else assert.equal(content(tree), children);
    effects.splice(0).forEach(callback => callback());
    await new Promise(resolve => setImmediate(resolve));
    listeners.get('supymem-session-changed')?.();
    cursor = 0; tree = exports.LocalModeBoundary({children, local: false});
    assert.equal(tree.props.value, configuredMode);
    if (configuredMode) assert.equal(setup(tree), 'setup');
    else assert.equal(content(tree), children);
    assert.equal(requests.length, configuredMode ? 1 : 0);
    states.length = 0; effects.length = 0; cursor = 0;
    const explicitlyLocal = exports.LocalModeBoundary({children, local: true});
    assert.equal(explicitlyLocal.props.value, true);
    assert.equal(setup(explicitlyLocal), 'setup');
    const requestsBefore = requests.length;
    effects.splice(0).forEach(callback => callback());
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.length, requestsBefore + 1);
    for (const path of ['/admin', '/admin/users', '/operator', '/ops/finance', '/billing', '/billing/plans', '/stripe', '/plans', '/credits', '/referrals', '/invite/token', `/v/${domain}/billing`]) {
      const capabilities = [{state: 'not_in_this_version', surfaces: {paths: ['/admin', '/operator', '/ops', '/billing', '/stripe', '/plans', '/credits', '/referrals']}}];
      assert.equal(exports.isHostedOnlyPath(path) || !require('../../lib/localFeatureVisibility.ts').surfaceVisible(capabilities, 'paths', path, true), true, path);
    }
    for (const path of ['/', '/settings/local', '/setup', '/connections', '/administer', `/v/${domain}/plan`]) {
      assert.equal(exports.isHostedOnlyPath(path), false, path);
    }
    assert.doesNotMatch(source, /desktopInvoke|__TAURI|\u2014/);
    const root = readFileSync(__dirname + '/../../app/layout.tsx', 'utf8');
    assert.match(root, /<LocalModeBoundary/);
    assert.match(root, /SUPYMEM_MODE/);
  }
});

test('local screens ignore hosted device intents and never open computer permission cards across domains', () => {
  const tree = ts.createSourceFile('AppShell.tsx', readFileSync(__dirname + '/AppShell.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const frame = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellFrame');
  const inner = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const permissions = frame.body.statements.find(node => node.getText(tree).includes('const needed ='));
  const intent = inner.body.statements.find(node => node.getText(tree).includes('pathname !== "/auth/device"'));
  const connect = inner.body.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'resumeMacConnectIfNeeded');
  const code = ts.transpileModule(`${permissions.getText(tree)}\n${intent.getText(tree)}\n${connect.getText(tree)}`, {
    compilerOptions: {target: ts.ScriptTarget.ES2022},
  }).outputText;
  for (const domain of ['Fleet procurement', 'Home cooking']) for (const local of [true, false]) {
    const listeners = new Map(), cards = [];
    let intents = 0, approvals = 0;
    const context = {local, pathname: '/', MAC_TASK_PERMISSION_EVENT: 'permissions',
      useEffect: callback => callback(), useIsoLayoutEffect: callback => callback(),
      permissionRequestRef: {current: null}, accountSubjectRef: {current: null},
      setPermissionRequest: card => {if (card) cards.push(card);}, setPermissionRequestKey() {}, setAccountKey() {},
      window: {addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener() {}},
      isDesktopShell: () => true, readDeviceIntent: () => {intents++; return {device: domain};},
      resolveGateway: () => 'https://example.test', continueDeviceApproval: () => {approvals++; return true;}};
    vm.runInNewContext(code, context);
    listeners.get('permissions')({detail: {resume() {assert.fail('A notification never starts unrequested work');}}});
    assert.equal(context.resumeMacConnectIfNeeded(), !local, domain);
    assert.equal(cards.length, local ? 0 : 1, domain);
    assert.equal(intents, local ? 0 : 2, domain);
    assert.equal(approvals, local ? 0 : 1, domain);
  }
});

test('page chat keeps a readable minimum, stacks below 1100px, and gives drafts full card width across domains', () => {
  const source = readFileSync(__dirname + '/AppShell.tsx', 'utf8');
  const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let frameClass;
  function visit(node) {
    if (ts.isJsxAttribute(node) && node.name.getText(tree) === 'className' && node.initializer?.getText(tree).includes('"home-shell"')) {
      frameClass = node.initializer.expression.getText(tree);
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(frameClass);
  for (const [ventureId, audience] of [['ven_procurement', 'Hospital purchasing teams'], ['ven_cycling', 'People planning weekend rides']]) {
    const context = {local: false, effectiveVentureId: ventureId, entryPage: false, bare: false, pathname: `/v/${ventureId}/idea/current`,
      chatFirstHome: false, chatShown: true, officeTab: false, audience, cx: (...parts) => parts.filter(Boolean).join(' ')};
    assert.match(vm.runInNewContext(frameClass, context), /\bchat-page-layout\b/, audience);
    for (const state of [{chatFirstHome: true}, {officeTab: true}, {chatShown: false}]) {
      assert.doesNotMatch(vm.runInNewContext(frameClass, {...context, ...state}), /\bchat-page-layout\b/, audience);
    }
  }
  const tokens = readFileSync(__dirname + '/../../styles/tokens.css', 'utf8');
  const minimum = Number(tokens.match(/--w-chat:\s*(\d+)px;/)[1]);
  assert.ok(minimum >= 360 && minimum <= 420);
  assert.match(tokens, /\.app-shell\.home-shell\.chat-open \{ grid-template-columns: var\(--w-sidebar\) minmax\(0, 1fr\) minmax\(var\(--w-chat\),/);
  assert.match(tokens, /width: min\(max\(var\(--w-chat\), var\(--chat-panel-width\)\), 100%\);/);
  const stack = tokens.slice(tokens.indexOf('@media (max-width: 1099px)'));
  assert.match(stack, /\.app-shell\.home-shell\.chat-page-layout \{\s*grid-template-columns: minmax\(0, 1fr\);\s*grid-template-rows: var\(--h-titlebar\) minmax\(0, 1fr\) minmax\(0, 1fr\);/);
  assert.match(stack, /\.home-shell\.chat-page-layout \.workspace \{ display: grid; grid-column: 1; grid-row: 2; \}/);
  assert.match(stack, /\.home-shell\.chat-page-layout #supymem-chat-panel \{ grid-column: 1; grid-row: 3; \}/);
  const dock = readFileSync(__dirname + '/../chat/ChatDock.module.css', 'utf8');
  assert.match(dock, /\.homePanel \.imStack:has\(\.codeBlock\) \{ width: 100%; max-width: 100%; \}/);
  assert.match(dock, /\.homePanel \.codeBlock pre \{ padding: 40px 12px 12px; \}/);
  assert.match(dock, /\.codeBlock pre \{[^}]*white-space: pre-wrap; word-break: break-word;/);
});
