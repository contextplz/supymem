const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync, readdirSync } = require('node:fs');
const { join, resolve } = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

test('home navigation stays in chat or opens a sidebar, and raw legacy links fail the source audit', () => {
  const root = resolve(__dirname, '../..');
  let shell;
  const events = [], paths = [];
  const window = { location: { href: 'https://app.supymem.com/', origin: 'https://app.supymem.com' }, dispatchEvent: event => events.push(event.detail) };
  const context = { window, document: { querySelector: () => shell }, URL,
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } } };
  function load(file, modules = {}) {
    const exports = {};
    vm.runInNewContext(ts.transpileModule(readFileSync(join(root, file), 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText, { ...context, exports, require: id => { assert.ok(id in modules, id); return modules[id]; } });
    return exports;
  }
  const routes = load('lib/chatRouteAction.ts', {
    './chatVisibility.ts': require('../../lib/chatVisibility.ts'),
    './localMode.ts': require('../../lib/localMode.ts'), './operationsScope.ts': require('../../lib/operationsScope.ts'),
  });
  const nativeExpressions = [...readFileSync(resolve(root, 'components/chat/ChatDock.tsx'), 'utf8')
    .matchAll(/data-chat-route-native=\{([^}]+)\}/g)].map(match => match[1]);
  assert.equal(nativeExpressions.length, 2);
  const jsx = (type, props) => ({ type, props });
  const links = load('components/chat/ChatLink.tsx', { '@/lib/chatRouteAction': routes,
    './PageResultLink': {pageResultKind: () => null},
    'react/jsx-runtime': { jsx, jsxs: jsx }, 'next/link': { default: 'a' } });
  const nativeRouter = { push: (href, options) => paths.push(['push', href, options]), replace: (href, options) => paths.push(['replace', href, options]) };
  const router = routes.chatNavigation(nativeRouter);
  assert.equal(routes.chatNavigation(nativeRouter), router, 'navigation does not invalidate effects on each render');
  const catalog = readFileSync(resolve(root, '../../../services/company_os/app/analytics/feature_catalog.py'), 'utf8');
  for (const { venture, audience } of [{ venture: 'ven_warehouse', audience: 'warehouse operators' }, { venture: 'ven_gardening', audience: 'home gardeners' }]) {
    shell = { dataset: { ventureId: venture } };
    window.location.href = `https://app.supymem.com/v/${venture}`;
    assert.equal(routes.chatRouteAction({ role: 'assistant', body_md: `For ${audience}, review /v/${venture}/idea.` }, venture)[0].href, `/v/${venture}/idea`);
    for (const [suffix, action] of [['idea', 'idea.review'], ['research', 'research'], ['site', 'site.preview'],
      ['site?build=1', 'pagebuilder.build'], ['tasks', 'status'], ['plan', 'status'], ['builds', 'status'],
      ['experiments', 'experiment.list'], ['suggestions', 'whats_next']]) {
      router.push(`/v/${venture}/${suffix}`);
      assert.equal(events.at(-1).action, action, suffix);
      assert.ok(catalog.includes(`"${action}"`), `${action} already has analytics attribution`);
    }
    assert.deepEqual(JSON.parse(JSON.stringify(events.find(event => event.action === 'pagebuilder.build').payload)), { prepare: true });
    router.replace(`/v/${venture}/settings`);
    assert.equal(events.at(-1).sidebar, 'settings');
    router.push(`/v/${venture}/settings#settings-computer-use`);
    assert.equal(events.at(-1).section, 'mac');
    router.push(`/v/${venture}/settings#god-mode`);
    assert.equal(events.at(-1).section, 'manager');
    for (const [suffix, section] of [['settings', 'venture'], ['settings#settings-computer-use', 'mac']]) {
      const recovery = { label: `Recover settings for ${audience}`, href: `/v/${venture}/${suffix}`, kind: 'recovery_link' };
      assert.equal(routes.inChatActions([recovery])[0], recovery);
      for (const expression of nativeExpressions) {
        const native = vm.runInNewContext(expression, { a: recovery });
        assert.equal(native, undefined);
        assert.equal(vm.runInNewContext(expression, { a: { ...recovery, kind: 'secret_form' } }), true);
        let prevented = 0;
        links.ChatAnchor({ href: recovery.href, 'data-chat-route-native': native }).props.onClick({
          currentTarget: { hasAttribute: () => Boolean(native) }, preventDefault() { prevented++; },
        });
        assert.equal(prevented, 1);
        assert.equal(events.at(-1).sidebar, 'settings');
        assert.equal(events.at(-1).section, section);
      }
    }
    router.push('/account');
    assert.equal(events.at(-1).section, 'account');
    router.push(`/v/${venture}/device-work-orders/order_saved`);
    assert.equal(events.at(-1).sidebar, 'view');
    assert.equal(events.at(-1).href, `/v/${venture}/device-work-orders/order_saved`);
    router.push(`/connections?venture=${venture}`);
    assert.equal(events.at(-1).sidebar, 'toolkit');
    router.push(`/gtm?venture=${venture}`);
    assert.equal(events.at(-1).action, 'gtm.status');
    for (const [href, action] of [['/content', 'content.drafts'], ['/community', 'gtm.status'], ['/ops/finance', 'money']]) {
      router.push(href); assert.equal(events.at(-1).action, action);
      assert.deepEqual(JSON.parse(JSON.stringify(routes.inChatActions([{ label: 'Open', href }], false))), [{ label: 'Open', href }]);
    }
    assert.equal(paths.length, 0);
    for (const href of ['/guide', `/guide?venture=${venture}`, `/v/${venture}/guide`]) {
      const before = events.length;
      router.push(href);
      assert.equal(events.length, before, 'the dedicated guide is not a chat action');
      assert.deepEqual(paths.splice(0), [['push', href, undefined]]);
    }
    const anchor = { getAttribute: () => `/v/${venture}/research`, hasAttribute: () => false };
    let prevented = 0, clicked = 0;
    const event = { target: { closest: () => anchor }, currentTarget: anchor, preventDefault() { prevented++; }, stopPropagation() {} };
    links.captureChatLink(event);
    assert.equal(prevented, 1);
    const before = events.length;
    links.default({ href: `/v/${venture}/research`, onClick(event) { event.preventDefault(); } }).props.onClick({ ...event,
      defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } });
    assert.equal(events.length, before, 'the original scope and validation handler may refuse navigation');
    anchor.hasAttribute = () => true;
    links.ChatAnchor({ href: `/v/${venture}/connections`, 'data-chat-route-native': true, onClick() { clicked++; } }).props.onClick(event);
    links.captureChatLink(event);
    assert.equal(clicked, 1);
    assert.equal(events.length, before, 'secure connection forms keep their own action');
    for (const href of [`https://foreign.example/v/${venture}/plan`, `https://user@app.supymem.com/v/${venture}/plan`, `/v/${venture}/connect/github`]) {
      assert.equal(routes.routeInChat(href), false, href);
    }
    const other = `${venture}_other`;
    router.push(`/v/${other}/site?build=1`);
    const handoff = new URL(events.at(-1).href, window.location.origin);
    assert.equal(handoff.pathname, `/v/${other}`);
    assert.equal(routes.homeRouteAction(handoff.searchParams.get('chat_route'), other).action, 'pagebuilder.build');
    shell = null;
    router.push(`/v/${venture}/plan`, { scroll: false });
    router.replace('/connections');
    assert.deepEqual(paths.splice(0), [['push', `/v/${venture}/plan`, { scroll: false }], ['replace', '/connections', undefined]]);
  }

  const segments = require('../../lib/chatVisibility.ts').CHAT_FIRST_HIDDEN_SEGMENTS;
  const legacy = new RegExp(`/(?:v/[^\\n]+/(?:idea|${segments.join('|')})(?:[/?#\\x22\\x27\\x60}]|$)|(?:gtm|connections|account|content|community|marketing|guide|ops)(?:[/?#\\x22\\x27\\x60}]|$))`);
  const files = readdirSync(join(root, 'components'), { recursive: true }).filter(file => /\.(?:tsx|ts)$/.test(file) && !file.includes('.test.') && !file.startsWith('admin/'))
    .map(file => `components/${file}`).concat(['app/new/page.tsx', 'app/new/ConfirmedConversation.tsx', 'app/v/[ventureId]/page.tsx', 'app/v/[ventureId]/device-work-orders/[orderId]/page.tsx', 'app/v/[ventureId]/idea/current/page.tsx', 'app/v/[ventureId]/linkedin/page.tsx']);
  const leaks = [];
  for (const file of files) {
    if (file === 'components/chat/ChatLink.tsx') continue;
    const text = readFileSync(join(root, file), 'utf8');
    const tree = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const rawLink = /import Link from ["']next\/link["']/.test(text);
    function visit(node) {
      if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
        const tag = node.tagName.getText(tree);
        const href = node.attributes.properties.find(prop => ts.isJsxAttribute(prop) && prop.name.getText(tree) === 'href')?.initializer?.getText(tree) || '';
        const external = node.attributes.properties.some(prop => ts.isJsxAttribute(prop) && prop.name.getText(tree) === 'target' && prop.initializer?.getText(tree) === '"_blank"');
        const ideaQuestionnaire = file === 'app/v/[ventureId]/idea/current/page.tsx' && tag === 'a' && href === '{`/v/${encodeURIComponent(ventureId)}/idea`}'
          && node.attributes.properties.some(prop => ts.isJsxAttribute(prop) && prop.name.getText(tree) === 'data-chat-route-native' && !prop.initializer);
        if (!ideaQuestionnaire && (tag === 'a' || tag === 'Link' && rawLink) && (legacy.test(href) || !external && /^\{(?:href|\w*Href|\w+\.href|committedEntryHref\(.*)\}$/.test(href))) leaks.push(`${file}: ${node.getText(tree)}`);
      }
      if (ts.isCallExpression(node) && /^(?:router\.(?:push|replace)|window\.location\.(?:assign|replace))$/.test(node.expression.getText(tree)) && legacy.test(node.arguments[0]?.getText(tree) || '')) {
        if (!text.includes('chatNavigation(useRouter())') && !node.parent.getText(tree).includes('routeInChat(')) leaks.push(`${file}: ${node.getText(tree)}`);
      }
      ts.forEachChild(node, visit);
    }
    if (file === 'components/layout/AppShell.tsx') {
      const shellFunction = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
      visit(shellFunction.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(tree) === 'founderUI === "home"').thenStatement);
    } else visit(tree);
  }
  assert.deepEqual(leaks, []);
  const shellSource = readFileSync(join(root, 'components/layout/AppShell.tsx'), 'utf8');
  assert.match(shellSource, /founderUI === "home" && !guidePage \|\| chatPanelOpen/, 'the guide stays visible on mobile until chat is deliberately opened');
  assert.match(readFileSync(join(root, 'components/office/CompanyOffice.tsx'), 'utf8'), /aria-label="How to use Supymem"[^>]*data-usage-control="guide_nav"/, 'the chat-first sidebar exposes the dedicated guide');
});
