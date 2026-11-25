const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

test('polish3: Settings returns to your own venture chat or the picker without sending an action', () => {
  const source = readFileSync(join(__dirname, 'CompanyOffice.tsx'), 'utf8');
  const tree = ts.createSourceFile('CompanyOffice.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect, home, back;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect' && node.getText(tree).includes('resumeAppRoute')) effect = node.arguments[0].getText(tree);
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'homeHref') home = node.initializer.getText(tree);
    if (ts.isJsxOpeningElement(node) && node.tagName.getText(tree) === 'Link' && node.attributes.getText(tree).includes('styles.back')) back = node.attributes;
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(effect, 'Settings must recover the account-scoped saved venture');
  const native = back.properties.find(node => node.name?.text === 'data-chat-route-native');
  assert.equal(native.initializer.expression.getText(tree), 'true');
  const resume = require('../../lib/appResume.ts');
  for (const [ventureId, domain] of [['ven_freight', 'Freight dispatch'], ['ven_gardens', 'Community gardens']]) {
    for (const saved of [`/v/${ventureId}/builds`, null, '//outside.example', '/new']) {
      const subject = `${domain}-owner`, values = new Map(saved ? [[`supymem.lastRoute.v1:${encodeURIComponent(subject)}`, saved]] : []);
      const context = {local: true, pathname: '/settings/local', ventureId: '', state: {owner: {subject}}, returnVenture: null,
        window: {localStorage: {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)}}, ...resume,
        setReturnVenture: value => {context.returnVenture = value;}};
      vm.runInNewContext(ts.transpileModule(`(${effect})()`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
      const href = vm.runInNewContext(home, context);
      assert.equal(href, saved?.startsWith('/v/') ? `/v/${ventureId}` : '/', domain);
      context.state.owner.subject = 'another-owner';
      assert.equal(vm.runInNewContext(home, context), '/', 'A different owner cannot inherit your venture');
      Object.defineProperty(context.window, 'localStorage', {get() {throw new Error('Storage denied');}});
      vm.runInNewContext(ts.transpileModule(`(${effect})()`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
      assert.equal(vm.runInNewContext(home, context), '/', 'Denied storage still offers the venture picker');
    }
  }
});

test('your office navigation hides unshipped routes locally and retains coding-tool builds across domains', () => {
  const source = ts.createSourceFile('CompanyOffice.tsx', readFileSync(join(__dirname, 'CompanyOffice.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let commands;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'commands') commands = node.initializer.getText(source);
    ts.forEachChild(node, find);
  }
  find(source);
  const {surfaceVisible} = require('../../lib/localFeatureVisibility.ts');
  for (const ventureId of ['ven_factory', 'ven_family_recipes']) for (const local of [true, false]) {
    const routes = [{href: '/ads', label: 'Ads'}, {href: `/v/${ventureId}/builds`, label: 'Builds'}];
    const exported = {};
    vm.runInNewContext(ts.transpileModule(`exports.commands = ${commands}`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, {
      exports: exported, ventureId, local, query: '', chatAvailable: true, homeHref: `/v/${ventureId}`,
      router: {}, nativeRouter: {}, onSettings() {}, prefillChat() {}, chatFirstHidden: () => false,
      founderRouteLinks: () => routes,
      featureVisible: (surface, value) => surfaceVisible([{state: 'not_in_this_version', surfaces: {paths: ['/ads']}}], surface, value, local),
    });
    assert.equal(exported.commands.some(command => command.id === '/ads'), !local);
    assert.ok(exported.commands.some(command => command.id === `/v/${ventureId}/builds`));
  }
});

test('your narrow local workspace keeps chat beside a collapsible sidebar and wraps the sample banner across domains', () => {
  const output = ts.transpileModule(readFileSync(join(__dirname, 'CompanyOffice.tsx'), 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
  }).outputText;
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)];
  for (const [ventureId, audience] of [['ven_inventory', 'Wholesale inventory teams'], ['ven_recipes', 'Families sharing recipes']]) {
    for (const local of [true, false]) {
      const states = [];
      let cursor = 0, focused = 0;
      const mocks = {
        react: {
          useState(initial) {
            const index = cursor++;
            if (!(index in states)) states[index] = initial;
            return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }];
          },
          useEffect() {}, useRef: current => ({current}),
        },
        'react/jsx-runtime': require('react/jsx-runtime'),
        'next/navigation': {useRouter: () => ({push() {}}), useSearchParams: () => new URLSearchParams()},
        '@/components/chat/ChatLink': {default: 'a'},
        '@/lib/chatRouteAction': {chatNavigation: router => router},
        '@/lib/founderRoutes': {founderRouteLinks: () => []},
        '@/components/layout/LocalModeBoundary': {useLocalMode: () => local, useFeatureVisibility: () => (surface, value) => !local || value !== 'referrals.show'},
        '@/lib/chatVisibility': {chatFirstHidden: () => false},
        '@/lib/companyHome': {useCompanyHomeData: () => ({data: {name: audience}, owner: {epoch: 1, subject: ventureId}})},
        './CompanyHome': {HOME_TABS: {home: 'Home'}, CompanyHomeContext: React.createContext(null), CompanyHome: () => null},
        './CompanyOffice.module.css': {default: new Proxy({}, {get: (_, name) => String(name)})},
      };
      const module = {exports: {}, require: name => mocks[name] || {}};
      vm.runInNewContext(output, module);
      const render = (standalone = false) => {
        cursor = 0;
        const pathname = `/v/${ventureId}${standalone ? '/idea/current' : ''}`;
        return nodes(module.exports.CompanyOffice({ventureId, pathname, standalone, chatAvailable: true, onSettings() {}, children: audience}));
      };
      let rendered = render();
      const sidebar = rendered.find(node => node.type === 'aside');
      const menu = rendered.find(node => node.props.className === 'local-office-menu');
      assert.equal(rendered.some(node => node.props.className?.split(' ').includes('local-office')), local, audience);
      assert.equal(Boolean(menu), local, audience);
      assert.equal(sidebar.props.id, local ? 'local-office-sidebar' : undefined);
      assert.equal(sidebar.props['aria-label'], 'Your sidebar');
      assert.deepEqual(rendered.filter(node => node.type === 'nav').map(node => node.props['aria-label']), ['Main', 'Chat history']);
      if (!local) continue;
      assert.equal(menu.props.type, 'button');
      assert.equal(menu.props['aria-controls'], sidebar.props.id);
      assert.equal(menu.props['aria-expanded'], false);
      menu.props.onClick();
      rendered = render();
      assert.ok(rendered.some(node => node.props.className?.split(' ').includes('local-sidebar-open')));
      const openMenu = rendered.find(node => node.props.className === 'local-office-menu');
      assert.equal(openMenu.props['aria-expanded'], true);
      openMenu.props.ref.current = {focus: () => focused++};
      rendered.find(node => node.type === 'aside').props.onKeyDown({key: 'Escape', preventDefault() {}});
      assert.equal(focused, 1);
      assert.equal(render().find(node => node.props.className === 'local-office-menu').props['aria-expanded'], false);
      render().find(node => node.props.className === 'local-office-menu').props.onClick();
      render().find(node => node.props.className === 'local-office-menu').props.onKeyDown({key: 'Escape', preventDefault() {}});
      assert.equal(render().find(node => node.props.className === 'local-office-menu').props['aria-expanded'], false);
      render().find(node => node.props.className === 'local-office-menu').props.onClick();
      render().find(node => node.type === 'nav').props.onClick();
      assert.equal(render().find(node => node.props.className === 'local-office-menu').props['aria-expanded'], false);
      states[1] = true;
      rendered = render();
      assert.ok(rendered.some(node => node.props.className?.split(' ').includes('local-sidebar-open')));
      assert.ok(rendered.some(node => node.props.id === 'command-palette'));
      rendered.find(node => node.props.className === 'local-office-menu').props.onClick();
      assert.equal(render().find(node => node.props.className === 'local-office-menu').props['aria-expanded'], false);
      const page = render(true);
      assert.ok(page.some(node => node.props.role === 'main' && node.props.tabIndex === -1));
      assert.equal(page.find(node => node.props.children === 'Back to chat').props.href, `/v/${ventureId}`);
      const pageMenu = page.find(node => node.props.className === 'local-office-menu');
      pageMenu.props.onClick();
      assert.equal(render(true).find(node => node.props.className === 'local-office-menu').props['aria-expanded'], true);
    }
  }
  const css = readFileSync(join(__dirname, '../../styles/tokens.css'), 'utf8');
  assert.match(css, /\.local-sample-workspace\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /\.local-sample-workspace > p\s*\{[^}]*white-space: normal;[^}]*overflow-wrap: anywhere/);
  const narrow = css.slice(css.lastIndexOf('@media (max-width: 980px)'));
  assert.match(narrow, /\.app-shell\.home-shell\.chat-first-home:has\(\.local-office\)\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(narrow, /\.app-shell\.home-shell\.chat-first-home:has\(\.local-office\) \.workspace\s*\{[^}]*grid-column: 1;[^}]*grid-row: 1 \/ -1;[^}]*display: grid;[^}]*pointer-events: none/);
  assert.match(narrow, /\.app-shell\.home-shell\.chat-first-home:has\(\.local-office\) #supymem-chat-panel\s*\{[^}]*grid-column: 1;[^}]*grid-row: 2/);
  assert.match(narrow, /\.local-office:is\(\.office-chat-first, \.office-standalone\) > \.cos-rail\s*\{[^}]*display: none;[^}]*pointer-events: auto/);
  assert.match(narrow, /\.local-office\.local-sidebar-open > \.cos-rail\s*\{[^}]*display: flex/);
  assert.match(narrow, /\.local-office:is\(\.office-chat-first, \.office-standalone\) > \.cos-rail > \[role="separator"\]\s*\{[^}]*display: none/);
});

