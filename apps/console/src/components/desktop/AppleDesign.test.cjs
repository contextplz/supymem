const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync, statSync } = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');
const root = path.resolve(__dirname, '../..');
const source = file => readFileSync(path.join(root, file), 'utf8');
const nodes = value => !value || typeof value !== 'object' ? [] : [value, ...[value.props?.children].flat(8).flatMap(nodes)];
const text = value => typeof value === 'string' || typeof value === 'number' ? String(value) :
  !value || typeof value !== 'object' ? '' : [value.props?.children].flat(8).map(text).join('');
function harness(file, mocks = {}, initial = []) {
  const state = [...initial], effects = [], cache = new Map();
  let cursor = 0;
  const jsx = (type, props) => ({ type, props });
  const imports = {
    react: {
      useState(value) {
        const index = cursor++;
        if (!(index in state)) state[index] = typeof value === 'function' ? value() : value;
        return [state[index], next => { state[index] = typeof next === 'function' ? next(state[index]) : next; }];
      },
      useRef(current) { const index = cursor++; return state[index] ??= { current }; },
      useId: () => `id-${cursor++}`, useEffect: effect => effects.push(effect), useCallback: callback => callback,
      useContext: context => context.value,
    },
    'react/jsx-runtime': { jsx, jsxs: jsx }, 'next/link': { default: 'a' }, '@/components/chat/ChatLink': { default: 'a', ChatAnchor: 'a', captureChatLink() {} },
    'next/navigation': { usePathname: () => '/', useRouter: () => ({ push() {} }) },
    '@/lib/client': {}, '@/lib/desktop': {},
    '@/lib/founderUI': { FounderUIContext: { value: 'classic' } },
    '@/components/layout/LocalModeBoundary': { useLocalMode: () => false, useFeatureVisibility: () => () => true },
    './LocalModeBoundary': { useLocalMode: () => false, useFeatureVisibility: () => () => true },
    '@/components/primitives/ErrorNotice': { ErrorNotice: 'error' },
    './VentureCard': { VentureCard: 'card' }, './DeleteVentureDialog': { DeleteVentureDialog: 'dialog' },
    './BetaPathStrip': { BetaPathStrip: 'journey' }, ...mocks,
  };
  function load(filename) {
    if (cache.has(filename)) return cache.get(filename).exports;
    const loaded = new Module(filename, module);
    cache.set(filename, loaded); loaded.filename = filename; loaded.paths = Module._nodeModulePaths(path.dirname(filename));
    const native = loaded.require.bind(loaded);
    loaded.require = name => {
      if (name in imports) return imports[name];
      if (name.endsWith('.css')) return { default: new Proxy({}, { get: (_, key) => key }) };
      if (name.startsWith('@/') || name.startsWith('.')) {
        const base = name.startsWith('@/') ? path.join(root, name.slice(2)) : path.resolve(path.dirname(filename), name);
        const found = [base, `${base}.ts`, `${base}.tsx`].find(candidate => { try { return statSync(candidate).isFile(); } catch { return false; } });
        if (found) return load(found);
      }
      return native(name);
    };
    loaded._compile(ts.transpileModule(readFileSync(filename, 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText, filename);
    return loaded.exports;
  }
  return { exports: load(path.join(root, file)), state, effects, render(component, props = {}) { cursor = 0; effects.length = 0; return component(props); } };
}
const venture = { id: 'venture-a', name: 'Harbor', stage: 'live', agents: { hired: 4, working: 2 } };

test('U47 Add Venture is the first tile even with 45 saved ventures', () => {
  const ventures = Array.from({ length: 45 }, (_, index) => ({ ...venture, id: `venture-${index}` }));
  const loaded = harness('components/ventures/VentureHub.tsx', {
    '@/lib/founderUI': { FounderUIContext: { value: 'home' } },
  }, [false, ventures, [], 7, false]);
  const grid = nodes(loaded.render(loaded.exports.VentureHub)).find(node => node.props?.['aria-label'] === 'Ventures');
  const tiles = nodes(grid).filter(node => node.type === 'li');
  assert.equal(tiles.length, 46);
  assert.equal(text(tiles[0]), '+Add Venture');
  assert.equal(nodes(tiles[1]).find(node => node.type === 'a').props.href, '/v/venture-0');
});

test('1: card actions live behind a named native disclosure, retain IDs, and close with focus restored', () => {
  const loaded = harness('components/ventures/VentureCard.tsx');
  const calls = [], props = { venture, busy: false, error: '', onPause: () => calls.push('pause'), onResume: () => calls.push('resume'), onDelete: () => calls.push('delete') };
  let rendered = nodes(loaded.render(loaded.exports.VentureCard, props));
  const disclosure = rendered.find(node => node.type === 'details');
  assert.ok(disclosure);
  const summary = nodes(disclosure).find(node => node.type === 'summary');
  assert.equal(summary.props['aria-label'], 'Actions for Harbor');
  assert.equal(summary.props['data-usage-control'], 'venture_more');
  assert.equal(disclosure.props.open, undefined);
  assert.equal(rendered.filter(node => node.type === 'button').length, nodes(disclosure).filter(node => node.type === 'button').length);
  let focused = 0;
  const menu = { open: true, querySelector: () => ({ focus: () => focused++ }), contains: target => target === 'inside' };
  const event = { currentTarget: { closest: () => menu }, preventDefault() {}, stopPropagation() {} };
  nodes(disclosure).find(node => node.props?.['data-usage-control'] === 'venture_pause').props.onClick(event);
  assert.equal(menu.open, false); assert.equal(focused, 1);
  menu.open = true;
  nodes(disclosure).find(node => node.props?.['aria-label'] === 'Delete Harbor').props.onClick(event);
  assert.deepEqual(calls, ['pause', 'delete']); assert.equal(menu.open, false);
  menu.open = true;
  disclosure.props.onKeyDown({ key: 'Escape', currentTarget: menu, preventDefault() {} });
  assert.equal(menu.open, false); assert.equal(focused, 3);
  menu.open = true;
  disclosure.props.onBlur({ currentTarget: menu, relatedTarget: 'inside' }); assert.equal(menu.open, true);
  disclosure.props.onBlur({ currentTarget: menu, relatedTarget: null }); assert.equal(menu.open, false);
  props.venture = { ...venture, paused: true }; props.busy = true;
  rendered = nodes(loaded.render(loaded.exports.VentureCard, props));
  assert.ok(rendered.filter(node => node.type === 'button').every(node => node.props.disabled));
  props.busy = false;
  rendered = nodes(loaded.render(loaded.exports.VentureCard, props));
  rendered.find(node => node.props?.['data-usage-control'] === 'venture_resume').props.onClick(event);
  assert.deepEqual(calls, ['pause', 'delete', 'resume']);
});

test('2, 17: desktop rail reveals whole labels after deliberate hover and pushes the workspace', () => {
  const css = source('components/layout/SideNav.module.css'), tokens = source('styles/tokens.css');
  const rail = css.slice(css.indexOf('@media (min-width: 1024px)'), css.indexOf('@media (max-width: 1023px)'));
  assert.match(rail, /opacity var\(--d-fast\) var\(--e-out\) var\(--sidebar-label-delay\)/);
  assert.match(rail, /visibility 0s var\(--sidebar-label-delay\)/);
  assert.match(rail, /\.groupHead\s*\{\s*opacity: 0;\s*visibility: hidden;\s*transition: none/);
  assert.doesNotMatch(rail, /\.groupHead\s*\{[^}]*height: 0/);
  assert.match(tokens, /--sidebar-label-delay: var\(--d-base\)/);
  assert.doesNotMatch(tokens, /transition: width|transition-delay: 60ms/);
  assert.match(tokens, /\.sidebar::before\s*\{[^}]*width: 100%/);
  assert.match(tokens, /\.app-shell:is\(\.sidebar-pinned, \.sidebar-expanded\).*grid-template-columns: var\(--w-sidebar\)/);
  assert.doesNotMatch(tokens, /\.sidebar:hover/);
  assert.match(source('components/layout/AppShell.tsx'), /setSidebarExpanded\(true\);\s*\}, 250\)/);
  assert.match(css, /\.label\s*\{\s*min-width: 0;\s*flex: 1/);
  assert.match(source('components/layout/SideNav.tsx'), /aria-label=\{item.label\}/);
  assert.match(source('components/layout/SideNav.tsx'), /title=\{`\$\{item.label\}: \$\{item.description\}/);
  assert.match(source('components/guide/GuideNavLink.tsx'), /aria-label="How to use Supymem"/);
});

test('1: existing typed delete confirmation retries mismatch and hands its recovery receipt to hub Undo', async () => {
  const requests = [], receipts = [];
  let mismatch = true;
  const loaded = harness('components/ventures/DeleteVentureDialog.tsx', { '@/lib/client': { api: async (url, options) => {
    requests.push({ url, options });
    return mismatch ? { ok: false, status: 422, data: { error: { code: 'confirmation_mismatch', message: 'Type DELETE to confirm.' } } } : { ok: true };
  } } });
  const props = { venture, retentionDays: 7, onClose() {}, onDeleted: receipt => receipts.push(receipt) };
  const render = () => nodes(loaded.render(loaded.exports.DeleteVentureDialog, props));
  const submit = () => render().find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
  assert.ok(render().find(node => node.props?.role === 'dialog' && node.props['aria-modal'] === 'true'));
  submit();
  assert.equal(render().find(node => node.type === 'button' && text(node) === 'Continue').props.disabled, true);
  submit(); assert.equal(requests.length, 0);
  render().find(node => node.type === 'input').props.onChange({ target: { value: 'delete' } });
  submit(); assert.ok(render().find(node => node.props?.['data-usage-control'] === 'venture_delete'));
  submit(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(render().find(node => node.type === 'input').props['aria-invalid'], true);
  assert.equal(receipts.length, 0);
  mismatch = false;
  render().find(node => node.type === 'input').props.onChange({ target: { value: 'DELETE' } });
  submit(); submit(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 2);
  assert.ok(requests.every(request => request.url === '/api/ventures/venture-a/delete' && JSON.parse(request.options.body).confirmation === 'DELETE'));
  const overlay = render().find(node => node.props?.['data-closing']);
  assert.equal(overlay.props.inert, true);
  assert.equal(receipts.length, 0);
  overlay.props.onTransitionEnd({ target: 'overlay', currentTarget: 'overlay', propertyName: 'opacity' });
  assert.equal(receipts[0].retentionDays, 7);

  const calls = [], hub = harness('components/ventures/VentureHub.tsx', { '@/lib/client': {
    api: async url => { calls.push(url); return { ok: true, data: { items: [] } }; },
    ensureSession: async () => ({ ok: true }), apiSwr: async (url, update) => update({ ok: true, data: { items: [] } }),
  } }, [false, [venture], [], 7, false]);
  const hubRender = () => nodes(hub.render(hub.exports.VentureHub));
  hubRender().find(node => node.type === 'card').props.onDelete();
  hubRender().find(node => node.type === 'dialog').props.onDeleted(receipts[0]);
  assert.equal(hubRender().some(node => node.type === 'dialog'), false);
  const undo = hubRender().find(node => node.type === 'button' && text(node) === 'Undo');
  assert.equal(undo.props['data-usage-control'], 'venture_restore');
  undo.props.onClick(); await new Promise(resolve => setImmediate(resolve));
  assert.ok(calls.includes('/api/ventures/venture-a/restore'));
  assert.equal(hubRender().some(node => node.type === 'button' && text(node) === 'Undo'), false);
});

test('4: the shell owns the only home h1, with no repeat title strip and an intact empty-state start', () => {
  assert.match(source('app/page.tsx'), /<AppShell title="Ventures"/);
  assert.equal((source('components/layout/AppShell.tsx').split('<header className="workspace-header">')[1].split('</header>')[0].match(/<h1>/g) || []).length, 1);
  for (const ventures of [[], [venture]]) {
    const loaded = harness('components/ventures/VentureHub.tsx', {}, [false, ventures, [], 7, false]);
    const rendered = nodes(loaded.render(loaded.exports.VentureHub));
    assert.equal(rendered.some(node => node.type === 'h1'), false);
    assert.equal(rendered.some(node => ['All ventures', 'Your ventures'].includes(text(node))), false);
    if (!ventures.length) assert.ok(rendered.find(node => node.props?.['data-usage-control'] === 'onboarding_start'));
    else assert.ok(rendered.find(node => node.props?.['aria-label'] === '1 venture'));
  }
});

test('10: home portraits use saved ventures and Manage retains production actions and recovery', async () => {
  const calls = [];
  const loaded = harness('components/ventures/VentureHub.tsx', {
    '@/lib/founderUI': { FounderUIContext: { value: 'home' } },
    '@/lib/client': { api: async (url, init) => { calls.push([url, init]); return { ok: true, data: { paused: url.endsWith('/pause') } }; },
      ensureSession: async () => {}, apiSwr: async (url, update) => update({ ok: true, data: { items: [venture] } }) },
  }, [false, [venture], [], 7, false]);
  const render = () => nodes(loaded.render(loaded.exports.VentureHub));
  assert.ok(render().find(node => node.type === 'a' && node.props.href === '/v/venture-a' && text(node) === 'Harbor'));
  assert.equal(render().some(node => node.type === 'card'), false);
  assert.equal(text(render().find(node => node.type === 'h1')), 'Which venture?');
  assert.ok(render().find(node => node.props?.['data-tauri-drag-region'] !== undefined));
  const add = render().find(node => node.props?.['aria-label'] === 'Add venture');
  assert.equal(text(add), '+Add Venture');
  render().find(node => node.type === 'button' && text(node) === 'Manage Ventures').props.onClick();
  assert.equal(render().some(node => node.type === 'card'), false);
  assert.ok(render().find(node => node.props?.className === 'editBadge'));
  assert.equal(text(render().find(node => node.props?.['aria-expanded'] !== undefined && node.props.className.startsWith('manage '))), 'Done');
  const manage = () => render().find(node => node.props?.['aria-label'] === 'Manage Harbor' && node.type === 'button').props.onClick();
  manage();
  assert.equal(text(render().find(node => node.type === 'dialog' && !node.props.venture)), 'HarborPause, resume, open, or delete this venture.NameSave nameOpenPauseDeleteCancel');
  await render().find(node => node.props?.['data-usage-control'] === 'venture_pause').props.onClick();
  assert.equal(calls[0][0], '/api/ventures/venture-a/pause');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), { reason: null });
  manage();
  assert.equal(text(render().find(node => node.props?.['data-usage-control'] === 'venture_resume')), 'Resume');
  await render().find(node => node.props?.['data-usage-control'] === 'venture_resume').props.onClick();
  assert.equal(calls[1][0], '/api/ventures/venture-a/resume');
  manage();
  render().find(node => node.type === 'button' && text(node) === 'Delete').props.onClick();
  assert.ok(render().find(node => node.type === 'dialog' && node.props.venture.id === venture.id));
  render().find(node => node.type === 'dialog').props.onDeleted({ id: venture.id, name: venture.name, retentionDays: 7 });
  assert.ok(render().find(node => node.type === 'button' && text(node) === 'Undo'));
  render().find(node => node.type === 'button' && text(node) === 'Undo').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls[2][0], '/api/ventures/venture-a/restore');
});

test('10: failed management keeps retry and cancel available, native dialog restores focus', async () => {
  let fail = true, focused = 0, shown = 0, closed = 0;
  const previousDocument = global.document;
  global.document = { activeElement: { focus: () => focused++ } };
  try {
    const loaded = harness('components/ventures/VentureHub.tsx', {
      '@/lib/founderUI': { FounderUIContext: { value: 'home' } },
      '@/lib/client': { api: async () => ({ ok: !fail, status: fail ? 503 : 200, data: {} }) },
    }, [false, [venture], [], 7, false]);
    const render = () => nodes(loaded.render(loaded.exports.VentureHub));
    render().find(node => node.type === 'button' && text(node) === 'Manage Ventures').props.onClick();
    render().find(node => node.props?.['aria-label'] === 'Manage Harbor').props.onClick();
    const dialog = render().find(node => node.type === 'dialog');
    dialog.props.ref.current = { showModal: () => shown++, close: () => closed++ };
    const cleanup = loaded.effects.find(effect => effect.toString().includes('showModal'))();
    assert.equal(shown, 1);
    await render().find(node => node.props?.['data-usage-control'] === 'venture_pause').props.onClick();
    assert.ok(render().some(node => node.type === 'error' && node.props.error === 'Couldn’t pause this venture.' && node.props.source === 'VentureHub.cardErrors'));
    assert.equal(render().find(node => node.props?.['data-usage-control'] === 'venture_pause').props.disabled, false);
    assert.ok(render().find(node => node.type === 'button' && text(node) === 'Cancel'));
    fail = false;
    await render().find(node => node.props?.['data-usage-control'] === 'venture_pause').props.onClick();
    assert.equal(render().some(node => node.type === 'dialog'), false);
    cleanup();
    assert.equal(closed, 1); assert.equal(focused, 1);
  } finally { global.document = previousDocument; }
});

test('10: light pixel-card chooser keeps sizing, add circle, overlays, recovery and mobile rule', () => {
  const css = source('components/ventures/VentureHubHome.module.css');
  for (const value of ['--nf-bg: #fff', '--nf-tile: min(112px, 22vw)', 'gap: 24px',
    'padding: 72px 24px 32px', 'height: 52px', 'background: var(--tile-color, #e50914)',
    'box-shadow: inset 0 0 0 4px #808080', 'background: rgba(0, 0, 0, 0.72)', 'width: min(420px, 100%)',
    'right: 18px', 'bottom: 18px', '--nf-tile: min(112px, 38vw)', 'padding-top: 72px']) assert.ok(css.includes(value), value);
  assert.match(css, /\.manage\s*\{[^}]*border-radius: 12px/);
  assert.match(css, /\.managing \.editBadge\s*\{[^}]*opacity: 1/);
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.doesNotMatch(css, /min-height: 400px/);
});

test('venture card selection persists through the authorized API and failed writes preserve the previous card', async () => {
  const requests = [], storage = new Map(), previousStorage = global.localStorage;
  global.localStorage = {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)};
  let fail = true;
  try {
    const loaded = harness('components/ventures/VentureHub.tsx', {
      '@/lib/founderUI': {FounderUIContext: {value: 'home'}},
      '@/lib/client': {
        verifiedSessionSubject: () => 'owned', verifiedSessionEpoch: () => 1,
        api: async (url, options) => {requests.push({url, options}); return {ok: !fail, data: {detail: 'secret internal traceback'}};},
      },
    }, [false, [{...venture, card_type: 'hardware', card_color: 'green', card_art: '3'}], [], 7, false]);
    const render = () => nodes(loaded.render(loaded.exports.VentureHub));
    render().find(node => node.type === 'button' && text(node) === 'Manage Ventures').props.onClick();
    render().find(node => node.props?.['aria-label'] === 'Manage Harbor').props.onClick();
    const select = () => render().find(node => node.props?.['aria-label'] === 'Card 8');
    const click = () => select().props.onClick();
    click(); click(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.length, 1);
    assert.equal(select().props['aria-pressed'], false);
    assert.equal(storage.size, 0);
    const notice = render().find(node => node.type === 'error' && node.props.source === 'VentureHub.cardErrors');
    assert.match(notice.props.error, /Your previous card is unchanged/);
    assert.doesNotMatch(notice.props.error, /secret|traceback/);
    fail = false; click(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(select().props['aria-pressed'], true);
    assert.equal(requests[1].url, '/api/ventures/venture-a/card');
    assert.equal(requests[1].options.method, 'PATCH');
    assert.deepEqual(JSON.parse(requests[1].options.body), {card_type: 'hardware', card_color: 'green', card_art: '8'});
    assert.equal(JSON.parse(storage.get('supymem.venture-card.venture-a')).art, '8');
  } finally {global.localStorage = previousStorage;}
});

test('card choices use server art, reject corrupt local art and survive blocked storage', () => {
  const loaded = harness('components/ventures/ventureCards.ts'), cards = loaded.exports;
  const previousWindow = global.window, previousStorage = global.localStorage;
  global.window = {};
  global.localStorage = {getItem: () => '{"type":"wrong","art":"../../private","color":[]}', setItem() {throw new Error('blocked');}};
  try {
    const choice = cards.choiceFromVenture('venture-a', 'Harbor', '', 'hardware', 'green', '8');
    assert.equal(cards.cardImage(choice), '/venture-cards/8.png');
    assert.equal(cards.loadCardChoice('venture-a', 'Harbor').art, cards.artFor('venture-a'));
    assert.equal(cards.saveCardChoice('venture-a', choice), false);
  } finally {global.window = previousWindow; global.localStorage = previousStorage;}
});

test('venture card manager renames the actual venture and retries without exposing internal errors', async () => {
  for (const [id, name, requested, saved] of [
    ['fleet', 'Fleet Audits', 'fleet checks', 'Fleet Checks'],
    ['garden', 'Garden Notes', 'garden diary', 'Garden Diary'],
  ]) {
    const requests = [];
    const loaded = harness('components/ventures/VentureHub.tsx', {
      '@/lib/founderUI': {FounderUIContext: {value: 'home'}},
      '@/lib/client': {verifiedSessionSubject: () => 'owned', verifiedSessionEpoch: () => 1,
        api: async (url, {method, body}) => {
          requests.push({url, method, body: JSON.parse(body)});
          return requests.length === 1 ? {ok: false, data: {error: 'internal error'}} : {ok: true, data: {id, name: saved}};
        }},
    }, [false, [{...venture, id, name}], [], 7, false]);
    const render = () => nodes(loaded.render(loaded.exports.VentureHub));
    render().find(node => node.type === 'button' && text(node) === 'Manage Ventures').props.onClick();
    render().find(node => node.props?.['aria-label'] === `Manage ${name}`).props.onClick();
    render().find(node => node.type === 'input').props.onChange({target: {value: requested}});
    const save = () => render().find(node => node.type === 'button' && text(node) === 'Save name').props.onClick();
    await save();
    const failed = render();
    assert.match(failed.find(node => node.type === 'error' && node.props.source === 'VentureHub.cardErrors').props.error, /Couldn’t rename/);
    assert.doesNotMatch(JSON.stringify(failed), /internal error/);
    assert.ok(failed.some(node => node.props?.['aria-label'] === `Manage ${name}`));
    await save();
    assert.ok(render().some(node => node.props?.['aria-label'] === `Manage ${saved}`));
    const request = {url: `/api/ventures/${id}`, method: 'PATCH', body: {name: requested}};
    assert.deepEqual(requests, [request, request]);
  }
});

test('Mac U1: many ventures scroll inside the chooser, start at the top, and retain every action', () => {
  const context = { value: 'home' }, ventures = Array.from({ length: 40 }, (_, index) => ({ ...venture, id: `ven_${index}`, name: `Venture ${index}` }));
  const loaded = harness('components/ventures/VentureHub.tsx', {
    '@/lib/founderUI': { FounderUIContext: context },
  }, [false, ventures, [], 7, false]);
  const page = loaded.render(loaded.exports.VentureHub), scroller = { scrollTop: 800 };
  page.props.ref(scroller);
  assert.equal(scroller.scrollTop, 0);
  const rendered = nodes(page);
  assert.equal(rendered.filter(node => node.props?.['aria-label']?.startsWith('Open Venture ')).length, 40);
  assert.ok(rendered.some(node => node.type === 'h1' && text(node) === 'Which venture?'));
  assert.ok(rendered.some(node => node.props?.['aria-label'] === 'Add venture'));
  assert.ok(rendered.some(node => node.type === 'button' && text(node) === 'Manage Ventures'));
  context.value = 'classic';
  assert.equal(loaded.render(loaded.exports.VentureHub).props.ref, undefined);
  const css = source('components/ventures/VentureHubHome.module.css');
  assert.match(css, /\.page \{[^}]*height: 100%;[^}]*min-height: 0;[^}]*overflow-y: auto;[^}]*justify-content: flex-start/);
  assert.match(css, /\.page > \* \{ flex-shrink: 0/);
  const tokens = source('styles/tokens.css');
  assert.match(tokens, /\.home-shell\.app-shell-bare \.workspace \{[^}]*grid-template-rows: minmax\(0, 1fr\)/);
  assert.match(tokens, /\.home-shell\.app-shell-bare \.workspace-body \{[^}]*min-height: 0; overflow: hidden/);
});

test('Mac U5: other-venture notice lives in the sidebar with working navigate/dismiss and a missing-target fallback', () => {
  const previousDocument = global.document, target = {}, actions = [];
  global.document = { getElementById: id => id === 'company-office-notice' ? target : null };
  try {
    const notice = { id: 'notice', title: 'Needs review', body: 'Review the work', target_path: '/v/ven_other/work' };
    const loaded = harness('components/desktop/DesktopNoticeBanner.tsx', {
      'react-dom': { createPortal: (children, target) => ({ type: 'portal', props: { children, target } }) },
      'next/navigation': { usePathname: () => '/v/ven_current/research', useRouter: () => ({ push: href => actions.push(href) }) },
    }, [notice]);
    const props = { ventureId: 'ven_current', otherVentureTarget: 'company-office-notice' };
    let rendered = loaded.render(loaded.exports.DesktopNoticeBanner, props);
    assert.equal(rendered.type, 'portal');
    assert.equal(rendered.props.target, target);
    assert.match(rendered.props.children.props.className, /railOther/);
    nodes(rendered).find(node => node.type === 'button' && text(node) === '1 other venture needs you').props.onClick();
    assert.deepEqual(actions, ['/']);
    assert.equal(loaded.render(loaded.exports.DesktopNoticeBanner, props), null);
    loaded.state[0] = notice;
    rendered = loaded.render(loaded.exports.DesktopNoticeBanner, props);
    nodes(rendered).find(node => node.props?.['aria-label'] === 'Dismiss').props.onClick();
    assert.equal(loaded.render(loaded.exports.DesktopNoticeBanner, props), null);
    loaded.state[0] = notice;
    const missing = { ...props, otherVentureTarget: 'missing' };
    assert.equal(loaded.render(loaded.exports.DesktopNoticeBanner, missing).type, 'aside');
    assert.match(source('components/office/CompanyOffice.tsx'), /aria-label="Your sidebar"[\s\S]*id="company-office-notice"/);
    assert.match(source('components/layout/AppShell.tsx'), /<DesktopNoticeBanner ventureId=\{effectiveVentureId\} otherVentureTarget="company-office-notice"/);
    assert.match(source('components/desktop/DesktopNoticeBanner.module.css'), /\.railOther \{[^}]*flex-wrap: wrap;[^}]*font: 400 12px\/1\.4/);
  } finally { global.document = previousDocument; }
});

test('Mac U4: the configured main webview accepts the first click without changing other windows', () => {
  const config = JSON.parse(readFileSync(path.resolve(root, '../../../src-tauri/tauri.conf.json'), 'utf8'));
  assert.equal(config.app.windows.find(window => window.label === 'main').acceptFirstMouse, true);
  assert.ok(config.app.windows.filter(window => window.label !== 'main').every(window => window.acceptFirstMouse === undefined));
});

test('10: summary requests paint live ventures and deleted records retain restore retry', async () => {
  const requests = [];
  let failRestore = true;
  const deleted = { id: 'ven_deleted', name: 'Orchard', stage: 'archived', days_left: 3 };
  const loaded = harness('components/ventures/VentureHub.tsx', {
    '@/lib/founderUI': { FounderUIContext: { value: 'home' } },
    '@/lib/client': {
      ensureSession: async () => {},
      apiSwr: async (url, update) => {
        requests.push(url);
        update({ ok: true, data: { items: url.includes('/deleted') ? [deleted] : [venture], retention_days: 7 } });
      },
      api: async (url, init) => { requests.push([url, init.method]); return { ok: !failRestore, data: {} }; },
    },
  });
  const render = () => nodes(loaded.render(loaded.exports.VentureHub));
  assert.ok(render().find(node => text(node) === 'Loading your ventures…'));
  loaded.effects[0]();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(requests, ['/api/ventures?summary=true', '/api/ventures/deleted?summary=true']);
  assert.ok(render().find(node => node.props?.href === '/v/venture-a'));
  assert.equal(text(render().find(node => node.type === 'summary')), 'Recently deleted (1)');
  assert.ok(render().find(node => text(node) === '3 days left to restore'));
  render().find(node => node.props?.['data-usage-control'] === 'venture_restore').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(render().find(node => node.type === 'error' && node.props.error === 'Couldn’t restore this venture.' && node.props.source === 'VentureHub.trashError'));
  assert.equal(render().find(node => node.props?.['data-usage-control'] === 'venture_restore').props.disabled, false);
  failRestore = false;
  render().find(node => node.props?.['data-usage-control'] === 'venture_restore').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.filter(request => Array.isArray(request) && request[0] === '/api/ventures/ven_deleted/restore').length, 2);
});

test('11: light switcher retains real venture names, switching, rename and read retry', async () => {
  let fail = true;
  const calls = [], saved = [];
  const loaded = harness('components/office/WorkspaceSwitcher.tsx', {
    '@/lib/founderUI': { FounderUIContext: { value: 'home' } },
    './WorkspaceSwitcher.module.css': { default: new Proxy({}, { get: (_, key) => `light-${key}` }) },
    '@/lib/apiCache': { cacheGet: () => ({ data: venture }) },
    '@/lib/ventureNameRecovery': { startVentureNameRecovery: () => ({ stop() {}, refresh() {} }) },
    '@/lib/ventures': { renameVenture: async (id, name) => { saved.push([id, name]); return { ok: true, data: { name } }; } },
    '@/lib/client': { verifiedSessionSubject: () => 'founder', apiSwr: async (url, update) => {
      calls.push(url); update({ ok: !fail, data: { items: [venture, { id: 'ven_b', name: 'Beacon', stage: 'live' }] } });
    } },
  });
  const previousWindow = global.window;
  global.window = { dispatchEvent() {} };
  try {
    const render = () => nodes(loaded.render(loaded.exports.WorkspaceSwitcher, { currentId: venture.id }));
    assert.equal(render().find(node => node.type === 'button').props.className, 'light-switcher');
    assert.equal(render().some(node => node.props?.className?.includes('portraitTile')), false);
    render().find(node => node.props?.['aria-haspopup'] === 'listbox').props.onClick();
    render();
    loaded.effects[2]();
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(render().find(node => node.type === 'button' && text(node) === 'Try again'));
    assert.ok(render().find(node => node.type === 'error' && node.props.source === 'WorkspaceSwitcher.listError' && node.props.error === 'Couldn’t load your ventures.'));
    fail = false;
    await render().find(node => node.type === 'button' && text(node) === 'Try again').props.onClick();
    const item = render().find(node => node.props?.href === '/v/ven_b');
    assert.equal(text(item), 'BeaconOpen venture');
    item.props.onClick();
    assert.equal(render().some(node => node.props?.role === 'listbox'), false);
    render().find(node => node.props?.['aria-label'] === 'Rename venture').props.onClick();
    render().find(node => node.type === 'input').props.onChange({ target: { value: 'Harbor Works' } });
    render().find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(saved, [['venture-a', 'Harbor Works']]);
    assert.ok(render().find(node => node.props?.title === 'Harbor Works'));
    assert.deepEqual(calls, ['/api/ventures', '/api/ventures']);
  } finally { global.window = previousWindow; }
  const css = source('components/office/WorkspaceSwitcher.module.css');
  for (const value of ['gap: 10px', 'padding: 8px 10px', 'border: 1px solid var(--line)', 'color: var(--fg-0)',
    'background: var(--bg-3)', 'font: 600 9px/1.2 var(--font-sans)', 'font: 600 13px/1.3 var(--font-sans)',
    'top: calc(100% + 6px)', 'border-radius: 12px', 'background: var(--bg-card)']) assert.ok(css.includes(value), value);
});

test('5: blocker steps are URL-free and readable; primary review, secondary dismiss, long-list collapse', () => {
  for (const body of ['Connect your account.', 'Next: 1) Connect Gmail (/v/venture-a/connect/gmail) 2) Review evidence (https://example.com/private) 3) Approve scope', '1) One 2) Two 3) Three 4) Four']) {
    const routes = [], notice = { id: 'notice-a', title: 'Harbor needs you', body, target_path: '/v/ven_a/now' };
    const loaded = harness('components/desktop/DesktopNoticeBanner.tsx', {
      'next/navigation': { usePathname: () => '/v/ven_a', useRouter: () => ({ push: href => routes.push(href) }) },
    }, [notice]);
    let rendered = nodes(loaded.render(loaded.exports.DesktopNoticeBanner, { ventureId: 'ven_a' }));
    assert.equal(text(rendered[0]).includes('example.com'), false); assert.equal(text(rendered[0]).includes('/connect/gmail'), false);
    const count = (body.match(/\d+\)/g) || []).length;
    assert.equal(rendered.filter(node => node.type === 'li').length, count);
    assert.equal(rendered.some(node => node.type === 'details'), count > 3);
    if (count > 3) assert.ok(rendered.find(node => node.props?.['data-usage-control'] === 'desktop_notice_steps'));
    const review = rendered.find(node => node.type === 'button' && text(node) === 'Open review');
    const dismiss = rendered.find(node => node.type === 'button' && text(node) === 'Dismiss');
    assert.equal(review.props.className, 'primary sm'); assert.equal(dismiss.props.className, 'ghost sm');
    review.props.onClick(); assert.deepEqual(routes, ['/v/ven_a/now']); assert.equal(loaded.state[0], null);
    loaded.state[0] = notice; rendered = nodes(loaded.render(loaded.exports.DesktopNoticeBanner, { ventureId: 'ven_a' }));
    rendered.find(node => node.type === 'button' && text(node) === 'Dismiss').props.onClick(); assert.equal(loaded.state[0], null);
  }
});

test('6: cards and header share truthful teammate copy and cards do not repeat the roster', () => {
  const loaded = harness('components/ventures/VentureCard.tsx');
  const rendered = nodes(loaded.render(loaded.exports.VentureCard, { venture: { ...venture, status: { detail: '2 of 4 teammates working' } }, busy: false, error: '' }));
  assert.equal(rendered.filter(node => node.type === 'p' && text(node) === '2 of 4 teammates working').length, 1);
  assert.doesNotMatch(text(rendered[0]), /hired/);
  assert.match(source('components/layout/AppShell.tsx'), /\{teamCountLabel\(hired, working\)\}/);
});

test('7: home has no empty notice row; venture space is reserved and actual notices stay visible', () => {
  for (const [pathname, ventureId, reserved] of [['/', undefined, false], ['/connections', undefined, false], ['/new', undefined, true], ['/v/venture-a', 'venture-a', true]]) {
    const loaded = harness('components/desktop/DesktopNoticeBanner.tsx', { 'next/navigation': { usePathname: () => pathname, useRouter: () => ({ push() {} }) } });
    const rendered = loaded.render(loaded.exports.DesktopNoticeBanner, { ventureId });
    assert.equal(!!rendered, reserved);
    if (reserved) assert.equal(rendered.props['aria-hidden'], 'true');
    loaded.state[0] = { title: 'Review needed', body: 'Please review.', target_path: '/', id: 'notice-a' };
    assert.ok(nodes(loaded.render(loaded.exports.DesktopNoticeBanner, { ventureId })).find(node => node.props?.role === 'status'));
  }
  assert.match(source('styles/tokens.css'), /\.workspace-header\s*\{[^}]*min-height: 48px;\s*padding: var\(--s-2\) var\(--s-5\)/);
  assert.match(source('components/ventures/VentureHub.module.css'), /\.page\s*\{[^}]*padding: var\(--s-5\) var\(--s-5\)/);
});

test('8: hub shares the header left inset at desktop and compact widths, without a centered dead column', () => {
  const css = source('components/ventures/VentureHub.module.css');
  assert.match(css, /\.page\s*\{[^}]*width: min\(var\(--w-content-max\), 100%\);\s*margin: 0;/);
  assert.match(css, /@media \(max-width: 720px\)\s*\{\s*\.page\s*\{\s*padding: var\(--s-5\) var\(--s-5\)/);
  assert.match(source('styles/tokens.css'), /\.workspace-header\s*\{[^}]*padding: var\(--s-2\) var\(--s-5\)/);
});

test('11, 12, 20, 21: card depth is quiet, every control presses, and paused text stays readable', () => {
  const card = source('components/ventures/VentureCard.module.css'), tokens = source('styles/tokens.css');
  assert.match(card, /\.card:has\(\.body:hover\),\s*\.card:focus-within\s*\{[^}]*var\(--line-strong\)[^}]*var\(--sh-hover\)/);
  assert.doesNotMatch(card, /\.card:hover|opacity: 0\.78|var\(--sh-2\)/);
  assert.match(card, /--press-scale: 0\.99/);
  assert.match(tokens, /--d-press: 100ms/);
  assert.match(tokens, /:where\(button, a\[class\], summary, \[role="button"\]\):active:not\(:disabled\):not\(\[aria-disabled="true"\]\)\s*\{[^}]*transform: scale\(var\(--press-scale\)\);[^}]*opacity: 0\.88/);
  assert.match(tokens, /prefers-reduced-motion: reduce[\s\S]*transition-property: opacity, background-color, color, border-color, visibility !important/);
  assert.match(tokens, /transform: none; opacity: 0\.88/);
  assert.match(tokens, /prefers-contrast: more[\s\S]*--line-strong:[\s\S]*--fg-2: var\(--fg-1\)/);
});

test('13, 14, 22, 23: home keeps its typography tokens without fetching Google Fonts', () => {
  const tokens = source('styles/tokens.css');
  assert.doesNotMatch(tokens, /@import.*fonts\.googleapis/);
  assert.match(tokens, /--font-sans: "DM Sans"/);
  assert.match(tokens, /--t-display: 600 28px\/1\.2 var\(--font-logo\)/);
  assert.match(tokens, /--font-sans: -apple-system, BlinkMacSystemFont, system-ui/);
  assert.match(tokens, /--t-display: 700 1\.625rem\/1\.15 var\(--font-sans\)/);
  assert.match(tokens, /--t-label: 600 0\.6875rem\/1\.3/);
  assert.match(tokens, /html:has\(\.home-shell\), \.home-shell \{/);
  assert.match(tokens, /html \{ font-size: 100%; \}\s*body \{ font: var\(--t-body\); \}/);
  assert.match(tokens, /\.page-heading h1,[^}]*letter-spacing: -0\.01em/);
  assert.match(source('components/ventures/VentureCard.module.css'), /letter-spacing: -0\.01em/);
  for (const file of ['components/ventures/VentureHub.module.css', 'components/layout/RunStatusBar.module.css', 'components/layout/NextStepStrip.module.css', 'components/chat/VentureChatLauncher.module.css', 'components/desktop/DesktopNoticeBanner.module.css']) {
    assert.match(source(file), /border-radius: var\(--r-control\)/, file);
  }
  for (const file of ['components/guide/CapabilityGuide.module.css', 'components/office/office.module.css', ...['site/site', 'org/team', 'tasks/tasks', 'research/research'].map(name => `app/v/[ventureId]/${name}.module.css`)]) {
    assert.doesNotMatch(source(file), /var\(--font-logo\)/, file);
  }
});

test('15, 16: focus is visible and chat stays mounted, inert while closed, and reverses without workspace reflow', () => {
  const chat = source('components/chat/ChatDock.module.css'), tokens = source('styles/tokens.css'), shell = source('components/layout/AppShell.tsx');
  assert.match(chat, /\.composerBox:focus-within\s*\{[^}]*border-color: var\(--accent\);[^}]*box-shadow:/);
  // Focus shows on the whole composer, never as a second outline inside it.
  assert.match(chat, /\.composerBox:focus-within\s*\{[^}]*border-color: var\(--accent\)/);
  assert.match(chat, /textarea:focus-visible,\s*\.composerBox input:focus-visible\s*\{\s*outline: none;/);
  assert.match(tokens, /:where\(a, button, input, textarea, select, summary, \[tabindex\], \[role="button"\]\):focus-visible/);
  assert.match(shell, /className="chat-panel" hidden=\{!chatPanelOpen\} inert=\{!chatPanelOpen\}/);
  assert.match(shell, /setChatPanelOpen\(\(open\) => !open\)/);
  assert.match(shell, /chatToggleRef\.current\?\.focus\(\)/);
  assert.match(tokens, /\.app-shell\.home-shell\.chat-open/);
  assert.match(tokens, /\.home-shell\.chat-open \.workspace \{ display: none/);
  assert.match(tokens, /\.chat-panel\s*\{[^}]*position: absolute;[^}]*transition: transform var\(--d-slow\) var\(--e-out\), opacity[^}]*display var\(--d-slow\) allow-discrete/);
  assert.match(tokens, /\.chat-panel\[hidden\]\s*\{[^}]*translateX\(16px\)/);
  assert.match(tokens, /@starting-style\s*\{ \.chat-panel \{ opacity: 0; transform: translateX\(16px\)/);
  assert.match(source('components/chat/ChatDock.tsx'), /if \(!resizable \|\| !autoFocus \|\| hideComposer \|\| !visible\) return/);
  assert.match(source('components/chat/VentureChatLauncher.tsx'), /visible=\{open\}/);
});

test('18, 24, 25, 28: rail keeps venture identity and pin; global navigation drops duplicate headings and Beta badges', () => {
  const nav = harness('components/layout/SideNav.tsx');
  const rendered = nodes(nav.render(nav.exports.SideNav, { pathname: '/' }));
  const group = rendered.find(node => node.type === 'nav');
  assert.equal(group.props['aria-label'], 'Ventures');
  assert.equal(nodes(group).filter(node => typeof node.type === 'function').length, 4);
  assert.doesNotMatch(source('components/layout/SideNav.tsx'), /styles\.beta|id: "ventures", label: "Ventures", items/);
  const css = source('components/office/office.module.css'), shell = source('components/layout/AppShell.tsx');
  assert.match(source('components/office/WorkspaceSwitcher.tsx'), /title=\{`Switch venture: \$\{label\}`\}/);
  assert.match(css, /\.switcherCopy,[\s\S]*\.switcherChevron,[\s\S]*\.switcherRename,[\s\S]*display: none/);
  assert.doesNotMatch(source('styles/tokens.css'), /\.sidebar-brand,[^}]*display: none/);
  assert.doesNotMatch(shell, /[◀▶]/);
  assert.match(shell, /<NAV_ICONS\.sidebar \/>/);
  assert.match(source('components/layout/SideNav.module.css'), /\.chevron\s*\{[^}]*transition: transform var\(--d-fast\) var\(--e-out\)/);
});

test('19, 26: status motion is finite and in-progress-only; translucent chrome has solid preference fallbacks', () => {
  const status = source('components/layout/RunStatusBar.module.css');
  assert.doesNotMatch(status, /infinite|pulse|activitySweep|#[0-9a-f]{3,8}\b/);
  assert.match(status, /\.bar\[data-working="true"\]::after[^}]*animation: activityStart var\(--d-slow\) var\(--e-out\)/);
  assert.match(source('components/layout/RunStatusBar.tsx'), /<circle cx="8" cy="8" r="3"/);
  assert.match(source('styles/tokens.css'), /prefers-reduced-transparency: reduce[^}]*backdrop-filter: none !important/);
  assert.match(source('components/suggestions/suggestions.module.css'), /prefers-reduced-transparency: reduce[\s\S]*background: var\(--sidebar\); border: 1px solid var\(--sidebar-text\)/);
});

test('27: setup toast enters and exits on one path using shared surface tokens, and dismissal hides its controls', () => {
  const loaded = harness('components/layout/AttentionToast.tsx'), props = { ventureId: 'venture-a' };
  assert.equal(loaded.render(loaded.exports.AttentionToast, props).props.hidden, true);
  let listener;
  const previousWindow = global.window;
  global.window = { addEventListener: (_, handler) => { listener = handler; }, removeEventListener() {} };
  try {
    loaded.effects[0]();
    listener({ detail: { kind: 'untrusted' } });
    assert.equal(loaded.render(loaded.exports.AttentionToast, props).props.hidden, true);
    listener({ detail: { kind: 'provider_connection' } });
    const toast = loaded.render(loaded.exports.AttentionToast, props);
    assert.equal(toast.props.hidden, false); assert.equal(toast.props.inert, false);
    assert.equal(nodes(toast).find(node => node.type === 'a').props.href, '/v/venture-a/connections');
    nodes(toast).find(node => node.type === 'button').props.onClick();
    assert.equal(loaded.render(loaded.exports.AttentionToast, props).props.inert, true);
  } finally { global.window = previousWindow; }
  const toast = source('components/layout/AttentionToast.module.css'), dialog = source('components/ventures/DeleteVentureDialog.module.css');
  for (const token of ['--bg-1', '--fg-0', '--line', '--sh-2', '--z-toast']) assert.ok(toast.includes(`var(${token})`));
  assert.match(toast, /display var\(--d-base\) allow-discrete/);
  assert.match(dialog, /@starting-style[\s\S]*transform: scale\(0\.98\)/);
  assert.match(dialog, /transform-origin: var\(--dialog-source-x/);
  assert.match(dialog, /\.overlay\[data-closing="true"\][^}]*opacity: 0/);
});

test('27: dialog Escape retains its focus trap during entry and restores the source after the mirrored exit', () => {
  let closed = 0;
  const loaded = harness('components/ventures/DeleteVentureDialog.tsx'), props = { venture, retentionDays: 7, onClose: () => closed++, onDeleted() {} };
  let rendered = nodes(loaded.render(loaded.exports.DeleteVentureDialog, props));
  rendered.find(node => node.props?.role === 'dialog').props.onKeyDown({ key: 'Escape', preventDefault() {} });
  assert.equal(closed, 0);
  rendered = nodes(loaded.render(loaded.exports.DeleteVentureDialog, props));
  const overlay = rendered.find(node => node.props?.['data-closing']);
  assert.equal(overlay.props.inert, true);
  overlay.props.onTransitionEnd({ target: 'child', currentTarget: 'overlay', propertyName: 'opacity' });
  assert.equal(closed, 0);
  overlay.props.onTransitionEnd({ target: 'overlay', currentTarget: 'overlay', propertyName: 'opacity' });
  assert.equal(closed, 1);
  overlay.props.onTransitionEnd({ target: 'overlay', currentTarget: 'overlay', propertyName: 'opacity' });
  assert.equal(closed, 1);
});
