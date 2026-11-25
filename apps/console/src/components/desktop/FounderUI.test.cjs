const chatRoutes = require("../../lib/chatRouteAction.ts");
const assert = require('node:assert/strict');
const { existsSync, readFileSync, readdirSync, statSync } = require('node:fs');
const { dirname, join, relative, resolve } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const root = join(__dirname, '../..');
const read = path => readFileSync(join(root, path), 'utf8');
const tree = ts.createSourceFile('AppShell.tsx', read('components/layout/AppShell.tsx'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const shell = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
const classic = shell.body.statements.filter(ts.isReturnStatement).at(-1).expression;
const homeBranch = shell.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(tree) === 'founderUI === "home"');
const home = homeBranch.thenStatement.expression;

test('your local coding request starts supported work without changing hosted consent copy across domains', () => {
  for (const domain of ['Warehouse routing', 'Weekend cooking']) {
    for (const local of [true, false]) {
      const exports = {};
      const jsx = (type, props) => ({type, props});
      vm.runInNewContext(ts.transpileModule(read('components/desktop/ConnectTools.tsx'), {compilerOptions: {
        module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
      }}).outputText, {exports, window: {}, require(id) {
        if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
        if (id === 'react') return {useState: initial => [initial === false ? true : initial, () => {}], useEffect() {}};
        if (id.endsWith('/desktop')) return {isDesktopShell: () => true};
        if (id.endsWith('/localMode')) return {isLocalConsole: () => local};
        return {};
      }});
      const tree = JSON.stringify(exports.ConnectTools());
      if (local) {
        assert.match(tree, /To build, connect Claude Code, Codex, Cursor or OpenCode in Settings > Keys and connections > Coding tools/, domain);
        assert.match(tree, /Your request starts work after you allow the tool and your subscription login is verified/, domain);
        assert.match(tree, /Supymem Local/);
        assert.doesNotMatch(tree, /approve the coding run/);
      } else {
        assert.match(tree, /You review the plan, approve the coding run and approve each deployment separately/);
        assert.doesNotMatch(tree, /Supymem Local/);
      }
      assert.match(tree, /Opening an app does not approve a coding task/);
    }
  }
});

function renderFrame(expression, context = {}) {
  const output = ts.transpileModule(`const render = () => (${expression});`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  const passthrough = ({ children }) => React.createElement(React.Fragment, null, children);
  const values = {
    entryPage: context.pathname === '/new', captureChatLink() {}, chatPanelStyles: {panel: 'panel', homePanel: 'homePanel'}, chatDockStyles: {replyAnnouncement: 'visually-hidden'}, settingsSection: 'venture',
    React, cx: (...parts) => parts.filter(Boolean).join(' '),
    bare: false, officeHome: false, nativeDesktop: false, authed: true, sessionSubject: 'synthetic', pathname: '/v/ven_fixture/tasks',
    effectiveVentureId: 'ven_fixture', ventureId: 'ven_fixture', sidebarPinned: false, sidebarExpanded: false,
    chatPanelOpen: false, chatAvailable: true, chatShown: false, localBuildActivity: false, localSubpage: false, chatFirstHome: false, officeTab: false, guidePage: false, chatPanelSize: { width: 340 }, settingsOpen: false,
    isVentureOverview: false, ideaStatus: undefined, clarificationSkip: undefined, fill: false,
    shownTitle: 'Work', subtitle: undefined, shownStatus: null, stage: undefined, hired: 0,
    headerRight: null, bottomBar: null, connectionError: '', permissionRequest: null,
    children: React.createElement('article', null, 'production page content'),
    companyOperations: false, activeScope: null, pendingAsk: null, managerName: 'Charlie',
    chatToggleRef: { current: null }, workspaceBodyRef: { current: null }, localHomeRef: { current: null },
    setSavedTitle() {}, setVentureName() {}, setSettingsOpen() {}, setChatPanelOpen() {}, setChatPanelSize() {},
    setPendingAsk() {}, doLogout() {}, togglePin() {}, closeSidebar() {}, showsDetailToggle: () => false,
    ASK_EVENT: 'ask', NAV_ICONS: { settings: () => null, sidebar: () => null, ventures: () => React.createElement('svg', { 'aria-hidden': true }) },
    ChatOpenContext: React.createContext(false), ClarificationSkipContext: React.createContext(null),
    FounderUIContext: React.createContext('classic'),
    UsageTracker: () => null, AppShellErrorBoundary: passthrough, WorkspaceErrorBoundary: passthrough,
    OfficeTeamRail: () => null, RunStatusBar: () => null, NextStepStrip: () => null,
    AttentionToast: () => null, DesktopNoticeBanner: () => null, FounderCoaching: () => null,
    AgentDesk: () => null, ReportProblem: () => null, DetailToggle: () => null,
    Link: ({ href, children, ...props }) => React.createElement('a', { href, ...props }, children),
    WorkspaceSwitcher: () => React.createElement('span', null, 'venture switcher'),
    SideNav: () => React.createElement('nav', null, 'classic navigation'),
    GuideNavLink: () => React.createElement('a', {href: '/guide'}, 'How to use Supymem'),
    VentureChatLauncher: ({ open = true }) => React.createElement('div', { hidden: !open, inert: !open }, 'chat'),
    CharlieFace: () => React.createElement('img', {src: '/charlie/charlie-mark.png', alt: ''}),
    CharlieMascot: () => React.createElement('img', {src: '/charlie/poster.png', alt: ''}), DesktopAccountConnection: () => null, CharlieDock: () => React.createElement('button', null, 'Charlie'),
    CompanyOffice: props => React.createElement('div', { 'data-home-rail': true }, React.createElement('a', {href: `/v/${props.ventureId}`}, 'Back to company'), React.createElement('h1', null, props.title), props.children),
    CompanySettingsDialog: () => null, local: false,
    CompanyHome: passthrough, HOME_TABS: {home: 'Home'}, searchParams: new URLSearchParams(),
    ...context,
  };
  values.chatShown = context.chatShown ?? ((values.chatPanelOpen || values.chatFirstHome) && values.chatAvailable);
  return renderToStaticMarkup(vm.runInNewContext(`${output}\nrender();`, values));
}

test('classic frame retains your workspace and local recovery without hosted account chrome across domains', () => {
  assert.equal(tree.parseDiagnostics.length, 0);
  for (const domain of ['Dispatch software', 'Cooking club']) for (const local of [true, false]) {
    const html = renderFrame(classic.getText(tree), {
      local, nativeDesktop: true, shownTitle: domain, settingsOpen: true, permissionRequest: {},
      permissionRequestKey: 1, onMacTaskPermissionsGranted() {},
      accountPending: false, reconnecting: false, checking: false, reconnectInPlace() {},
      connectionError: 'Connection interrupted', connectionMessage: 'Check that your app is running, then retry.',
      shownStatus: { code: 'live', label: 'Watching' }, ventureStatusLabel: () => 'Watching',
      WorkspaceSwitcher: ({children}) => React.createElement('span', null, children),
      DesktopTaskPermissionCard: () => React.createElement('p', null, 'Mac permission recovery'),
      DesktopAccountConnection: () => React.createElement('p', null, 'Mac account connection'),
      ReportProblem: () => React.createElement('p', null, 'Report problem'),
      RunStatusBar: () => React.createElement('p', null, 'Run status'),
      NextStepStrip: () => React.createElement('p', null, 'Next step'),
      headerRight: React.createElement('button', null, 'Page action'),
      bottomBar: React.createElement('footer', null, 'Page footer'),
    });
    for (const phrase of ['classic navigation', 'production page content', domain, 'Watching', 'Run status', 'Next step', 'Page action', 'Page footer', 'Ask Charlie']) {
      assert.ok(html.includes(phrase), phrase);
    }
    assert.match(html, /aria-controls="supymem-chat-panel"/);
    assert.match(html, local ? /href="\/settings\/local"[^>]*aria-label="Venture settings"/ : /href="\/v\/ven_fixture\/settings"[^>]*aria-label="Venture settings"/);
    assert.match(html, /Check that your app is running, then retry/);
    assert.match(html, local ? />Retry<\/button>/ : />Reconnect<\/button>/);
    assert.doesNotMatch(html, /home-shell|data-home-rail|Back to company/);
    if (local) {
      assert.doesNotMatch(html, /sidebar-foot|Account settings|Log out|Report problem|Mac permission recovery|Mac account connection/);
    } else {
      for (const phrase of ['sidebar-foot', 'Account settings', 'Log out', 'Report problem', 'Mac permission recovery', 'Mac account connection']) {
        assert.ok(html.includes(phrase), phrase);
      }
    }
  }
  const tokens = read('styles/tokens.css');
  assert.match(tokens, /html:has\(\.home-shell\), \.home-shell/);
  assert.match(tokens, /\.home-shell button:active \{ transform: none/);
});

test('new-page uses the company rail and a single chat panel without the old 96px entry layout', () => {
  const html = renderFrame(home.getText(tree), { pathname: '/new', effectiveVentureId: undefined, ventureId: undefined });
  assert.match(html, /data-home-rail/);
  assert.equal((html.match(/id="supymem-chat-panel"/g) || []).length, 1);
  assert.match(html, /<main id="supymem-chat-panel"[^>]*aria-labelledby="entry-chat-title"[^>]*><h1 id="entry-chat-title"/);
  assert.doesNotMatch(html, /sidebar-company/);
  const tokens = read('styles/tokens.css');
  assert.match(tokens, /\.app-shell\.home-shell \{ --w-sidebar: 96px/);
  assert.match(tokens, /\.home-shell \.sidebar-company \{[^}]*flex-direction: column;[^}]*padding: 10px 0;[^}]*font-size: 11px/);
  const companyRule = tokens.match(/\.home-shell \.sidebar-company \{([^}]*)\}/)[1];
  assert.doesNotMatch(companyRule, /text-transform|letter-spacing/);
  const switcher = read('components/office/WorkspaceSwitcher.module.css');
  assert.match(switcher, /:global\(\.home-shell \.sidebar\) \.switcher \{[^}]*flex-direction: column;[^}]*padding: 8px 0/);
  assert.match(switcher, /:global\(\.home-shell \.sidebar\) \.switcherName \{ font-size: 10px/);
  assert.match(switcher, /:global\(\.home-shell \.sidebar\) \.switcherChevron \{ display: none/);
});

test('home frame keeps production content, title, back link and company rail on founder routes', () => {
  for (const pathname of ['/v/ven_fixture/tasks', '/v/ven_fixture/context', '/gtm', '/new', '/account', '/guide']) {
    const html = renderFrame(home.getText(tree), { pathname });
    assert.match(html, /home-shell/);
    assert.match(html, /data-home-rail/);
    assert.match(html, /production page content/);
    assert.match(html, /<h1>Work<\/h1>/);
    assert.match(html, /Back to company/);
    assert.doesNotMatch(html, /classic navigation/);
  }
});

test('venture history stays published on every page while the closed chat is hidden and inert', () => {
  const published = [], loaded = [];
  const historyContext = {window: {dispatchEvent: event => published.push(event), addEventListener() {}, removeEventListener() {}},
    CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options?.detail;}}};
  const historyHook = compile('components/chat/useChatHistory.ts', id => id === 'react'
    ? {useEffect: effect => effect(), useRef: current => ({current})}
    : {isLocalConsole: () => false}, historyContext);
  const launcher = compile('components/chat/VentureChatLauncher.tsx', id => {
    if (id === './useChatHistory') return historyHook;
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    if (id === "@/lib/chatRouteAction") return chatRoutes;
    if (id === "@/components/settings/AccountSettings") return { AccountSettings: "account-settings" };

    if (id === 'react') return { useContext: () => 'home', useState: initial => [initial, () => {}], useEffect: effect => effect() };
    if (id === 'react/jsx-runtime') return require(id);
    if (id === './useVentureChat') return { useVentureChat: ventureId => ({ managerName: 'Guide', conversationsAvailable: true,
      conversationId: 'saved', conversationItems: [{ id: 'saved', title: 'Saved discussion' }], archivedConversationItems: [], messages: [],
      reloadConversations: () => loaded.push(ventureId) }) };
    if (id === './ChatDock') return { ChatDock: () => null };
    if (id === './CharlieDock') return { CharlieMascot: () => null };
    if (id === '@/lib/chatPrefill') return { PREFILL_ACTION: 'prefill' };
    if (id === '@/components/primitives/ErrorNotice') return { ErrorNotice: () => null };
    return { default: {} };
  }, { window: { dispatchEvent: event => published.push(event), addEventListener() {}, removeEventListener() {} },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } } });
  for (const ventureId of ['ven_alpha', 'ven_beta']) {
    for (const suffix of ['', '/idea/current', '/tasks', '/connect/provider']) {
      const shown = !suffix;
      const html = renderFrame(home.getText(tree), { effectiveVentureId: ventureId, pathname: `/v/${ventureId}${suffix}`,
        chatShown: shown, chatFirstHome: shown, chatAvailable: suffix !== '/connect/provider', VentureChatLauncher: launcher.VentureChatLauncher });
      assert.match(html, /id="supymem-chat-panel"/);
      assert.equal(/hidden=""/.test(html), !shown);
      assert.equal(/inert=""/.test(html), !shown);
      assert.equal(loaded.at(-1), ventureId);
      const history = published.filter(event => event.type === 'supymem:chat-history').at(-1).detail;
      assert.equal(history.ventureId, ventureId);
      assert.equal(history.items[0].id, 'saved');
    }
  }
  assert.match(read('components/chat/VentureChatLauncher.module.css'), /\.panel\[hidden\]\s*\{\s*display: none;/);
});