test('your local office sidebar excludes hosted referrals across unrelated domains', () => {
  const tree = ts.createSourceFile('CompanyOffice.tsx', readFileSync(join(__dirname, 'CompanyOffice.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const slots = tree.statements.find(statement => ts.isVariableStatement(statement) && statement.declarationList.declarations.some(node => node.name.getText(tree) === 'SIDE_SLOTS'));
  const output = ts.transpileModule(`${slots.getText(tree)}\nSIDE_SLOTS;`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  for (const domain of ['Freight dispatch teams', 'Home cooking clubs']) {
    const items = vm.runInNewContext(output, {local: true, featureVisible: (surface, value) => value !== 'referrals.show'});
    assert.equal(items.some(item => item.id === 'referrals'), false, domain);
    assert.ok(items.some(item => item.id === 'idea'), domain);
  }
});

test('every sidebar tab and slot opens an existing view across domains', async () => {
  const source = readFileSync(join(__dirname, 'CompanyOffice.tsx'), 'utf8');
  const tree = ts.createSourceFile('CompanyOffice.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const declarations = tree.statements.filter(statement => ts.isVariableStatement(statement)
    && statement.declarationList.declarations.some(declaration => ['SIDE_TABS', 'SIDE_SLOTS'].includes(declaration.name.getText(tree))));
  let navigation, go;
  function find(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'go') go = node.getText(tree);
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText(tree) === 'nav'
      && node.openingElement.attributes.properties.some(attribute => attribute.name?.getText(tree) === 'aria-label' && attribute.initializer?.text === 'Main')) navigation = node.getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(navigation && go);
  const output = ts.transpileModule(`${declarations.map(node => node.getText(tree)).join('\n')}\n${go}\nfunction render() { return ${navigation}; }\nrender();`, {
    compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
  }).outputText;
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)];
  const routes = {exports: {}, URL, require: name => require(join(__dirname, '../../lib', name))};
  vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, '../../lib/chatRouteAction.ts'), 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  }).outputText, routes);
  for (const [ventureId, audience] of [['ven_fleet', 'Freight dispatch teams'], ['ven_kitchen', 'Home cooking clubs']]) {
    const paths = [], events = [], selected = [];
    const rendered = vm.runInNewContext(output, {React, ventureId, pathname: `/v/${ventureId}`, homeHref: `/v/${ventureId}`,
      history: {}, onHome: true, tab: 'home', local: false, featureVisible: () => true, newChat() {}, setSelected: value => selected.push(value),
      router: {push: path => paths.push(path)}, nativeRouter: {push: path => paths.push(path)},
      window: {dispatchEvent: event => events.push(event)}, CustomEvent,
    });
    const buttons = nodes(rendered).filter(node => node.type === 'button' && typeof node.props.children === 'string');
    assert.equal(buttons.length, 5);
    for (const button of buttons) {
      const label = button.props.children;
      const before = paths.length;
      button.props.onClick();
      const href = paths.at(-1);
      assert.equal(paths.length, before + 1, label);
      const tab = new URL(href, 'https://console.local').searchParams.get('office');
      if (tab) {
        assert.ok(['archives', 'toolkit'].includes(tab));
        assert.equal(selected.at(-1), tab);
        assert.ok(readFileSync(join(__dirname, 'CompanyHome.tsx'), 'utf8').includes(`tab === "${tab}"`));
      } else if (label === 'Connections') {
        assert.equal(href, `/connections?venture=${ventureId}`);
        assert.ok(readFileSync(join(__dirname, '../../app/connections/page.tsx'), 'utf8').length);
      } else {
        const suffix = href.slice(`/v/${ventureId}/`.length);
        assert.ok(readFileSync(join(__dirname, `../../app/v/[ventureId]/${suffix}/page.tsx`), 'utf8').length);
        assert.equal(routes.exports.homeRouteAction(href, ventureId).sidebar, 'view');
      }
    }
    assert.equal(events.length, 0, audience);
  }
});

