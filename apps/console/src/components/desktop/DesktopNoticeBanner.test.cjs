const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

const src = (...p) => readFileSync(join(__dirname, '..', '..', ...p), 'utf8');
const rule = (css, selector) => {
  const found = new RegExp(`^${selector.replace('.', '\\.')}\\s*\\{([^}]*)\\}`, 'm').exec(css);
  assert.ok(found, `${selector} rule exists`);
  return found[1];
};

function reviewBanner(target, layout = 'home', currentPath = '/v/ven_review', ventureId = 'ven_review', noticeValues = {}, messages) {
  const paths = [], updates = [], effects = [], receipts = [];
  const jsx = (type, props) => ({ type, props });
  const mocks = {
    react: { useContext: () => layout, useEffect: effect => effects.push(effect), useState: () => [{
      id: 'not_review', title: 'Task finished', body: 'Saved result', target_path: target,
      subject: 'usr_review', device_id: 'device_review', auth_epoch: 1,
      ...noticeValues,
    }, value => updates.push(value)] },
    'react/jsx-runtime': { jsx, jsxs: jsx },
    'react-dom': {},
    'next/navigation': { usePathname: () => currentPath, useRouter: () => ({ push: path => paths.push(path) }) },
    '@/lib/desktop': { desktopInvoke: (...args) => receipts.push(args) },
    '@/lib/client': { verifiedSessionSubject: () => 'usr_review', verifiedSessionEpoch: () => 1 }, '@/lib/founderUI': {},
    './localMode.ts': { isLocalConsole: () => false },
    './operationsScope.ts': require('../../lib/operationsScope.ts'),
    './chatVisibility.ts': require('../../lib/chatVisibility.ts'),
    './DesktopNoticeBanner.module.css': { default: {} },
  };
  function compiled(source) {
    const exports = {};
    const code = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText;
    vm.runInNewContext(code, { exports, URL, require: name => {
      assert.ok(name in mocks, `unexpected import ${name}`);
      return mocks[name];
    } });
    return exports;
  }
  mocks['@/lib/desktopNoticeTarget'] = compiled(src('lib', 'desktopNoticeTarget.ts'));
  mocks['@/lib/chatRouteAction'] = compiled(src('lib', 'chatRouteAction.ts'));
  mocks['@/components/office/avatars'] = compiled(src('components', 'office', 'avatars.tsx'));
  const banner = compiled(src('components', 'desktop', 'DesktopNoticeBanner.tsx')).DesktopNoticeBanner({ ventureId, messages });
  const button = banner?.props.children[1]?.props?.children?.[0];
  return { banner, button, paths, updates, mocks, effects, receipts };
}

test('open-chat notices dedupe only visible assistant copy and otherwise stay compact without sidebar tooltips', () => {
  for (const [ventureId, summary] of [
    ['ven_inventory', 'Warehouse inventory alerts for logistics teams.'],
    ['ven_running', 'A weekend running club for neighborhood families.'],
  ]) {
    const target = `/v/${ventureId}/now`, currentPath = `/v/${ventureId}`;
    const notice = { title: 'Your Idea page changed', body: `Your Idea page changed: ${summary}` };
    const message = { id: 'msg_update', role: 'assistant', body_md: `<!--employee:Guide:Manager:manager-->\n${notice.body}` };
    const view = (messages, values = notice, destination = target) => reviewBanner(destination, 'home', currentPath, ventureId, values, messages);
    const duplicate = view([message]);
    assert.equal(duplicate.banner, null);
    duplicate.effects[2]();
    assert.deepEqual(duplicate.receipts, []);
    for (const messages of [[], [{ ...message, role: 'user' }], [{ ...message, body_md: 'A different saved update.' }]]) {
      const shown = view(messages);
      assert.equal(shown.banner.props.children[0].props.children[0].props.children, notice.title);
      assert.equal(shown.banner.props.children[0].props.children[1].props.children, notice.body);
      assert.equal(shown.button.props.children, 'Open in chat');
    }
    assert.ok(view([message], notice, '/v/ven_unrelated/now').banner);
    assert.ok(view([message], notice, 'https://untrusted.test/review').banner);
    assert.ok(view([message], { ...notice, body: 'Your Idea page changed' }).banner);
    const longBody = `${notice.body} ${`🌱 ${summary}`.repeat(20)}`;
    assert.equal(view([{ ...message, body_md: longBody }], { ...notice, body: [...longBody].slice(0, 400).join('') }).banner, null);
    const spacedBody = `${notice.body}${' '.repeat(400)}More saved detail.`;
    assert.equal(view([{ ...message, body_md: spacedBody }], { ...notice, body: spacedBody.slice(0, 400).trim() }).banner, null);
  }
  const css = src('components', 'desktop', 'DesktopNoticeBanner.module.css');
  assert.match(rule(css, '.banner'), /flex-direction:\s*column/);
  assert.match(rule(css, '.banner'), /min-width:\s*0;[^}]*max-width:\s*100%/);
  assert.match(rule(css, '.copy'), /width:\s*100%/);
  assert.match(rule(css, '.compact p'), /-webkit-line-clamp:\s*2;[^}]*overflow:\s*hidden;[^}]*text-overflow:\s*ellipsis/);
  assert.match(rule(css, '.actions'), /flex-wrap:\s*wrap/);
  assert.match(src('components', 'layout', 'AppShell.tsx'), /\(!chatShown \|\| officeTab\) && <DesktopNoticeBanner/);
  assert.match(src('components', 'layout', 'AppShell.tsx'), /open=\{chatShown && !officeTab\}/);
  assert.match(src('components', 'chat', 'ChatDock.tsx'), /homePanel && visible && <DesktopNoticeBanner[^>]*messages=\{messages\}/);
  assert.match(src('app', 'new', 'page.tsx'), /<DesktopNoticeBanner[^>]*messages=\{turns.map\(/);
  assert.doesNotMatch(src('components', 'office', 'CompanyOffice.tsx'), /title=\{item.hint\}/);
});