test('connection notices belong only to the view that started OAuth, including late completions', async () => {
  const jsx = (type, props, key) => ({ type, props: props || {}, key });
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(10).flatMap(nodes)];
  const text = node => typeof node === 'string' ? node : node && typeof node === 'object' ? [node.props?.children].flat(10).map(text).join('') : '';
  const state = { owner: { epoch: 1, subject: 'usr_views' }, current: () => true,
    data: { ventureId: 'ven_views', workspaceId: 'wsp_views', agents: [], archives: [], pins: [], errors: [],
      cards: [{ id: 'gmail', name: 'Mail account', action: 'connect', connected: false }] } };
  let slots = [], cursor = 0, viewKey;
  let authorization = Promise.resolve({ authorize_url: 'https://example.com/authorize' });
  const react = { createContext: value => ({ value, Provider: 'provider' }), useContext: context => context.value,
    useId: () => 'saved-document-title',
    useState: initial => {
      const instance = slots, index = cursor++;
      instance[index] ??= { value: initial };
      return [instance[index].value, value => { instance[index].value = typeof value === 'function' ? value(instance[index].value) : value; }];
    }, useRef: initial => { const index = cursor++; return slots[index] ??= { current: initial }; }, useEffect() {} };
  const modules = { react, 'react/jsx-runtime': { jsx, jsxs: jsx }, 'next/link': { default: 'a' }, '@/lib/chatRouteAction': chatRoutes,
    '@/components/chat/ChatLink': { default: 'a', ChatAnchor: 'a', captureChatLink() {} },
    'next/navigation': { useRouter: () => ({ push() {} }) }, '@/lib/companyHome': { useCompanyHomeData: () => state, companyDepartments: () => [] },
    '@/lib/founderRoutes': { founderRouteLinks: () => [] }, '@/lib/desktop': { isDesktopShell: () => false },
    '@/lib/opsApi': { connectHubProvider: () => authorization }, './OfficeProvider': { useOfficeOptional: () => null },
    '@/components/connections/connectionCopy': { connectionActionLabel: () => 'Connect', connectionState: () => 'Not connected' } };
  const homeModule = compile('components/office/CompanyHome.tsx', id => modules[id] || { default: {} }, { URL, window: { open() {} } });
  homeModule.CompanyHomeContext.value = state;
  let tab = 'toolkit';
  const office = compile('components/office/CompanyOffice.tsx', id => {
    if (id === 'react') return { useState: initial => [initial, () => {}], useRef: initial => ({ current: initial }), useEffect() {} };
    if (id === 'next/navigation') return { useRouter: () => ({ push() {} }), useSearchParams: () => new URLSearchParams({ office: tab }) };
    if (id === './CompanyHome') return homeModule;
    return modules[id] || { default: {} };
  });
  const render = nextTab => {
    tab = nextTab;
    const frame = office.CompanyOffice({ ventureId: state.data.ventureId, pathname: `/v/${state.data.ventureId}`, onSettings() {}, children: 'Page content' });
    const view = nodes(frame).find(node => node.type === homeModule.CompanyHome);
    assert.ok(view);
    if (view.key !== viewKey) { slots = []; viewKey = view.key; }
    cursor = 0;
    return homeModule.CompanyHome(view.props);
  };
  const connect = () => nodes(render('toolkit')).find(node => node.props['aria-label'] === 'Mail account: Connect').props.onClick();
  const flush = () => new Promise(resolve => setImmediate(resolve));
  connect(); await flush();
  const banner = nodes(render('toolkit')).find(node => node.props.role === 'status' && text(node).includes('Approve'));
  assert.ok(banner);
  for (const nextTab of ['departments', 'archives', 'home', 'toolkit']) {
    assert.equal(text(render(nextTab)).includes(text(banner)), false);
  }
  let finish;
  authorization = new Promise(resolve => { finish = resolve; });
  connect(); render('departments'); render('toolkit');
  finish({ authorize_url: 'https://example.com/authorize' }); await flush();
  assert.equal(text(render('toolkit')).includes(text(banner)), false);
  assert.equal(nodes(render('toolkit')).some(node => node.type === 'button' && text(node) === 'Open authorization again'), false);
});