test('Connections sidebar navigates once per click without posting chat messages for unrelated companies', () => {
  const source = readFileSync(join(__dirname, 'CompanyOffice.tsx'), 'utf8');
  const tree = ts.createSourceFile('CompanyOffice.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const slots = tree.statements.find(statement => ts.isVariableStatement(statement)
    && statement.declarationList.declarations.some(declaration => declaration.name.getText(tree) === 'SIDE_SLOTS'));
  let navigation;
  function find(node) {
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText(tree) === 'nav'
      && node.openingElement.attributes.properties.some(attribute => attribute.name?.getText(tree) === 'aria-label'
        && attribute.initializer?.text === 'Main')) navigation = node;
    ts.forEachChild(node, find);
  }
  find(tree);
  assert.ok(navigation);
  const output = ts.transpileModule(`${slots.getText(tree)}\nfunction render() { return ${navigation.getText(tree)}; }\nrender();`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  for (const ventureId of ['ven_wholesale_inventory', 'ven_family_recipes']) {
    const events = [], paths = [];
    const rendered = vm.runInNewContext(output, { React, ventureId, pathname: `/v/${ventureId}`, homeHref: `/v/${ventureId}`,
      chatFirst: true, local: false, featureVisible: () => true, selected: '', history: {}, onHome: true, tab: 'home', SIDE_TABS: [], newChat() {}, go() {},
      router: { push: path => paths.push(path) }, nativeRouter: { push: path => paths.push(path) }, window: { dispatchEvent: event => events.push(event) },
      CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    });
    const html = renderToStaticMarkup(rendered);
    assert.match(html, /<button[^>]*type="button"[^>]*data-sidebar-slot="connections"[^>]*>Connections<\/button>/);
    function nodes(node) { return !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(5).flatMap(nodes)]; }
    const button = nodes(rendered).find(node => node.props?.['data-sidebar-slot'] === 'connections');
    for (let click = 0; click < 3; click++) button.props.onClick();
    assert.deepEqual(events, []);
    assert.deepEqual(paths, Array(3).fill(`/connections?venture=${ventureId}`));
  }
});