function openChatReview(view, query, layout = 'home') {
  const source = src('components', 'layout', 'AppShell.tsx');
  const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect;
  function find(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect'
        && node.arguments[0].getText(tree).includes('searchParams.get("notice_review")')) effect = node.arguments[0].getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree);
  assert.ok(effect);
  const actions = [], paths = [], opened = [];
  const searchParams = new URLSearchParams(query);
  vm.runInNewContext(ts.transpileModule(`(${effect})();`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, {
    founderUI: layout, chatAvailable: true, effectiveVentureId: 'ven_review', chatFirstEntry: searchParams.get('chat') === '1',
    searchParams, ...view.mocks['@/lib/desktopNoticeTarget'], ...view.mocks['@/lib/chatRouteAction'],
    setPendingAsk: action => actions.push(action), setChatPanelOpen: value => opened.push(value),
    router: { replace: path => paths.push(path) },
  });
  return { actions, paths, opened };
}

test('chat-first notice review opens venture chat and runs the matching existing action without a hidden-page detour', () => {
  for (const [segment, action] of [
    ['now', 'status'], ['connections', 'connections.status'], ['analytics', 'status'],
    ['settings', 'status'], ['suggestions', 'whats_next'],
  ]) {
    const view = reviewBanner(`/v/ven_review/${segment}`);
    assert.equal(view.button.props.children, 'Open in chat');
    view.button.props.onClick();
    assert.deepEqual(view.paths, [`/v/ven_review?chat=1&notice_review=${segment}`]);
    assert.deepEqual(view.updates, [null]);
    const opened = openChatReview(view, view.paths[0].split('?')[1]);
    assert.equal(opened.actions.length, 1);
    assert.equal(opened.actions[0].action, action);
    assert.equal(opened.actions[0].href, undefined);
    assert.deepEqual(opened.opened, [true]);
    assert.deepEqual(opened.paths, ['/v/ven_review']);
  }
});

test('desktop-only reviews without a chat equivalent retain the banner and trusted navigation', () => {
  for (const target of ['/v/ven_review/device-work-orders/dwo_review', '/v/ven_review#attention-ht_review']) {
    const view = reviewBanner(target);
    assert.equal(view.button.props.children, 'Open review');
    view.button.props.onClick();
    assert.deepEqual(view.paths, [target]);
  }
});

test('classic notice reviews keep their existing page navigation', () => {
  const target = '/v/ven_review/now', view = reviewBanner(target, 'classic');
  assert.equal(view.button.props.children, 'Open review');
  view.button.props.onClick();
  assert.deepEqual(view.paths, [target]);
});

test('notice review handoff rejects untrusted routes and never runs actions in classic mode', () => {
  const view = reviewBanner('/v/ven_review/now');
  for (const query of ['chat=1&notice_review=../now', 'chat=1&notice_review=now%3Fnext%3Dhttps://evil.test', 'chat=1&notice_review=not-a-page', 'notice_review=now', '']) {
    assert.deepEqual(openChatReview(view, query).actions, []);
  }
  assert.deepEqual(openChatReview(view, 'chat=1&notice_review=now', 'classic').actions, []);
});

