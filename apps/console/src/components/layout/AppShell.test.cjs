const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');

test('local navigation focuses its main landmark without overriding an anchor link', () => {
  const file = __dirname + '/AppShell.tsx';
  const tree = ts.createSourceFile(file, readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const shell = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const effect = shell.body.statements.find(node => ts.isExpressionStatement(node) &&
    node.expression.getText(tree).startsWith('useEffect(') && node.getText(tree).includes('localHomeRef.current'));
  assert.ok(effect);
  const code = ts.transpileModule(effect.getText(tree), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  for (const pathname of ['/v/warehouse-dispatch', '/v/weekend-recipes']) {
    let focused = 0;
    const context = {local: true, authed: true, pathname, officeTab: false,
      window: {location: {hash: ''}},
      localHomeRef: {current: {querySelector(selector) {
        assert.match(selector, /main/);
        return {focus: options => { assert.ok(options.preventScroll); focused++; }};
      }}}, useEffect: callback => callback()};
    vm.runInNewContext(code, context);
    assert.equal(focused, 1, pathname);
    context.window.location.hash = '#section-title';
    vm.runInNewContext(code, context);
    assert.equal(focused, 1, pathname);
  }
});

test('your AI and coding connections remain visible while company app connections load across domains', () => {
  const file = __dirname + '/AppShell.tsx';
  const tree = ts.createSourceFile(file, readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const shell = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const home = shell.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(tree) === 'founderUI === "home"');
  for (const domain of ['Stockroom scheduling', 'Family recipe sharing']) {
    const context = {React, local: true, founderUI: 'home', effectiveVentureId: undefined, entryPage: false, bare: false,
      chatFirstHome: false, chatShown: false, localBuildActivity: false, localSubpage: false, officeTab: false, pathname: '/connections', chatPanelSize: {width: 400},
      sessionSubject: domain, authed: true, clarificationSkip: null, searchParams: new URLSearchParams(),
      chatAvailable: false, nativeDesktop: false, isVentureOverview: false, headerRight: null, shownTitle: 'Connections',
      workspaceBodyRef: {current: null}, localHomeRef: {current: null}, ideaStatus: null, connectionError: '', permissionRequest: null,
      companyOperations: true, bottomBar: null, children: domain, captureChatLink() {}, showsDetailToggle: () => false,
      setOperationsScope() {}, cx: (...parts) => parts.filter(Boolean).join(' '),
      FounderUIContext: React.createContext('classic'), ChatOpenContext: React.createContext(false), ClarificationSkipContext: React.createContext(null),
      CompanyOffice: ({children}) => React.createElement('main', null, children),
      AppShellErrorBoundary: ({children}) => children, WorkspaceErrorBoundary: ({children}) => children,
      LocalConnections: () => React.createElement('section', {'data-owner-connections': domain}, 'Your AI provider and coding tools'),
      OpsWorkspaceGate: () => React.createElement('p', {'data-app-connections': domain}, 'Loading your company workspaces'),
      UsageTracker: () => null, NextStepStrip: () => null, AttentionToast: () => null, DesktopNoticeBanner: () => null, AgentDesk: () => null};
    vm.runInNewContext(ts.transpileModule(`function renderShell() { ${home.getText(tree)} }`, {
      compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
    }).outputText, context);
    const markup = renderToStaticMarkup(context.renderShell());
    assert.ok(markup.includes(`data-owner-connections="${domain}"`));
    assert.ok(markup.includes(`data-app-connections="${domain}"`));
    assert.ok(markup.indexOf('data-owner-connections') < markup.indexOf('data-app-connections'));
  }
});

test('local venture subpages fill one workspace without a second chat or unrelated next step', () => {
  const file = __dirname + '/AppShell.tsx';
  const tree = ts.createSourceFile(file, readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const shell = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const home = shell.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(tree) === 'founderUI === "home"');
  const layout = shell.body.statements.filter(node => ts.isVariableStatement(node) &&
    ['localBuildActivity', 'localSubpage', 'chatAvailable', 'chatShown'].includes(node.declarationList.declarations[0].name.getText(tree)));
  const openInChat = shell.body.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'openInChat');
  const navigation = shell.body.statements.find(node => node.getText(tree).includes('window.addEventListener(HOME_NAVIGATION_EVENT'));
  const code = ts.transpileModule(`function renderShell() {
    ${layout.map(node => node.getText(tree)).join('\n')} ${openInChat.getText(tree)} ${navigation.getText(tree)} ${home.getText(tree)}
  }`, {
    compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
  }).outputText;
  for (const ventureId of ['supplier-inventory', 'family-cooking']) for (const route of ['device-work-orders/order-1', 'idea/current', 'leads', 'linkedin', 'guide']) {
    const build = route.startsWith('device-work-orders/');
    const routes = [], asks = [], listeners = new Map();
    const context = {React, local: true, founderUI: 'home', effectiveVentureId: ventureId, entryPage: false, bare: false, fill: false,
      chatFirstHome: false, officeTab: false, pathname: `/v/${ventureId}/${route}`, chatPanelSize: {width: 400},
      sessionSubject: ventureId, authed: true, clarificationSkip: null, searchParams: new URLSearchParams({chat: '1'}),
      nativeDesktop: false, isVentureOverview: false, guidePage: route === 'guide', headerRight: null, shownTitle: build ? 'Your build activity' : 'Sidebar page', subtitle: null,
      workspaceBodyRef: {current: null}, localHomeRef: {current: null}, ideaStatus: null, connectionError: '', permissionRequest: null, chatPanelOpen: true,
      companyOperations: false, bottomBar: null, children: React.createElement('p', null, ventureId), captureChatLink() {},
      showsDetailToggle: () => false, pendingAsk: null, setChatPanelSize() {}, cx: (...parts) => parts.filter(Boolean).join(' '),
      router: {push: href => routes.push(href)}, setPendingAsk: ({action}) => asks.push(action), setChatPanelOpen() {},
      HOME_NAVIGATION_EVENT: 'navigate', PREFILL_ACTION: require('../../lib/chatPrefill.ts').PREFILL_ACTION,
      useEffect: callback => callback(), window: {addEventListener: (name, listener) => listeners.set(name, listener)},
      FounderUIContext: React.createContext('classic'), ChatOpenContext: React.createContext(false), ClarificationSkipContext: React.createContext(null),
      CompanyOffice: ({children, standalone, nextStep}) => React.createElement('div', {role: 'main', tabIndex: -1},
        React.createElement('aside', null, 'Sidebar'), standalone && React.createElement('a', {href: `/v/${ventureId}`, 'data-chat-route-native': 'true'}, 'Back to chat'), nextStep, children),
      VentureChatLauncher: ({open}) => React.createElement('section', {id: 'supymem-chat-panel', hidden: !open}),
      Link: props => React.createElement('a', props), AppShellErrorBoundary: ({children}) => children, WorkspaceErrorBoundary: ({children}) => children,
      UsageTracker: () => null, NextStepStrip: () => 'Unrelated next step', AttentionToast: () => null, DesktopNoticeBanner: () => null, AgentDesk: () => null};
    vm.runInNewContext(code, context);
    const markup = renderToStaticMarkup(context.renderShell());
    assert.doesNotMatch(markup, /chat-open|chat-page-layout|charlie-buddy|Unrelated next step/, route);
    assert.match(markup, /id="supymem-chat-panel" hidden=""/, route);
    assert.match(markup, new RegExp(`href="/v/${ventureId}" data-chat-route-native="true">Back to chat`));
    assert.equal(markup.includes('<aside>Sidebar</aside>'), !build, route);
    assert.match(markup, build ? /class="workspace build-activity-workspace"/ : /class="workspace local-subpage-workspace"/);
    if (build) assert.deepEqual(markup.match(/<h1>.*?<\/h1>/g), ['<h1>Your build activity</h1>'], ventureId);
    assert.match(markup, /role="main" tabindex="-1"/, ventureId);
    listeners.get('navigate')({detail: {action: 'status'}});
    assert.deepEqual(asks, ['status'], ventureId);
    assert.deepEqual(routes, [`/v/${ventureId}`], ventureId);
  }
  const css = readFileSync(__dirname + '/../../styles/tokens.css', 'utf8');
  assert.match(css, /:has\(> \.build-activity-workspace\)[^{]*\{[^}]*grid-template-rows: var\(--h-titlebar\) minmax\(0, 1fr\)/);
  assert.match(css, /\.build-activity-workspace\s*\{[^}]*grid-column: 1; grid-row: 2; grid-template-rows: auto minmax\(0, 1fr\)/);
  assert.match(css, /:has\(> \.local-subpage-workspace\)[^{]*\{[^}]*grid-template-rows: var\(--h-titlebar\) minmax\(0, 1fr\)/);
});