test('Connections replaces chat with the main workspace and returns without sending a message across unrelated companies', () => {
  const shellTree = ts.createSourceFile('AppShell.tsx', readFileSync(join(__dirname, '../layout/AppShell.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const shell = shellTree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const chatShown = shell.body.statements.filter(ts.isVariableStatement).flatMap(statement => statement.declarationList.declarations)
    .find(declaration => declaration.name.getText(shellTree) === 'chatShown').initializer.getText(shellTree);
  const home = shell.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(shellTree) === 'founderUI === "home"').thenStatement.expression;
  let frameClass, launcherOpen, buddy;
  function visit(node) {
    if (ts.isJsxAttribute(node) && node.name.getText(shellTree) === 'className' && node.initializer?.getText(shellTree).includes('"app-shell"')) frameClass = node.initializer.expression.getText(shellTree);
    if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(shellTree) === 'VentureChatLauncher') launcherOpen = node.attributes.properties.find(attribute => attribute.name?.getText(shellTree) === 'open').initializer.expression.getText(shellTree);
    if (ts.isJsxExpression(node) && node.getText(shellTree).includes('className="charlie-buddy"')) buddy = node.expression.getText(shellTree);
    ts.forEachChild(node, visit);
  }
  visit(home);
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
  const compile = source => ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const output = compile(readFileSync(join(__dirname, 'CompanyOffice.tsx'), 'utf8'));
  const linkOutput = compile(readFileSync(join(__dirname, '../chat/ChatLink.tsx'), 'utf8'));
  for (const [ventureId, company] of [['ven_wholesale_inventory', 'Wholesale inventory planning'], ['ven_family_recipes', 'Family recipe sharing']]) {
    const messages = [];
    const links = { exports: {}, require: name => name === 'next/link' ? { default: 'a' } : name === '@/lib/chatRouteAction'
      ? { routeInChat: href => { messages.push(href); return true; } } : name === './PageResultLink' ? {pageResultKind: () => null} : require(name) };
    vm.runInNewContext(linkOutput, links);
    const mocks = {
      react: { useState: initial => [initial, () => {}], useEffect() {}, useRef: current => ({ current }) },
      'react/jsx-runtime': require('react/jsx-runtime'),
      'next/navigation': { useRouter: () => ({ push() {} }), useSearchParams: () => new URLSearchParams({ venture: ventureId }) },
      '@/components/chat/ChatLink': links.exports,
      '@/lib/chatRouteAction': { chatNavigation: router => router },
      '@/lib/founderRoutes': { founderRouteLinks: () => [] },
      '@/components/layout/LocalModeBoundary': { useLocalMode: () => false, useFeatureVisibility: () => () => true },
      '@/lib/chatVisibility': { chatFirstHidden: () => false },
      '@/lib/companyHome': { useCompanyHomeData: () => ({ data: { name: company }, owner: { epoch: 1, subject: ventureId } }) },
      './CompanyHome': { HOME_TABS: { home: 'Home' }, CompanyHomeContext: React.createContext(null), CompanyHome: () => null },
      './CompanyOffice.module.css': { default: new Proxy({}, { get: (_, name) => String(name) }) },
      '@/components/primitives/ErrorNotice': { ErrorNotice: () => null },
    };
    const office = { exports: {}, require: name => mocks[name] || {} };
    vm.runInNewContext(output, office);
    const content = React.createElement('div', { className: 'connectionsGrid' }, company);
    const rendered = office.exports.CompanyOffice({ ventureId, pathname: '/connections', chatAvailable: true, title: 'Connections', onSettings() {}, children: content });
    const all = nodes(rendered), main = all.find(node => node.props.className === 'main');
    const canvas = nodes(main).find(node => node.props.className === 'canvas workspace-body');
    assert.ok(nodes(canvas).includes(content));
    assert.ok(!nodes(all.find(node => node.type === 'aside')).includes(content));
    for (const chatPanelOpen of [false, true]) {
      const context = { React, local: false, localBuildActivity: false, localSubpage: false, founderUI: 'home', pathname: '/connections', chatPanelOpen, effectiveVentureId: ventureId,
        entryPage: false, officeTab: false, guidePage: false, chatAvailable: true, chatFirstHome: false, bare: false,
        cx: (...parts) => parts.filter(Boolean).join(' '), chatToggleRef: {}, managerName: 'Assistant' };
      context.chatShown = vm.runInNewContext(chatShown, context);
      assert.equal(context.chatShown, false);
      assert.doesNotMatch(vm.runInNewContext(frameClass, context), /chat-open|chat-first-home/);
      assert.equal(vm.runInNewContext(launcherOpen, context), false);
      assert.equal(vm.runInNewContext(ts.transpileModule(buddy, { compilerOptions: { jsx: ts.JsxEmit.React } }).outputText, context), false);
      assert.equal(vm.runInNewContext(chatShown, { ...context, pathname: `/v/${ventureId}`, chatFirstHome: true }), true);
      assert.equal(vm.runInNewContext(chatShown, { ...context, pathname: `/v/${ventureId}/guide`, guidePage: true }), chatPanelOpen);
    }
    const back = nodes(main).find(node => node.type === links.exports.default);
    assert.equal(back.props.children, 'Back to chat');
    assert.equal(back.props.href, `/v/${ventureId}`);
    const anchor = links.exports.default(back.props);
    const target = { hasAttribute: name => Boolean(anchor.props[name]), closest() { return this; }, getAttribute: () => back.props.href };
    const event = { target, currentTarget: target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
    anchor.props.onClick(event);
    links.exports.captureChatLink(event);
    assert.equal(event.defaultPrevented, false);
    assert.deepEqual(messages, []);
  }
  assert.match(readFileSync(join(__dirname, '../marketing/MarketingLab.module.css'), 'utf8'), /\.connectionsGrid\s*\{[^}]*grid-template-columns: repeat\(auto-fit, minmax\(min\(100%, 15rem\), 1fr\)\)/);
});

test('sidebar New chat waits for a confirmed private thread on every surface and keeps previous chats across domains', async () => {
  const source = readFileSync(join(__dirname, 'CompanyOffice.tsx'), 'utf8');
  const tree = ts.createSourceFile('CompanyOffice.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const functions = [], buttons = [];
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && ['newChat', 'openHistory', 'historyRow'].includes(node.name?.text)) functions.push(node.getText(tree));
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText(tree) === 'button'
      && node.openingElement.attributes.properties.some(attribute => attribute.name?.getText(tree) === 'onClick'
        && attribute.initializer?.getText(tree) === '{newChat}')) buttons.push(node.getText(tree));
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.equal(functions.length, 3); assert.equal(buttons.length, 1);
  const compile = text => ts.transpileModule(text, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React}}).outputText;
  const office = compile(`${functions.join('\n')}\nfunction newButton() { return ${buttons[0]}; }`);
  const bridge = compile(readFileSync(join(__dirname, '../chat/useChatHistory.ts'), 'utf8'));
  const {historyGroups} = await import('../../lib/chatConversations.ts');
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
  const now = new Date(), yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
  for (const [ventureId, audience] of [['ven_wholesale', 'Wholesale inventory teams'], ['ven_recipes', 'Families sharing recipes']]) {
    for (const surface of ['home', 'idea/current', 'leads', 'connections', 'archives', 'toolkit', 'departments']) {
      for (const confirmed of [false, true]) {
        const paths = [], events = [], requests = [], listeners = new Map();
        let complete;
        const pending = new Promise(resolve => {complete = resolve;});
        const current = `${ventureId}_shared`, fresh = `${ventureId}_private`;
        const oldItems = [{id: current, title: audience, visibility: 'legacy_shared', created_at: now.toISOString()},
          {id: `${ventureId}_earlier`, title: `Earlier ${audience}`, visibility: 'private', created_at: yesterday.toISOString()}];
        const onHome = ['home', 'archives', 'toolkit', 'departments'].includes(surface);
        const context = {React, ventureId, homeHref: `/v/${ventureId}`, chatFirst: surface === 'home', onHome,
          pathname: surface === 'connections' ? '/connections' : `/v/${ventureId}${onHome ? '' : `/${surface}`}`,
          selected: current, archived: true, historyEdit: null, folders: [], history: {}, exports: {},
          historyRowId: id => `chat-history-${ventureId}-${id}`, chatTitle: text => text,
          setSelected: value => {context.selected = value;}, setArchived: value => {context.archived = value;},
          router: {push() {assert.fail('New chat must navigate without requesting a recommendation');}},
          nativeRouter: {push: href => {paths.push(href); context.chatFirst = true;}},
          require: id => id === 'react' ? {useEffect: effect => effect(), useRef: current => ({current})} : {isLocalConsole: () => false},
          CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options?.detail;}},
          window: {dispatchEvent: event => {events.push(event); if (event.type === 'supymem:chat-history') context.history = event.detail;},
            addEventListener: (type, listener) => listeners.set(type, listener), removeEventListener() {}},
        };
        vm.runInNewContext(office + '\n' + bridge, context);
        const chat = {conversationsAvailable: true, conversationId: current, conversationItems: oldItems,
          archivedConversationItems: [], messages: [{id: 'old_message', role: 'founder', text: audience}],
          reloadConversations() {}, conversationBusy: false,
          async newConversation() {
            requests.push(ventureId); chat.conversationBusy = true; publish();
            const created = await pending;
            if (created) {
              chat.conversationId = fresh; chat.messages = [];
              chat.conversationItems = [{id: fresh, title: 'New chat', visibility: 'private', created_at: now.toISOString()}, ...oldItems];
            }
            chat.conversationBusy = false; publish(); return created;
          },
        };
        const publish = () => context.exports.useChatHistory(ventureId, chat);
        publish();
        assert.equal(context.history.newConversation, chat.newConversation);
        assert.equal(context.newButton().props.disabled, false);
        const started = context.newButton().props.onClick();
        assert.equal(context.newButton().props.disabled, true);
        await context.newButton().props.onClick();
        assert.deepEqual(requests, [ventureId]); assert.deepEqual(paths, []);
        assert.equal(context.history.current, current); assert.equal(context.selected, current);
        assert.equal(context.newButton().props['aria-current'], undefined);
        complete(confirmed); await started;
        if (!confirmed) {
          assert.equal(context.history.current, current); assert.equal(context.selected, current);
          assert.equal(context.history.items.length, 2); assert.deepEqual(paths, []);
          assert.equal(context.archived, true); assert.equal(events.some(event => event.type === 'supymem-chat-talk'), false);
          continue;
        }
        assert.equal(chat.conversationItems[0].visibility, 'private'); assert.deepEqual(chat.messages, []);
        assert.equal(context.history.current, fresh); assert.equal(context.archived, false);
        assert.deepEqual(paths, surface === 'home' ? [] : [`/v/${ventureId}`]);
        const highlighted = context.history.items.flatMap(item => nodes(context.historyRow(item)))
          .filter(node => node.props?.['aria-current']);
        assert.equal(highlighted.length, 1); assert.equal(highlighted[0].props.id, `chat-history-${ventureId}-${fresh}`);
        assert.equal(context.newButton().props['aria-current'], undefined);
        assert.equal(context.history.items.length, 3);
        assert.deepEqual(historyGroups(context.history.items, now).map(group => group.label), ['Today', 'Yesterday']);
        assert.equal(events.at(-1).type, 'supymem-chat-talk');
        context.chatFirst = false;
        context.openHistory(current);
        assert.equal(paths.at(-1), `/v/${ventureId}`);
        assert.equal(context.selected, current);
        if (onHome) {
          assert.equal(events.at(-1).type, 'supymem:chat-jump');
          assert.equal(events.at(-1).detail.id, current);
        }
      }
    }
  }
});