test('notice review opens the result from onboarding or a desktop page with no active venture chat', () => {
  for (const [path, ventureId] of [['/new', 'ven_review'], ['/account', null]]) {
    const view = reviewBanner('/v/ven_review/now', 'home', path, ventureId);
    view.button.props.onClick();
    assert.deepEqual(view.paths, ['/v/ven_review?chat=1&notice_review=now']);
    assert.equal(openChatReview(view, view.paths[0].split('?')[1]).actions[0].action, 'status');
  }
});

test('venture chat pages pin the composer: column shrinks to the clipped body, thread scrolls, composer never shrinks', () => {
  const css = src('app', 'new', 'page.module.css');
  for (const root of ['.chat', '.confirmedChat']) {
    const body = rule(css, root);
    assert.match(body, /min-height:\s*0/);
    assert.doesNotMatch(body, /min-height:\s*[1-9]/, `${root} must not force a height taller than a short window`);
  }
  assert.match(rule(css, '.thread'), /flex:\s*1;[^}]*min-height:\s*0;[^}]*overflow-y:\s*auto/);
  assert.match(rule(css, '.ventureThread'), /flex:\s*1;\s*min-height:\s*0/);
  assert.match(rule(css, '.composerArea'), /flex:\s*none/);
  assert.match(rule(css, '.context'), /flex:\s*none/);
});

test('the notice banner is bounded so it cannot push the composer out of view', () => {
  const body = rule(src('components', 'desktop', 'DesktopNoticeBanner.module.css'), '.banner');
  assert.match(body, /max-height:\s*30vh/);
  assert.match(body, /overflow-y:\s*auto/);
});

test('venture notices keep their natural height in the scrollable office canvas', () => {
  const canvas = rule(src('components', 'office', 'CompanyOffice.module.css'), '.canvas');
  assert.match(canvas, /display:\s*grid/);
  assert.match(canvas, /grid-auto-rows:\s*max-content/);
  assert.match(canvas, /min-height:\s*0/);
  assert.match(canvas, /overflow:\s*auto/);
  assert.doesNotMatch(canvas, /(?:^|[;\s])(?:height|max-height):/);
  assert.match(src('components', 'office', 'CompanyOffice.tsx'), /className=\{`\$\{styles\.canvas\} workspace-body`\}[^>]*>\{notices\}\{children\}/);
});

test("another venture's blocker is compact inside any venture context, including /new idea clarification", () => {
  const banner = src('components', 'desktop', 'DesktopNoticeBanner.tsx');
  assert.match(banner, /isOtherVentureNotice\(notice\.target_path, pathname, ventureId\)/);
  assert.match(banner, /1 other venture needs you/);
  assert.match(src('components', 'layout', 'AppShell.tsx'), /<DesktopNoticeBanner ventureId=\{effectiveVentureId\} \/>/);
});

test('other-venture notices reserve the same row before arrival so content does not move', () => {
  const banner = src('components', 'desktop', 'DesktopNoticeBanner.tsx');
  assert.match(banner, /if \(!notice\) \{\s*if \(otherVentureTarget\) return null;\s*return ventureId \|\| pathname === "\/new" \? <div className=\{styles\.other\} aria-hidden="true" \/> : null/);
  assert.match(banner, /const line = <aside className=\{`\$\{styles\.other\}/);
  assert.match(rule(src('components', 'desktop', 'DesktopNoticeBanner.module.css'), '.other'), /min-height:\s*24px/);
  assert.match(src('components', 'desktop', 'DesktopNoticeBanner.module.css'), /\.other button\s*\{[^}]*white-space:\s*nowrap/);
});

test('recommendation badges never break inside the word and the idea hint precedes its textbox', () => {
  for (const file of [['components', 'chat', 'ChatDock.module.css'], ['app', 'v', '[ventureId]', 'idea', 'idea.module.css']]) {
    const badge = rule(src(...file), '.recommended');
    assert.match(badge, /white-space:\s*nowrap/);
    assert.match(badge, /overflow-wrap:\s*normal/);
    assert.match(badge, /flex-shrink:\s*0/);
  }
  const idea = src('app', 'v', '[ventureId]', 'idea', 'page.tsx');
  assert.match(idea, /className=\{styles\.recommended\}>Recommended/);
  assert.match(src('components', 'chat', 'ChatDock.tsx'), /className=\{styles\.recommended\}>Recommended/);
  assert.ok(idea.indexOf('Or type or speak your own answer below.') < idea.indexOf('<textarea id="idea-answer"'));
});