test('sidebar stays a plain list with Idea/LinkedIn slots, history and Settings', () => {
  const jsx = (type, props) => ({type, props: props || {}});
  const actions = [];
  const office = compile('components/office/CompanyOffice.tsx', id => {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    if (id === "@/lib/chatRouteAction") return chatRoutes;
    if (id === "@/components/settings/AccountSettings") return { AccountSettings: "account-settings" };

    if (id === 'react') return {useState: initial => [initial, () => {}], useEffect() {}, useRef: initial => ({current: initial})};
    if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    if (id === 'next/link') return {default: 'a'};
    if (id === 'next/navigation') return {useRouter: () => ({push() {}}), useSearchParams: () => new URLSearchParams()};
    if (id === '@/lib/companyHome') return {companyDepartments: () => [], useCompanyHomeData: () => ({data: {name: 'Real company', email: 'real@example.com', agents: [], pins: [], errors: []}, owner: {epoch: 1, subject: 'usr_fixture'}})};
    if (id === '../ventures/ventureCards') return compile('components/ventures/ventureCards.ts', () => ({}));
    if (id === './CompanyHome') return {HOME_TABS: {home: 'Home', archives: 'Tuesday Archives', toolkit: 'Toolkit', departments: 'Departments'}, CompanyHomeContext: {Provider: 'provider'}};
    if (id === '@/lib/founderRoutes') return {founderRouteLinks: () => [{href: '/new', label: 'New venture'}]};
    if (id === '@/components/layout/LocalModeBoundary') return {useLocalMode: () => false, useFeatureVisibility: () => () => true};
    if (id === '@/lib/chatVisibility') return {chatFirstHidden: () => false};
    return {default: new Proxy({}, {get: (_, key) => key})};
  });
  const tree = office.CompanyOffice({ventureId: 'ven_fixture', pathname: '/v/ven_fixture/tasks', title: 'Work', onSettings: () => actions.push('settings'), children: 'production page'});
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(5).flatMap(nodes)];
  const all = nodes(tree);
  const text = node => !node || typeof node !== 'object' ? String(node ?? '') : [node.props?.children].flat(5).map(text).join('');
  assert.ok(all.some(node => node.type === 'aside' && node.props['aria-label'] === 'Your sidebar'));
  const rows = all.filter(node => node.type === 'button' && node.props.className === 'cos-row');
  assert.deepEqual(rows.map(text), ['+New chat', 'Tuesday Archives', 'Idea', 'Leads', 'Connections', 'LinkedIn', 'All ventures', 'Settings']);
  assert.deepEqual(rows.filter(node => node.props['data-sidebar-slot']).map(node => node.props['data-sidebar-slot']), ['idea', 'leads', 'connections', 'linkedin']);
  assert.equal(all.filter(node => node.type === 'hr').length, 2);
  assert.ok(all.some(node => node.props.className === 'cos-empty' && text(node) === 'Your chats with Charlie show up here.'));
  assert.equal(rows.filter(node => node.props['aria-current']).length, 0);
  rows.at(-1).props.onClick(); assert.deepEqual(actions, ['settings']);
  assert.doesNotMatch(read('components/office/CompanyOffice.tsx'), /WorkspaceSwitcher|CharlieDock|pageList/);
  const controls = jsx('button', {children: 'Detailed'});
  const subpage = office.CompanyOffice({ventureId: 'ven_fixture', pathname: '/v/ven_fixture/research', title: 'Research', onSettings() {}, controls, children: 'research report'});
  const subpageNodes = nodes(subpage);
  const pageHead = subpageNodes.find(node => node.props.className === 'pageHead');
  const canvas = subpageNodes.find(node => node.props.className === 'canvas workspace-body');
  assert.ok(nodes(pageHead).includes(controls));
  assert.ok(!nodes(canvas).includes(controls));
  assert.equal(canvas.props.children.at(-1), 'research report');
});