function officeFixture(domain, local, currentHistory) {
  const chatRoutes = require('../../lib/chatRouteAction.ts');
  const exported = {}, jsx = (type, props) => ({type, props});
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/CompanyOffice.tsx', 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
  }).outputText, {exports: exported, require(id) {
    if (id === 'react') return {useState: initial => [initial?.items && currentHistory ? currentHistory() : initial, () => {}], useEffect() {}, useRef: initial => ({current: initial})};
    if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => local, useFeatureVisibility: () => (surface, value) => !local || value !== 'referrals.show'};
    if (id === 'next/navigation') return {useRouter: () => ({push() {}}), useSearchParams: () => new URLSearchParams()};
    if (id === '@/lib/chatRouteAction') return chatRoutes;
    if (id === '@/lib/founderRoutes') return {founderRouteLinks: () => []};
    if (id === '@/lib/chatVisibility') return {chatFirstHidden: () => false};
    if (id === '@/lib/chatConversations') return require('../../lib/chatConversations.ts');
    if (id === '@/lib/companyHome') return {companyDepartments: () => [], useCompanyHomeData: () => ({data: {name: domain, agents: [], pins: [], errors: []}, owner: {epoch: 1, subject: 'owner'}})};
    if (id === './CompanyHome') return {HOME_TABS: {home: 'Home', archives: 'Tuesday Archives'}, CompanyHomeContext: {Provider: 'provider'}};
    return {default: new Proxy({}, {get: (_, key) => key}), ChatAnchor: 'a'};
  }});
  return exported.CompanyOffice;
}

const officeNodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(5).flatMap(officeNodes)];
const officeText = node => !node || typeof node !== 'object' ? String(node ?? '') : [node.props?.children].flat(5).map(officeText).join('');

test('your sidebar keeps saved documents discoverable and the folders you create across domains', () => {
  for (const domain of ['warehouse teams', 'weekend walkers']) for (const local of [false, true]) {
    let history = {items: [], current: ''};
    const office = officeFixture(domain, local, () => history);
    const render = () => office({ventureId: 'venture', pathname: '/v/venture', onSettings() {}, children: domain});
    const rows = () => officeNodes(render()).filter(node => node.type === 'button' && node.props.className === 'cos-row').map(officeText);
    assert.equal(rows().includes('Tuesday Archives'), !local, domain);
    history = {items: [{id: 'saved', text: 'Your notes', folder: 'Tuesday Archives', createdAt: new Date().toISOString()}], current: 'saved'};
    assert.match(officeText(render()), /Tuesday Archives/);
  }
});

test('your settings screens have one heading and one back path across domains', () => {
  for (const ventureId of ['ven_freight', 'ven_gardens']) {
    const office = officeFixture(ventureId, true);
    for (const [pathname, heading, back] of [['/settings/local', 'Keys and connections', `/v/${ventureId}`],
      ['/settings/local/capabilities', 'Capabilities', '/settings/local']]) {
      const children = {type: 'main', props: {children: [{type: 'h1', props: {children: heading}},
        pathname.endsWith('/capabilities') && {type: 'a', props: {href: back, children: 'Back to your keys and connections'}}]}};
      const tree = officeNodes(office({ventureId, pathname, title: 'Settings', onSettings() {}, children}));
      assert.deepEqual(tree.filter(node => node.type === 'h1').map(officeText), [heading]);
      assert.deepEqual(tree.filter(node => node.props?.href && officeText(node).startsWith('Back to ')).map(node => node.props.href), [back]);
    }
  }
});

test('your command palette explains focus and returns it on dismissal across domains', () => {
  const source = readFileSync(__dirname + '/CompanyOffice.tsx', 'utf8');
  const css = readFileSync(__dirname + '/CompanyOffice.module.css', 'utf8');
  for (const domain of ['Freight scheduling', 'Family gardening']) {
    assert.match(source, /role="combobox"[^>]*aria-expanded=/, domain);
    assert.match(source, /<ul[^>]*id="office-command-list"[^>]*role="listbox"/, domain);
    assert.match(source, /paletteInvoker\.current\?\.focus/, domain);
    assert.match(css, /paletteSearch input:focus-visible\s*\{[^}]*outline: 2px solid var\(--focus-ring\)/, domain);
  }
});

test('your local command routes exclude hosted destinations across domains', async () => {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/../../lib/founderRoutes.ts', 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText,
    {exports, require: id => require(id.includes('betaRoutes') ? '../../lib/betaRoutes.ts' : '../../lib/localMode.ts')});
  const {founderRouteLinks} = exports;
  for (const venture of ['invoice-workflow', 'garden-journal']) {
    const local = founderRouteLinks(venture, true);
    assert.equal(local.some(route => /download|\/people/.test(route.href)), false);
    assert.ok(local.some(route => route.href === '/settings/local'));
    assert.ok(founderRouteLinks(venture, false).some(route => route.href === '/download'));
  }
});