test('home layout confines the outside-venture rail and keeps header controls separate from the scroll canvas', () => {
  const css = read('styles/tokens.css');
  assert.match(css, /\.app-shell\.home-shell \.sidebar \{[^}]*position: static; inset: auto; z-index: auto; justify-self: stretch;[^}]*grid-column: 1; grid-row: 2;[^}]*width: 100%; max-width: 100%/);
  assert.match(css, /\.home-shell \.sidebar::before \{ display: none/);
  assert.match(css, /\.app-shell\.home-shell\.venture-frame \.sidebar \{ display: none/);
  assert.match(css, /@media \(max-width: 1023px\) \{ \.app-shell\.home-shell[^\n]*\.app-shell\.home-shell \.sidebar \{ grid-column: 1 \/ -1; grid-row: 2;[^\n]*\.home-shell \.workspace \{ grid-column: 1; grid-row: 3/);
  const office = read('components/office/CompanyOffice.module.css');
  assert.match(office, /\.pageHead \{[^}]*grid-column: 1;[^}]*grid-row: 1/);
  const canvas = office.match(/\.canvas \{([^}]*)\}/)[1];
  assert.match(canvas, /grid-row: 2/);
  assert.match(canvas, /min-height: 0/);
  assert.match(canvas, /overflow: auto/);
  assert.doesNotMatch(canvas, /(?:^|\s)height: 100%/);
  assert.match(office, /\.pageHead:has\(\.pageControls\) \{ display: flex; flex-wrap: wrap/);
  const frame = renderFrame(home.getText(tree), { showsDetailToggle: () => true,
    DetailToggle: () => React.createElement('button', null, 'Detailed'),
    CompanyOffice: props => React.createElement('section', null, React.createElement('header', null, props.controls), React.createElement('main', null, props.children)),
  });
  assert.match(frame, /<header><button>Detailed<\/button><\/header>/);
  assert.doesNotMatch(frame, /<main><button>Detailed/);
});

test('inside venture uses PR buddy and outside uses lane C corner dock', () => {
  const css = read('components/chat/CharlieDock.module.css');
  assert.match(css, /\.homeDock \{[^}]*position: fixed;[^}]*top: 0;[^}]*right: 0;[^}]*width: 178px;[^}]*height: 156px;[^}]*border-radius: 0 0 0 36px/);
  assert.match(css, /\.dock \{[^}]*position: relative;[^}]*background: #05070b/);
  assert.match(css, /\.localDock \{[^}]*background: var\(--bg-card\)/);
  assert.match(read('styles/tokens.css'), /\.charlie-buddy \{[^}]*width: 150px;[^}]*height: 132px;[^}]*border: 1.5px dotted #b7b1c6/);
  assert.match(read('components/office/CompanyOffice.module.css'), /grid-template-columns: minmax\(0, 1fr\) 138px/);
  assert.match(read('components/office/CompanyOffice.module.css'), /@media \(max-width: 980px\) \{ \.rail \{ padding-right: 150px/);
  assert.match(read('styles/tokens.css'), /\.home-shell:not\(\.venture-frame\) \.sidebar \{ padding-right: 178px/);
  const closed = renderFrame(home.getText(tree));
  assert.match(closed, /class="charlie-buddy"/);
  for (const domain of ['procurement teams', 'home gardeners']) for (const local of [false, true]) {
    const buddy = renderFrame(home.getText(tree), {local, managerName: domain});
    assert.match(buddy, local ? /\/charlie\/poster\.png/ : /\/charlie\/charlie-mark\.png/);
    assert.doesNotMatch(buddy, local ? /\/charlie\/charlie-mark\.png/ : /\/charlie\/poster\.png/);
  }
  assert.match(closed, /hidden="" inert="">chat</);
  assert.doesNotMatch(closed, />Charlie</);
  const open = renderFrame(home.getText(tree), {chatPanelOpen: true});
  assert.doesNotMatch(open, /class="charlie-buddy"/); assert.match(open, />chat</);
  const outside = renderFrame(home.getText(tree), {effectiveVentureId: undefined, pathname: '/new'});
  assert.match(outside, /id="supymem-chat-panel"/); assert.match(outside, /data-home-rail/);
  assert.doesNotMatch(outside, /workspace-fixed-header/);
});

test('PR chat panel spans its grid and the pill composer shrinks without clipping Send', () => {
  const panel = read('components/chat/VentureChatLauncher.module.css');
  assert.match(panel, /\.homePanel \{[^}]*grid-column: 3;[^}]*grid-row: 1 \/ -1;[^}]*border-radius: 0/);
  assert.match(panel, /@media \(max-width: 1023px\) \{ \.homePanel \{ grid-column: 2; grid-row: 3;/);
  assert.match(panel, /@media \(max-width: 600px\) \{ \.homePanel \{ grid-column: 1; grid-row: 3;/);
  const css = read('components/chat/ChatDock.module.css');
  assert.match(css, /\.homePanel \.composer \{\s*display: flex;\s*min-width: 0/);
  assert.match(css, /\.homePanel \.composerBox \{[^}]*min-width: 0;[^}]*display: flex;[^}]*flex-wrap: wrap;[^}]*flex: 1 1 0;[^}]*border: 1px solid #e5e5ea;[^}]*border-radius: calc\(var\(--im-field, 36px\) \/ 2\)/);
  assert.match(css, /\.homePanel \.composerInput \{ flex: 1 1 80px; min-width: 0;/);
  assert.match(css, /\.homePanel \.composerActions \{[^}]*min-width: 0;[^}]*max-width: 100%;[^}]*flex: 0 1 auto;[^}]*flex-wrap: wrap/);
  assert.match(css, /\.homePanel \.composerTools \{[^}]*min-width: 0;[^}]*max-width: 100%;[^}]*flex-wrap: wrap/);
  assert.match(css, /\.homePanel \.composerActions button\[data-usage-control="chat_send"\],[^}]*width: var\(--im-inline, 28px\);[^}]*height: var\(--im-inline, 28px\);[^}]*border-radius: 50%;[^}]*background: #007aff/);
  for (const selector of ['composer', 'composerBox', 'composerInput', 'composerActions']) {
    const rule = css.match(new RegExp(`\\.homePanel \\.${selector} \\{([^}]*)\\}`))?.[1];
    assert.ok(rule);
    assert.doesNotMatch(rule, /overflow(?:-x)?:\s*(hidden|clip)/);
  }
});

test('company Home keeps conversation open without a dashboard; the chat stays mounted under the company tabs', () => {
  const declarations = shell.body.statements.filter(ts.isVariableStatement).flatMap(statement => statement.declarationList.declarations);
  const expression = name => declarations.find(declaration => declaration.name.getText(tree) === name).initializer.getText(tree);
  for (const tab of ['', 'home', 'unknown', 'archives', 'toolkit', 'departments']) {
    const context = {founderUI: 'home', pathname: '/v/ven_fixture', entryPage: false, isVentureOverview: true, guidePage: false, localBuildActivity: false, localSubpage: false, searchParams: new URLSearchParams({office: tab})};
    context.officeTab = vm.runInNewContext(expression('officeTab'), context);
    assert.equal(vm.runInNewContext(expression('chatFirstHome'), context), !['archives', 'toolkit', 'departments'].includes(tab));
    assert.equal(vm.runInNewContext(expression('chatShown'), {...context, chatAvailable: true, chatPanelOpen: false, chatFirstHome: !context.officeTab}), true);
  }
  for (const pathname of ['/guide', '/guide/', '/v/ven_fixture/guide', '/v/ven_fixture/guide/']) {
    const guidePage = vm.runInNewContext(expression('guidePage'), {pathname});
    assert.equal(guidePage, true);
    const context = {pathname, guidePage, founderUI: 'home', entryPage: false, chatAvailable: true, chatFirstHome: false, officeTab: false, localBuildActivity: false, localSubpage: false};
    assert.equal(vm.runInNewContext(expression('chatShown'), {...context, chatPanelOpen: false}), false, 'the dedicated guide stays visible on a narrow screen');
    assert.equal(vm.runInNewContext(expression('chatShown'), {...context, chatPanelOpen: true}), true, 'the founder can still open chat deliberately');
  }
  const frame = renderFrame(home.getText(tree), {chatFirstHome: true, chatShown: true});
  assert.match(frame, /chat-first-home chat-open/);
  assert.match(frame, />chat</);
  assert.doesNotMatch(frame, /class="charlie-buddy"/);
  assert.match(read('components/layout/AppShell.tsx'), /closable=\{false\}/);
  const css = read('styles/tokens.css');
  assert.match(css, /\.app-shell\.home-shell\.chat-first-home \{[^}]*grid-template-columns: var\(--office-rail\) minmax\(0, 1fr\)/);
  assert.match(css, /chat-first-home #supymem-chat-panel \{ grid-column: 2; grid-row: 1 \/ -1/);
  const office = read('components/office/CompanyOffice.tsx');
  assert.doesNotMatch(office, /railCollapsed|Collapse sidebar/);
  assert.match(office, /!chatFirst && <div/);
  assert.match(css, /html\[data-office-tab\]:not\(\[data-office-tab="home"\]\) #supymem-chat-panel \{\s*display: none/);
  assert.match(read('components/chat/useChatHistory.ts'), /supymem:chat-history[\s\S]*chat\.selectConversation\(id\)[\s\S]*chat\.newConversation\(\)/);
  const homePage = read('app/v/[ventureId]/page.tsx').match(/if \(local \|\| founderUI === "home"\) return <AppShell officeHome[\s\S]*?<\/AppShell>/)?.[0];
  assert.ok(homePage);
  assert.doesNotMatch(homePage, /Company next step|nextStep.title|\{cta\}/);
});

function compile(path, requireModule, context = {}) {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(read(path), { fileName: path, compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, { exports, require: id => id === '@/components/layout/LocalModeBoundary' ? {useLocalMode: () => false, useFeatureVisibility: () => () => true}
    : id.endsWith('localMode.ts') ? require(join(root, 'lib/localMode.ts')) : requireModule(id), ...context });
  return exports;
}

test('sidebar resize restores safely, clamps captured drags and keyboard steps, persists and resets', () => {
  function mount(saved, blocked = false) {
    let cursor = 0;
    const slots = [], effects = [], values = new Map([['supymem.sidebarWidth', saved]]), shellValues = new Map();
    const react = {
      useState(initial) { const index = cursor++; slots[index] ??= {value: initial}; return [slots[index].value, value => { slots[index].value = typeof value === 'function' ? value(slots[index].value) : value; }]; },
      useRef(initial) { const index = cursor++; return slots[index] ??= {current: initial}; },
      useEffect(effect, deps) { const index = cursor++; if (!slots[index] || deps.some((value, position) => value !== slots[index].deps[position])) { slots[index]?.cleanup?.(); slots[index] = {deps}; effects.push(() => { slots[index].cleanup = effect(); }); } },
    };
    const jsx = (type, props) => ({type, props});
    const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
    const owner = {}, window = {addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, get localStorage() {
      if (blocked) throw new Error('Storage denied');
      return {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
    }};
    const office = compile('components/office/CompanyOffice.tsx', id => {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    if (id === "@/lib/chatRouteAction") return chatRoutes;
    if (id === "@/components/settings/AccountSettings") return { AccountSettings: "account-settings" };

      if (id === 'react') return react;
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'next/navigation') return {useRouter: () => ({push() {}}), useSearchParams: () => new URLSearchParams()};
      if (id === '@/lib/founderRoutes') return {founderRouteLinks: () => []};
      if (id === '@/lib/companyHome') return {useCompanyHomeData: () => ({owner, data: {name: 'Company', pins: [], agents: []}})};
      if (id === './CompanyHome') return {HOME_TABS: {home: 'Home'}, CompanyHomeContext: {Provider: 'provider'}};
      return {default: new Proxy({}, {get: (_, key) => key})};
    }, {window, URLSearchParams, CustomEvent: class {}, document: {documentElement: {dataset: {}}}});
    function render() {
      cursor = 0;
      const all = nodes(office.CompanyOffice({ventureId: 'ven_resize', pathname: '/v/ven_resize', chatAvailable: true, onSettings() {}, children: null}));
      all.find(node => node.type === 'aside').props.ref.current = {closest: () => ({style: {setProperty: (key, value) => shellValues.set(key, value), removeProperty: key => shellValues.delete(key)}})};
      while (effects.length) effects.shift()();
      const handle = all.find(node => node.props.role === 'separator');
      assert.equal(all.find(node => node.props.className?.includes('office-chat-first')).props.style['--sidebar-width'], `${handle.props['aria-valuenow']}px`);
      return handle.props;
    }
    render();
    return {render, values, shellValues, deny: () => {blocked = true;}};
  }
  for (const saved of [null, '', 'invalid', 'Infinity']) assert.equal(mount(saved).render()['aria-valuenow'], 420);
  assert.equal(mount('999').render()['aria-valuenow'], 600);
  assert.equal(mount('100').render()['aria-valuenow'], 220);
  assert.equal(mount('280', true).render()['aria-valuenow'], 420);
  const ui = mount('280');
  let handle = ui.render(), captured = null;
  assert.equal(handle['aria-orientation'], 'vertical'); assert.equal(handle.tabIndex, 0);
  assert.equal(handle['aria-valuemin'], 220); assert.equal(handle['aria-valuemax'], 600);
  assert.equal(handle['aria-valuenow'], 280); assert.equal(ui.shellValues.get('--office-rail'), '280px');
  for (const [key, width] of [['ArrowLeft', 264], ['ArrowRight', 280]]) {
    let prevented = false;
    handle.onKeyDown({key, preventDefault() {prevented = true;}}); handle = ui.render();
    assert.equal(prevented, true); assert.equal(handle['aria-valuenow'], width);
  }
  const currentTarget = {focus() {}, setPointerCapture: id => {captured = id;}, hasPointerCapture: id => captured === id, releasePointerCapture: () => {captured = null;}};
  const pointer = {button: 0, pointerId: 1, clientX: 280, currentTarget, preventDefault() {}};
  handle.onPointerDown(pointer); assert.equal(captured, 1);
  handle.onPointerMove({...pointer, pointerId: 2, clientX: 900}); assert.equal(ui.render()['aria-valuenow'], 280);
  for (const [clientX, width] of [[100, 220], [500, 500], [900, 600]]) {
    handle.onPointerMove({...pointer, clientX}); handle = ui.render();
    assert.equal(handle['aria-valuenow'], width); assert.equal(ui.values.get('supymem.sidebarWidth'), String(width));
    assert.equal(ui.shellValues.get('--office-rail'), `${width}px`);
  }
  handle.onPointerUp(pointer); assert.equal(captured, null);
  handle.onPointerMove({...pointer, clientX: 100}); assert.equal(ui.render()['aria-valuenow'], 600);
  handle.onKeyDown({key: 'ArrowLeft', preventDefault() {}}); handle = ui.render();
  assert.equal(handle['aria-valuenow'], 584);
  handle.onDoubleClick(); assert.equal(ui.render()['aria-valuenow'], 420); assert.equal(ui.values.get('supymem.sidebarWidth'), '420');
  ui.deny(); ui.render().onKeyDown({key: 'ArrowLeft', preventDefault() {}}); assert.equal(ui.render()['aria-valuenow'], 404);
});

test('fresh authenticated /new renders the home workspace and real entry composer outside a venture', () => {
  const modules = new Map();
  const location = new URL('http://127.0.0.1:3100/new');
  const router = { push() {}, replace(href) { assert.fail(`unexpected navigation: ${href}`); } };
  const mocks = {
    'next/link': { default: ({ href, children, ...props }) => React.createElement('a', { href, ...props }, children) },
    'next/navigation': { useSearchParams: () => location.searchParams, usePathname: () => location.pathname, useRouter: () => router },
    '@/lib/client': { verifiedSessionSubject: () => 'usr_fresh', verifiedSessionEpoch: () => 1,
      ensureSession: async () => ({ ok: true, data: { subject: 'usr_fresh' } }), api: async () => ({ ok: true, status: 200, data: { items: [] } }) },
    '@/components/chat/ChatDock': { usePersistedDockSize: () => [{ width: 400 }, () => {}] },
    '@/components/chat/IntakeAttachments': { useIntakeAttachments: () => ({ files: [], evidenceIds: [], uploading: false }), IntakeAttachments: () => null },
    '@/components/chat/VoiceMic': { VoiceMic: () => null }, '@/components/chat/DictateButton': { DictateButton: () => null },
    '@/lib/usageCollectorState': { listenForUsageSubject: () => () => {} },
    '@/lib/founderUI': { FounderUIContext: React.createContext('home'), useFounderUI: () => 'home', useFounderUIState: () => ({ layout: 'home', loaded: true }) },
  };
  function load(path) {
    if (modules.has(path)) return modules.get(path);
    const cached = {};
    modules.set(path, cached);
    const exports = compile(path, id => {
      if (id in mocks) return mocks[id];
      if (id.endsWith('.css')) return { default: new Proxy({}, { get: (_, key) => key }) };
      if (id.startsWith('@/') || id.startsWith('.')) {
        const base = id.startsWith('@/') ? join(root, id.slice(2)) : resolve(dirname(join(root, path)), id);
        const found = [base, `${base}.ts`, `${base}.tsx`].find(file => { try { return statSync(file).isFile(); } catch { return false; } });
        if (found) {
          const path = relative(root, found);
          return mocks[`@/${path.replace(/\.tsx?$/, '')}`] || load(path);
        }
      }
      return require(id);
    }, { window: { location, localStorage: { getItem: () => null }, matchMedia: () => ({ matches: false }) }, URL, URLSearchParams,
      EventTarget, Event, CustomEvent, AbortController, TextEncoder, TextDecoder, setTimeout, clearTimeout, process: { env: {} } });
    Object.assign(cached, exports);
    return cached;
  }
  const Page = load('app/new/page.tsx').default;
  const html = renderToStaticMarkup(React.createElement(Page));
  assert.match(html, /class="app-shell native-shell founder-shell home-shell venture-frame chat-first-home chat-open"/);
  assert.doesNotMatch(html, /app-shell-bare|Loading Supymem workspace|login-gate/);
  assert.equal((html.match(/id="supymem-chat-panel"/g) || []).length, 1);
  assert.equal((html.match(/<textarea id="entry-message"/g) || []).length, 1);
  assert.match(html, /<label for="entry-message"[^>]*>What is your idea\?<\/label>/);
  assert.match(html, /data-usage-control="onboarding_continue"/);
  assert.match(html, /All ventures/);
});

test('local settings routes are reachable without exposing them in hosted navigation across domains', () => {
  const routes = compile('lib/founderRoutes.ts', () => ({ isBetaHiddenPath: () => false }));
  for (const venture of ['ven_dispatch', 'ven_cooking']) {
    for (const [href, label] of [['/settings/local', 'Settings'], ['/settings/local/capabilities', 'Features']]) {
      assert.ok(routes.FOUNDER_GLOBAL_ROUTES.includes(href));
      assert.equal(routes.founderRouteLinks(venture).some(route => route.href === href), false);
      assert.equal(routes.founderRouteLinks(venture, true).some(route => route.href === href && route.label === label), true);
    }
  }
});

test('every founder page is reachable via the rail, Charlie, or the desktop menu', () => {
  const routes = compile('lib/founderRoutes.ts', () => ({ isBetaHiddenPath: () => false }));
  const intent = readFileSync(join(root, '../../../services/company_os/app/chat/intent.py'), 'utf8');
  const desktop = readFileSync(join(root, '../../../src-tauri/src/lib.rs'), 'utf8');
  const report = desktop.slice(desktop.indexOf('"report" => {'), desktop.indexOf('"cursor-diagnostic" => {'));
  const support = report.match(/window\.location\.assign\('(\/settings\/local\/[^']+)'\)/)?.[1];
  assert.equal(support, '/settings/local/support');
  const venture = [...intent.slice(intent.indexOf('VENTURE_SCREEN_ROUTES ='), intent.indexOf('# Company-level pages')).matchAll(/"[^"]+": "([^"]*)"/g)].map(match => match[1]);
  const global = [...intent.slice(intent.indexOf('COMPANY_SCREEN_PATHS ='), intent.indexOf('_OPEN_SCREEN_RE =')).matchAll(/"[^"]+": "([^"]*)"/g)].map(match => match[1]);
  const reachable = new Set([
    ...routes.FOUNDER_GLOBAL_ROUTES, ...global, support,
    ...routes.FOUNDER_VENTURE_ROUTES.map(path => '/v/[ventureId]' + (path ? '/' + path : '')),
    ...venture.map(path => '/v/[ventureId]' + (path ? '/' + path : '')),
  ]);
  const internal = /^(admin|ops|auth|invite|setup|operator|welcome|suggestions\/overlay|gtm\/lab|pagebuilder-lab|lab\/diagrams)(\/|$)/;
  const dynamicParents = {
    '/v/[ventureId]/connect/[provider]': '/v/[ventureId]/connections',
    '/v/[ventureId]/device-work-orders/[orderId]': '/v/[ventureId]/builds',
  };
  const pages = [];
  function walk(path = '') {
    for (const entry of readdirSync(join(root, 'app', path), { withFileTypes: true })) {
      const next = path ? `${path}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(next);
      else if (entry.name === 'page.tsx') pages.push(path);
    }
  }
  walk();
  for (const path of pages.filter(path => !internal.test(path))) {
    const route = path ? `/${path}` : '/';
    assert.ok(reachable.has(route) || reachable.has(dynamicParents[route]), `unreachable founder page: ${route}`);
  }
  assert.match(read('components/site/BuildPageCard.tsx'), /device-work-orders/);
  assert.match(read('app/v/[ventureId]/connections/page.tsx'), /connect/);
  const realRoutes = compile('lib/founderRoutes.ts', () => compile('lib/betaRoutes.ts', () => {}));
  assert.ok(realRoutes.founderRouteLinks('ven fixture').some(route => route.href === '/gtm?venture=ven%20fixture'));
  assert.ok(realRoutes.founderRouteLinks('ven fixture').every(route => !route.href.startsWith('/ops') && !route.href.includes('/simulate')));
});

test('local settings navigation is gated and local pages retain closed feature labels across domains', () => {
  const tree = ts.createSourceFile('founderRoutes.ts', read('lib/founderRoutes.ts'), ts.ScriptTarget.Latest, true);
  const titles = tree.statements.filter(ts.isVariableStatement)
    .flatMap(statement => statement.declarationList.declarations)
    .find(declaration => declaration.name.getText(tree) === 'TITLES').initializer.properties
    .map(property => property.name.text);
  assert.equal(new Set(titles).size, titles.length, 'route titles must not have duplicate keys');
  const features = compile('lib/usageFeature.ts', () => ({}));
  for (const ventureId of ['ven_dispatch_software', 'ven_garden_club']) {
    const routes = compile('lib/founderRoutes.ts', () => ({ isBetaHiddenPath: () => false }));
    const hostedLinks = routes.founderRouteLinks(ventureId), localLinks = routes.founderRouteLinks(ventureId, true);
    assert.ok(hostedLinks.every(link => !link.href.startsWith('/settings/local')));
    assert.equal(localLinks.find(link => link.href === '/settings/local')?.label, 'Settings');
    assert.deepEqual(Array.from(localLinks, link => link.href).filter(href => !href.startsWith('/settings/local')),
      Array.from(hostedLinks, link => link.href).filter(href => !require(join(root, 'lib/localMode.ts')).isHostedOnlyPath(href)));
    assert.equal(features.usageFeature('/settings/local'), 'settings');
    assert.equal(features.usageFeature('/settings/local/capabilities'), 'settings');
    assert.equal(features.usageFeature('/setup'), 'onboarding');
    assert.equal(features.usageFeature(`/v/${ventureId}/settings`), 'settings');
  }
});

test('shared settings retain production safeguards and pages export only their page', () => {
  const settings = read('components/settings/VentureSettings.tsx');
  for (const component of ['GodModeRequestCard', 'BookingLinkCard', 'TaskPreferences', 'SuggestionsSettings', 'BrowserProfilePreparation']) assert.match(settings, new RegExp(`<${component}\\b`));
  assert.match(settings, /unconfirmed usage remains unresolved/);
  assert.match(settings, /platform key never/);
  const page = ts.createSourceFile('page.tsx', read('app/v/[ventureId]/settings/page.tsx'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const exports = page.statements.filter(node => node.modifiers?.some(modifier => modifier.kind === ts.SyntaxKind.ExportKeyword));
  assert.equal(exports.length, 1);
  for (const path of ['components/settings/VentureSettings.tsx', 'components/office/CompanySettingsDialog.tsx']) {
    assert.doesNotMatch(read(path), /LayoutToggle|Use classic layout|Use new layout|settings-layout/);
  }
  assert.equal(existsSync(join(root, 'components/settings/LayoutToggle.tsx')), false);
  assert.ok(exports[0].modifiers.some(modifier => modifier.kind === ts.SyntaxKind.DefaultKeyword));
  assert.doesNotMatch(read('components/office/CompanySettingsDialog.tsx'), /@\/app\//);
});

test('home ships no fake controls and preserves real ingestion, voice and stop actions', () => {
  const office = read('components/office/CompanyOffice.tsx');
  assert.doesNotMatch(office, /TODO_PHASE_2_FAKE_CONTROLS|Founded Day 0|Todoist|changeme/);
  assert.doesNotMatch(office.replace(/localStorage\.(?:getItem|setItem)\(SIDEBAR_WIDTH_KEY/g, '')
    .replace('resumeAppRoute(window.localStorage, subject, "/", true, "home")', ''), /localStorage/);
  assert.match(read('lib/companyHome.ts'), /ui-pins/);
  assert.match(office, /HOME_TABS/);
  const chat = read('components/chat/ChatDock.tsx');
  assert.match(chat, /<IntakeAttachments/);
  assert.match(chat, /<VoiceMic/);
  assert.match(chat, /<EmergencyStopButton/);
  assert.doesNotMatch(chat, /!isPanel && ventureId && onVoiceSend|aria-label="Add"/);
});

test('home is default, only admin policy restores the untouched classic frame, and refresh retries safely', async () => {
  let subject = 'usr_a', epoch = 1, cursor = 0;
  const states = [], requests = [], effects = [], listeners = new Map();
  let poll;
  const react = {
    createContext: value => ({ value }),
    useState(initial) {
      const index = cursor++;
      if (!(index in states)) states[index] = initial;
      return [states[index], value => { states[index] = value; }];
    },
    useEffect: callback => effects.push(callback),
  };
  const events = {
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name) { listeners.delete(name); },
  };
  const client = {
    api: (path, ...args) => {
      assert.equal(path, '/api/ui-config');
      assert.deepEqual(args, []);
      return new Promise((resolve, reject) => requests.push({ resolve, reject }));
    },
    verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
  };
  const ui = compile('lib/founderUI.tsx', id => id === 'react' ? react : client, {
    window: { ...events, setInterval: callback => { poll = callback; return 1; }, clearInterval() {} },
    document: { hidden: false, ...events },
  });
  const render = selected => { cursor = 0; return ui.useFounderUI(selected); };
  const frame = layout => renderFrame(`(() => { ${homeBranch.getText(tree)} return (${classic.getText(tree)}); })()`, { founderUI: layout });
  const settle = async () => { await Promise.resolve(); await Promise.resolve(); };
  assert.equal(render(subject), 'home');
  assert.match(frame(render(subject)), /data-home-rail/);
  assert.equal(ui.FounderUIContext.value, 'classic');
  const cleanup = effects[0]();
  assert.equal(listeners.has('supymem-founder-ui-changed'), false);
  requests.shift().resolve({ ok: true, data: { founder_ui: 'classic' } });
  await settle();
  assert.equal(render(subject), 'classic');
  assert.equal(frame(render(subject)), renderFrame(classic.getText(tree)));
  poll();
  requests.shift().reject(new Error('offline'));
  await settle();
  assert.equal(render(subject), 'classic');
  poll();
  requests.shift().resolve({ ok: true, data: { founder_ui: 'invalid' } });
  await settle();
  assert.equal(render(subject), 'classic');
  poll();
  subject = 'usr_b'; epoch++;
  subject = 'usr_a'; epoch++;
  requests.shift().resolve({ ok: true, data: { founder_ui: 'classic' } });
  await settle();
  assert.equal(render(subject), 'home');
  poll();
  requests.shift().resolve({ ok: false, data: null });
  await settle();
  poll();
  requests.shift().resolve({ ok: true, data: { founder_ui: 'home' } });
  await settle();
  assert.equal(render(subject), 'home');
  poll();
  requests.shift().resolve({ ok: true, data: { founder_ui: 'classic' } });
  await settle();
  assert.equal(render(subject), 'classic');
  cleanup();
  assert.equal(listeners.size, 0);
  assert.doesNotMatch(read('lib/founderUI.tsx'), /FOUNDER_UI_CHANGED|CustomEvent|method:\s*["']POST/);
});

test('desktop changes are limited to window chrome and authorized dragging', () => {
  const config = JSON.parse(readFileSync(join(root, '../../../src-tauri/tauri.conf.json'), 'utf8'));
  const main = config.app.windows.find(window => window.label === 'main');
  assert.equal(main.titleBarStyle, 'Overlay');
  assert.equal(main.hiddenTitle, true);
  assert.deepEqual(main.trafficLightPosition, { x: 18, y: 26 });
  const capability = JSON.parse(readFileSync(join(root, '../../../src-tauri/capabilities/default.json'), 'utf8'));
  assert.ok(capability.permissions.includes('core:window:allow-start-dragging'));
  assert.match(home.getText(tree), /desktopInvokeQuiet\("plugin:window\|start_dragging"\)/);
});

test('chat-first redirects wait for the real founder_ui setting so classic deep links survive the first render', () => {
  const shell = require('node:fs').readFileSync(require('node:path').join(__dirname, '../layout/AppShell.tsx'), 'utf8');
  assert.match(shell, /const hiddenPage = founderUILoaded && founderUI === "home"/);
});
