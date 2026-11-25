const assert = require('node:assert/strict'), test = require('node:test'), ts = require('typescript');
const { readFileSync, statSync } = require('node:fs'), Module = require('node:module'), path = require('node:path');
const root = path.resolve(__dirname, '../..'), jsx = (type, props) => ['ChatMessageText', 'ChatWorking'].includes(type?.name) ? type(props) : ({ type, props });
const nodes = n => !n || typeof n !== 'object' ? [] : [n, ...[n.props?.children].flat(8).flatMap(nodes)];
const label = n => !n || typeof n !== 'object' ? '' : [n.props?.children].flat(8).map(x => typeof x === 'string' || typeof x === 'number' ? String(x) : label(x)).join('');
function harness(file, extra = {}, initial = [], expose = '') {
  const state = [...initial], effects = [], layouts = [], cache = new Map(); let cursor = 0;
  const react = { useState(value) { const i = cursor++; if (!(i in state)) state[i] = typeof value === 'function' ? value() : value; return [state[i], next => { state[i] = typeof next === 'function' ? next(state[i]) : next; }]; },
    useRef(value) { const i = cursor++; if (!(i in state)) state[i] = { current: value }; return state[i]; }, useMemo: create => create(), useEffect(fn) { effects.push(fn); }, useLayoutEffect(fn) { layouts.push(fn); }, useCallback: fn => fn, useContext: context => context.value, createContext: value => ({ value }) };
  const mocks = { react, 'react/jsx-runtime': { jsx, jsxs: jsx }, 'next/link': { default: 'a' }, '@/components/chat/ChatLink': { default: 'a', ChatAnchor: 'a', captureChatLink() {} }, 'next/navigation': { usePathname: () => '/new', useRouter: () => ({replace() {}, push() {}}) },
    '@/lib/founderUI': { FounderUIContext: { value: 'classic' } },
    '@/components/layout/LocalModeBoundary': { useLocalMode: () => false, useFeatureVisibility: () => () => true },
    '@/lib/charlieMascot': { setCharlieMascot() {}, getCharlieMascotState: () => ({state: 'idle', level: null}) },
    '@/components/settings/useInboxBriefing': { useInboxBriefing: () => ({ enabled: false }) },
    '@/lib/client': { api: async () => ({ok: true, data: {items: []}}), verifiedSessionEpoch: () => 1, verifiedSessionSubject: () => 'owned' },
    '@/components/office/OfficeProvider': { useOfficeOptional: () => null }, '@/components/office/avatars': { EmployeePortrait: 'portrait', parseEmployeeStamp: text => ({ text }) },
    '@/lib/useComputerUseActive': { useComputerUseActive: () => false }, '@/components/primitives/ErrorNotice': { ErrorNotice: 'error' }, '@/components/desktop/EmergencyStopButton': { EmergencyStopButton: 'stop' },
    './MentionField': { MentionField: 'textarea' }, './MacTasksEntry': { MacTasksEntry: 'mac' }, './VoiceMic': { VoiceMic: 'voice' }, './DictateButton': { DictateButton: 'dictate' }, './RecoveryResultForm': {}, './ChatOutreachReview': { ChatOutreachReview: 'sender-review' },
    '@/components/chat/IntakeAttachments': { IntakeAttachments: 'attachments', useIntakeAttachments: () => ({files: [], evidenceIds: [], uploading: false, restore() {}}) },
    '@/components/chat/useChatHistory': {useChatHistory() {}}, ...extra };
  mocks['@/lib/client'].openIdeaSession ??= ventureId => mocks['@/lib/client'].api(`/api/ventures/${ventureId}/idea/sessions`, { method: 'POST' });
  mocks['@/lib/client'].apiLive ??= (...args) => mocks['@/lib/client'].api(...args);
  function load(filename) {
    if (cache.has(filename)) return cache.get(filename).exports;
    const m = new Module(filename, module); cache.set(filename, m); m.filename = filename; m.paths = Module._nodeModulePaths(path.dirname(filename)); const native = m.require.bind(m);
    m.require = id => { if (id in mocks) return mocks[id]; if (id.endsWith('.css')) return { default: new Proxy({}, { get: (_, key) => key }) };
      if (id.startsWith('@/') || id.startsWith('.')) { const base = id.startsWith('@/') ? path.join(root, id.slice(2)) : path.resolve(path.dirname(filename), id);
        const found = [base, `${base}.ts`, `${base}.tsx`].find(f => { try { return statSync(f).isFile(); } catch { return false; } }); if (found) return load(found); } return native(id); };
    m._compile(ts.transpileModule(readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText + (filename.endsWith(file) ? expose : ''), filename); return m.exports;
  }
  return { exports: load(path.resolve(root, file)), effects, layouts, state, render(fn, props) { cursor = 0; effects.length = 0; layouts.length = 0; return fn(props); } };
}
function setup() { global.document = { addEventListener() {}, removeEventListener() {} }; global.window = { addEventListener() {}, removeEventListener() {}, setInterval: () => 1, clearInterval() {}, setTimeout: () => 1, clearTimeout() {}, matchMedia: () => ({ matches: false }), location: { pathname: '/new', search: '', href: 'http://localhost/new' } }; global.requestAnimationFrame = fn => { fn(); return 1; }; global.cancelAnimationFrame = () => {}; }

test('entry dictation uses a draft icon rather than the live microphone', () => {
  for (const value of ['A repair log for rail operators', 'A seed exchange for home gardeners']) {
    setup();
    window.webkitSpeechRecognition = function () {};
    const view = harness('components/chat/DictateButton.tsx', { '@/lib/desktop': { isDesktopShell: () => false } });
    const render = () => nodes(view.render(view.exports.DictateButton, { value, onChange() {} }));
    render(); view.effects[0]();
    const button = render().find(node => node.type === 'button');
    assert.equal(button.props['aria-label'], 'Dictate');
    assert.equal(nodes(button).find(node => node.type === 'svg').props['aria-hidden'], 'true');
    assert.ok(nodes(button).some(node => node.type === 'path' && node.props.d.includes('h10')));
    assert.equal(nodes(button).some(node => node.type === 'rect'), false);
  }
});

test('answered entry questions leave each answer only in your bubble after history reload across domains', async () => {
  for (const [idea, answer, audience] of [
    ['A tool for fleet operators', 'It compares fuel receipts against vehicle mileage.', 'Independent fleet managers'],
    ['An app for home gardeners', 'It converts balcony measurements into planting layouts.', 'Apartment gardeners'],
  ]) {
    setup();
    const items = [
      { text: idea, proposal: { id: 'task', state: 'clarify', route: 'clarify', message: 'Describe one task.', choices: ['A tool', 'A service'] } },
      { text: `${idea}\nClarification: ${answer}`, proposal: { id: 'audience', state: 'clarify', route: 'clarify', message: 'Who is it for?', choices: [audience] } },
      { text: `${idea}\nClarification: ${answer}\nAudience: ${audience}`, proposal: { id: 'ready', state: 'pending', route: 'startup', message: 'What should we call it?' } },
    ];
    const page = harness('app/new/page.tsx', {
      '@/components/layout/AppShell': { AppShell: 'shell' }, './ConfirmedConversation': {},
      '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': { DictateButton: 'dictate' },
      '@/lib/client': { verifiedSessionSubject: () => 'owned', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
        api: async () => ({ ok: true, data: { items } }) },
    }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
    const render = () => nodes(page.render(page.exports.Conversation, {}));
    render(); await page.effects[2](); await new Promise(resolve => setImmediate(resolve));
    const paragraphs = render().filter(node => node.type === 'p').map(label);
    assert.equal(paragraphs.filter(text => text.includes(answer)).length, 1);
    assert.equal(paragraphs.filter(text => text.includes(audience)).length, 1);
    assert.equal(paragraphs.filter(text => text === 'Answered').length, 2);
    assert.equal(paragraphs.some(text => /You answered:/.test(text)), false);
  }
});

test('one entry option click submits once for task, audience and plain clarification across domains', async () => {
  for (const [idea, option] of [
    ['A tool for fleet operators', 'Compare fuel receipts against recorded vehicle mileage'],
    ['An app for home gardeners', 'Convert balcony measurements into seasonal planting layouts'],
  ]) for (const field of ['offer', 'customer', undefined]) {
    setup(); const requests = [];
    const page = harness('app/new/page.tsx', {
      '@/components/layout/AppShell': { AppShell: 'shell' }, './ConfirmedConversation': {},
      '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': { DictateButton: 'dictate' },
      '@/lib/client': { verifiedSessionSubject: () => 'owned', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
        api: async (url, options) => {
          if (url.endsWith('/history')) return { ok: true, data: { items: [] } };
          requests.push(JSON.parse(options.body));
          return { ok: true, data: requests.length === 1 ? { status: 'clarify', id: 'question', question: 'Which starting point?', choices: [option],
            ...(field ? { onboarding_field: field, onboarding_question: 'Which starting point?', onboarding_choices: [option] } : {}) }
            : { status: 'proposal', proposal: { id: 'ready', state: 'pending', route: 'startup', message: 'What should we call it?' } } };
        } },
    }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
    const render = () => nodes(page.render(page.exports.Conversation, {}));
    render(); await page.effects[2](); await new Promise(resolve => setImmediate(resolve));
    render().find(node => node.props?.id === 'entry-message').props.onChange({ target: { value: idea } });
    render().find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
    await new Promise(resolve => setImmediate(resolve));
    render().find(node => node.props?.id === 'entry-message').props.onChange({ target: { value: 'Start with a weekly summary' } });
    const pick = render().find(node => node.props?.['data-usage-control'] === 'onboarding_choice');
    pick.props.onClick(); pick.props.onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.length, 2);
    assert.equal(requests[1].text, `${idea}\n${field === 'customer' ? 'Audience' : 'Clarification'}: Assumed: ${option}\nStart with a weekly summary`);
    assert.ok(render().some(node => node.props?.['data-usage-control'] === 'onboarding_name'));
  }
});

test('entry picks display complete option text without an assumption prefix across domains', async () => {
  for (const [idea, option] of [
    ['A tool for fleet operators', 'Independent logistics operators reconciling international freight invoices and managing regional delivery schedules'],
    ['An app for home gardeners', 'Neighbourhood gardeners exchanging seasonal planting notes and sharing accessible balcony designs with nearby families'],
  ]) {
    setup();
    const items = [
      { text: idea, proposal: { id: 'question', state: 'clarify', route: 'clarify', message: 'Who is it for?', choices: [option] } },
      { text: `${idea}\nAudience: Assumed: ${option}`, proposal: { id: 'ready', state: 'pending', route: 'startup', message: 'What should we call it?' } },
    ];
    const page = harness('app/new/page.tsx', {
      '@/components/layout/AppShell': { AppShell: 'shell' }, './ConfirmedConversation': {},
      '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': { DictateButton: 'dictate' },
      '@/lib/client': { verifiedSessionSubject: () => 'owned', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
        api: async () => ({ ok: true, data: { items } }) },
    }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
    const render = () => nodes(page.render(page.exports.Conversation, {}));
    render(); await page.effects[2](); await new Promise(resolve => setImmediate(resolve));
    const prose = render().filter(node => node.type === 'p').map(label);
    assert.ok(prose.includes(option));
    assert.equal(prose.some(text => text.includes('Assumed:')), false);
    assert.equal(items[1].text, `${idea}\nAudience: Assumed: ${option}`);
  }
});

test('empty chat explains Charlie and offers one first action and your guide', () => {
  for (const ventureId of ['ven_freight', 'ven_hiking']) {
    setup(); const calls = [];
    const chatHarness = harness('components/chat/ChatDock.tsx');
    const tree = chatHarness.render(chatHarness.exports.ChatDock, { messages: [], ventureId, onSend() {}, onAction: (...args) => calls.push(args) });
    const all = nodes(tree), start = all.find(node => node.type === 'button' && label(node) === 'Find my first customers');
    assert.ok(start); start.props.onClick();
    assert.deepEqual(calls, [['chat.reply', { text: 'Find my first customers' }]]);
    assert.ok(all.some(node => node.type === 'a' && node.props.href === `/v/${ventureId}/guide` && label(node) === 'How to use Supymem'));
    assert.ok(label(tree).includes('Your conversation with Charlie starts here.'));
    assert.ok(!label(tree).includes('Try @ a teammate'));
    const routes = harness('lib/chatRouteAction.ts').exports, visibility = harness('lib/chatVisibility.ts').exports;
    for (const href of [`/v/${ventureId}/guide`, `/guide?venture=${ventureId}`]) {
      assert.equal(routes.inChatActions([{ label: 'How to use Supymem', href }])[0].href, href);
      assert.equal(routes.homeRouteAction(href, ventureId), null);
      assert.equal(visibility.chatFirstHidden(href), false);
    }
  }
});

test('How to use Supymem prompts open new conversations as drafts and never replace saved writing', () => {
  const guide = require('../../lib/capabilityGuide.ts');
  const items = [...guide.GUIDE_QUICK_START, ...guide.GUIDE_USE_CASES, ...guide.GUIDE_TIPS];
  for (const item of [...items, {id: 'forged-prompt', prompt: ''}]) for (const saved of ['', 'My own unsent idea']) {
    setup();
    window.location.search = `?guide_prompt=${item.id}`;
    window.localStorage = {getItem: () => saved, setItem() {}, removeItem() {}};
    const page = harness('app/new/page.tsx', {
      '@/components/layout/AppShell': {AppShell: 'shell'}, './ConfirmedConversation': {},
      '@/components/ventures/BetaPathStrip': {}, '@/lib/detailLevel': {useDetailLevel: () => ['brief']},
      '@/components/chat/VoiceMic': {VoiceMic: 'voice'}, '@/components/chat/DictateButton': {DictateButton: 'dictate'},
    }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
    const tree = nodes(page.render(page.exports.Conversation, {}));
    assert.equal(tree.find(node => node.props?.id === 'entry-message').props.value, saved || item.prompt);
  }
});

test('earlier new-venture history displays each founder answer separately from its cumulative intake context', async () => {
  for (const [idea, answer] of [['Reconcile supplier invoices for manufacturers', 'Finance teams review disputed charges'],
    ['Help families plan accessible holidays', 'Parents choose activities for their children']]) {
    setup();
    const proposal = (id, state) => ({id, state, route: 'startup', message: 'Saved', next_step: 'idea', skipped_steps: [], expires_at: ''});
    const items = [{text: idea, proposal: proposal('first', 'clarify')},
      {text: `${idea}\nClarification: ${answer}`, proposal: proposal('second', 'committed')}];
    const page = harness('app/new/page.tsx', {
      '@/components/layout/AppShell': {AppShell: 'shell'}, './ConfirmedConversation': {},
      '@/components/ventures/BetaPathStrip': {}, '@/lib/detailLevel': {useDetailLevel: () => ['brief']},
      '@/components/chat/VoiceMic': {VoiceMic: 'voice'}, '@/components/chat/DictateButton': {DictateButton: 'dictate'},
      '@/lib/client': {verifiedSessionSubject: () => 'owned', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ok: true}),
        api: async () => ({ok: true, data: {items}})},
    }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
    const render = () => nodes(page.render(page.exports.Conversation, {}));
    render(); for (const effect of page.effects) effect(); await new Promise(resolve => setImmediate(resolve));
    let all = render(); all.find(node => node.type === 'button' && label(node) === 'Show earlier conversations').props.onClick();
    all = render();
    const prose = all.filter(node => node.type === 'p' && node.props?.className === 'msgText').map(label);
    assert.ok(prose.includes(idea)); assert.ok(prose.includes(answer));
    assert.equal(prose.some(text => text.includes(idea) && text.includes(answer)), false);
  }
});

test('new composer keeps attachments in a menu and voice beside a full-size Continue at narrow and wide widths', () => {
  for (const idea of ['A compliance board for manufacturing teams', 'A dinner planner for busy parents']) {
    setup();
    const page = harness('app/new/page.tsx', {
      '@/lib/founderUI': { FounderUIContext: { value: 'home' } },
      '@/components/layout/AppShell': { AppShell: 'shell' }, './ConfirmedConversation': {},
      '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': { DictateButton: 'dictate' },
    }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
    let all = nodes(page.render(page.exports.Conversation, {}));
    all.find(node => node.props?.id === 'entry-message').props.onChange({target: {value: idea}});
    all = nodes(page.render(page.exports.Conversation, {}));
    const row = all.find(node => node.props?.className === 'composerActions');
    const children = row.props.children.flat().filter(Boolean);
    const groups = children.filter(node => node.props?.className === 'composerTools');
    assert.equal(groups.length, 2);
    const tools = groups[0];
    assert.ok(nodes(tools).some(node => node.type === 'voice'));
    const menu = nodes(tools).find(node => node.type === 'details');
    assert.equal(menu.props.className, 'attachmentMenu');
    assert.ok(nodes(menu).some(node => node.type === 'summary' && node.props['aria-label'] === 'Attach files or folder'));
    assert.ok(nodes(menu).some(node => node.props?.className === 'attachmentPopover'));
    assert.ok(nodes(groups[1]).some(node => node.type === 'dictate'));
    assert.ok(nodes(groups[1]).some(node => node.props?.['data-usage-control'] === 'onboarding_continue'));
    assert.equal(nodes(tools).some(node => node.props?.['data-usage-control'] === 'onboarding_continue'), false);
    for (const width of [320, 1440]) {
      const css = readFileSync(path.join(__dirname, 'ChatDock.module.css'), 'utf8');
      assert.match(css, /\.homePanel \.composerActions\s*\{[^}]*min-width: 0;[^}]*flex-wrap: wrap/, `${idea} at ${width}`);
      assert.match(css, /\.homePanel form\.composerBox \.composerActions\s*\{[^}]*flex-basis: 100%; width: 100%/);
      assert.match(css, /button\[data-usage-control="onboarding_continue"\]\s*\{[^}]*white-space: normal/);
      assert.match(css, /\.homePanel \.composerTools button:not\(\[data-usage-control="onboarding_continue"\]\)\s*\{[^}]*width: var\(--im-inline, 28px\)/);
      assert.match(css, /\.homePanel \.composerTools \.attachmentPopover button\s*\{[^}]*width: auto;[^}]*min-height: 0/);
      assert.match(readFileSync(path.join(__dirname, 'VoiceMic.module.css'), 'utf8'), /\.wrap\s*\{[^}]*flex-wrap: wrap/);
    }
    assert.match(readFileSync(path.join(__dirname, 'AttachFileButton.tsx'), 'utf8'), /<span>Attach files<\/span>/);
  }
});

test('tagged choices show Send in the card and accept selections with or without optional details across unrelated audiences', async () => {
  for (const [audience, description] of [['freight dispatchers', 'A driver missed the loading slot on Tuesday.'], ['weekend hikers', 'My friend found a closed trail on Saturday.']]) {
    for (const useAction of [true, false]) {
    for (const detail of ['', description]) {
    setup(); const sent = [], composer = { draft: '', pending: false, setDraft(next) { composer.draft = next; }, clearError() {},
      async submit(text) { sent.push({action: 'chat.reply', payload: {text}}); return true; } };
    const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer },
      '@/lib/founderUI': { FounderUIContext: { value: 'home' } } });
    const prompt = 'Describe that moment: who, when, what happened?', option = 'A concrete incident to share';
    const props = { variant: 'panel', messages: [{ id: 'question', role: 'assistant', body_md: `What makes it urgent for ${audience}?`, actions: [
      { label: option, text: option, action: 'chat.reply', payload: { question_id: 'turn', requires_detail: true, detail_prompt: prompt } }]}],
      onSend() {}, ...(useAction ? {onAction: async (action, payload) => { sent.push({ action, payload }); return true; }} : {}) };
    const render = () => nodes(h.render(h.exports.ChatDock, props));
    let focused = 0;
    render().find(node => node.props?.inputRef).props.inputRef.current = {focus() { focused++; }};
    render().find(node => node.props?.['data-usage-control'] === 'chat_question_detail').props.onClick();
    let all = render();
    assert.equal(focused, 1);
    const nextStep = `Optional: ${prompt}`;
    assert.ok(all.some(node => node.props?.role === 'status' && label(node) === nextStep && node.props.className === 'optionsHint'));
    const emptySend = all.find(node => node.props?.['data-usage-control'] === 'chat_options_submit');
    assert.equal(emptySend.props.disabled, false);
    assert.equal(label(emptySend), 'Send');
    all.find(node => node.props?.inputRef).props.onChange(detail); all = render();
    assert.equal(all.some(node => node.props?.role === 'alert'), false);
    assert.equal(all.find(node => node.props?.['data-usage-control'] === 'chat_question_detail').props['aria-pressed'], true);
    const submit = all.find(node => node.props?.['data-usage-control'] === 'chat_options_submit');
    assert.equal(submit.props.disabled, false); submit.props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(sent.length, 1); assert.equal(sent[0].action, 'chat.reply');
    assert.equal(sent[0].payload.text, `Assumed: ${option}${detail ? '\n' + detail : ''}`);
    if (useAction) assert.equal(sent[0].payload.question_id, 'turn');
    assert.equal(composer.draft, '');
    const css = readFileSync(path.join(__dirname, 'ChatDock.module.css'), 'utf8');
    assert.match(css, /\.optionsHint\s*\{[^}]*color: var\(--fg-2/);
    }
    }
  }
});

test('optional detail hints appear once inside the card, never as an alert, across unrelated audiences', () => {
  for (const audience of ['Quality inspectors', 'New parents']) {
    setup();
    const h = harness('components/chat/ChatDock.tsx');
    const prompt = 'Describe a situation if you want to add context.';
    const props = {messages: [{id: 'question', role: 'assistant', body_md: `What matters to ${audience}?`, actions: [
      {label: 'A recurring problem', action: 'chat.reply', payload: {question_id: 'turn', requires_detail: true, detail_prompt: prompt}}
    ]}], onSend() {}, onAction() {}};
    let all = nodes(h.render(h.exports.ChatDock, props));
    all.find(node => node.props?.['data-usage-control'] === 'chat_question_detail').props.onClick();
    all = nodes(h.render(h.exports.ChatDock, props));
    const card = all.find(node => node.props?.className === 'options');
    assert.equal(all.filter(node => label(node) === `Optional: ${prompt}`).length, 1);
    assert.ok(nodes(card).some(node => label(node) === `Optional: ${prompt}` && node.props.className === 'optionsHint'));
    assert.ok(nodes(card).some(node => label(node) === 'Your own answer'));
    assert.ok(nodes(card).some(node => label(node) === "I'm not sure"));
    assert.equal(all.some(node => node.props?.role === 'alert' && label(node).includes(prompt)), false);
  }
});

test('new ventures use the shared chat panel, answer chips and the normal name composer across unrelated audiences', async () => {
  for (const fixture of [{ idea: 'Track stock across warehouses', audience: 'Warehouse operators', name: 'Stock Ledger' },
    { idea: 'Help people plan a home garden', audience: 'Home gardeners', name: 'Garden Notebook' }]) {
    setup(); const requests = [];
    const proposal = { id: 'ready', state: 'pending', route: 'startup', message: 'What should we call it?', next_step: 'idea', skipped_steps: [], expires_at: '' };
    const receipt = { status: 'committed', route: 'startup', venture_id: 'ven_saved', workspace_id: 'wsp_saved', next_step: 'idea',
      action: { kind: 'idea', venture_id: 'ven_saved', href: '/v/ven_saved/idea' }, message: 'Saved' };
    const h = harness('app/new/page.tsx', {
      '@/lib/founderUI': { FounderUIContext: { value: 'home' } },
      'next/navigation': { useRouter: () => ({ replace() {} }) }, '@/components/layout/AppShell': { AppShell: 'shell' }, './ConfirmedConversation': {},
      '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': { DictateButton: 'dictate' },
      '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
        api: async (url, options) => {
          if (url.endsWith('/history')) return { ok: true, data: { items: [] } };
          const body = options?.body ? JSON.parse(options.body) : undefined;
          requests.push({ url, body });
          if (url.endsWith('/preview') && !body.previous_proposal_id) return { ok: true, data: { status: 'clarify', id: 'audience', question: 'Who is it for?',
            onboarding_question: 'Who is it for?', onboarding_field: 'customer', onboarding_choices: [fixture.audience, 'Someone else'], choices: [] } };
          return { ok: true, data: url.endsWith('/preview') ? { status: 'proposal', proposal } : url.endsWith('/confirm') ? receipt : { proposal } };
        } },
    }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
    const render = () => nodes(h.render(h.exports.Conversation, {}));
    render(); await h.effects[2](); await new Promise(resolve => setImmediate(resolve));
    const panel = render().find(node => node.type?.name === 'ChatPanel');
    assert.match(panel.type(panel.props).props.className, /dock panel homePanel/);
    render().find(node => node.props?.id === 'entry-message').props.onChange({ target: { value: fixture.idea } });
    render().find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(render().some(node => node.type === 'article' && label(node).includes('CharlieWho is it for?')));
    const chips = render().filter(node => node.props?.['data-usage-control'] === 'onboarding_choice');
    assert.deepEqual(chips.map(node => node.props['aria-label']), [fixture.audience, 'Someone else']);
    chips[0].props.onClick(); await new Promise(resolve => setImmediate(resolve));
    const all = render(), form = all.find(node => node.type === 'form');
    const name = nodes(form).find(node => node.props?.id === 'entry-message');
    assert.ok(name, 'you type the name in the normal chat composer');
    assert.equal(name.props.maxLength, 48);
    assert.equal(all.filter(node => node.type === 'form').length, 1);
    name.props.onChange({ target: { value: fixture.name } });
    render().find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.find(request => request.url.endsWith('/confirm')).body.name, fixture.name);
    assert.ok(requests.find(request => request.body?.previous_proposal_id === 'audience').body.text.includes(fixture.audience));
  }
});

test('name questions and defaults stay in Charlie bubbles for one-tap confirmation or typed edits across domains', async () => {
  for (const fixture of [{idea: 'Schedule equipment inspections', name: 'Inspection Desk', edited: 'Maintenance Notes'},
    {idea: 'Plan camping trips with family', name: 'Camp Journal', edited: 'Trail Planner'}]) for (const edit of [false, true]) {
    setup(); const requests = [];
    const proposal = {id: 'name-question', state: 'pending', route: 'startup', message: 'Review your direction', venture_name: fixture.name,
      next_step: 'idea', skipped_steps: [], expires_at: '2099-01-01T00:00:00Z'};
    const receipt = {status: 'committed', route: 'startup', venture_id: 'ven_named', workspace_id: 'wsp_named', next_step: 'idea',
      action: {kind: 'idea', venture_id: 'ven_named', href: '/v/ven_named/idea'}, message: 'Saved'};
    let fail = edit;
    const h = harness('app/new/page.tsx', {
      '@/components/layout/AppShell': {AppShell: 'shell'}, './ConfirmedConversation': {},
      '@/components/chat/VoiceMic': {VoiceMic: 'voice'}, '@/components/chat/DictateButton': {DictateButton: 'dictate'},
      '@/lib/client': {verifiedSessionSubject: () => 'owned', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ok: true}),
        api: async (url, options) => {
          if (url.endsWith('/history')) return {ok: true, data: {items: []}};
          requests.push({url, body: JSON.parse(options?.body || '{}')});
          if (url.endsWith('/confirm') && fail) { fail = false; throw new Error('Connection lost'); }
          return {ok: true, data: url.endsWith('/preview') ? {status: 'proposal', proposal} : url.endsWith('/confirm') ? receipt : {proposal}};
        }},
    }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
    const render = () => nodes(h.render(h.exports.Conversation, {}));
    render(); await h.effects[2](); await new Promise(resolve => setImmediate(resolve));
    render().find(node => node.props?.id === 'entry-message').props.onChange({target: {value: fixture.idea}});
    render().find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
    await new Promise(resolve => setImmediate(resolve));
    const all = render(), form = all.find(node => node.type === 'form');
    assert.ok(!label(form).includes('What should we call it?'));
    assert.equal(nodes(form).some(node => node.type === 'input'), false);
    assert.equal(nodes(form).find(node => node.type === 'textarea').props.value, '');
    const bubble = all.find(node => node.type === 'article' && label(node).includes('Charlie') && label(node).includes('You can keep this name or change it.'));
    assert.ok(bubble);
    const chip = nodes(bubble).find(node => node.type === 'button' && label(node) === fixture.name);
    assert.equal(chip.props.className, 'option');
    assert.ok(nodes(bubble).some(node => node.props?.['data-usage-control'] === 'onboarding_back' && !node.props.disabled));
    assert.ok(!nodes(form).some(node => node.props?.['data-usage-control'] === 'onboarding_suggest_name'));
    if (edit) {
      nodes(form).find(node => node.type === 'textarea').props.onChange({target: {value: fixture.name.repeat(5)}});
      assert.equal(render().find(node => node.props?.['data-usage-control'] === 'onboarding_continue').props.disabled, true);
      render().find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
      assert.equal(requests.some(request => request.url.endsWith('/confirm')), false);
      nodes(form).find(node => node.type === 'textarea').props.onChange({target: {value: fixture.edited}});
      render().find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(render().find(node => node.props?.id === 'entry-message').props.value, fixture.edited);
      assert.ok(render().some(node => node.props?.error === 'Connection lost'));
      render().find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
      nodes(form).find(node => node.type === 'textarea').props.onChange({target: {value: `Notes for ${fixture.edited}`}});
    } else {
      chip.props.onClick(); chip.props.onClick();
    }
    await new Promise(resolve => setImmediate(resolve));
    const confirmations = requests.filter(request => request.url.endsWith('/confirm'));
    assert.equal(confirmations.length, edit ? 2 : 1);
    assert.equal(confirmations.at(-1).body.name, edit ? fixture.edited : fixture.name);
    assert.equal(h.state[1], edit ? `Notes for ${fixture.edited}` : '');
  }
});

test('chat defaults confirm once from the question bubble and typed edits still use the normal composer across domains', async () => {
  for (const words of ['A dispatcher records inspections in a spreadsheet.', 'My family keeps camping plans in a shared notebook.']) for (const explicit of [false, true]) {
    setup(); const sent = [], composer = {draft: '', pending: false, setDraft(value) { this.draft = value; }, clearError() {}};
    const h = harness('components/chat/ChatDock.tsx', {'./useChatComposer': {useChatComposer: () => composer}});
    const message = {id: 'default-question', role: 'assistant', body_md: 'What do you do today?', actions: [
      {label: explicit ? `Use: ${words}` : 'Use my saved answer', action: 'chat.reply', payload: {question_id: 'default-question', prefill: words}}]};
    const props = {messages: [message], onSend() {}, onAction: async (action, payload) => {sent.push({action, payload}); return true;}};
    const render = () => nodes(h.render(h.exports.ChatDock, props));
    const all = render(), input = all.find(node => node.type === 'textarea');
    assert.equal(input.props.value, '');
    const use = all.find(node => node.type === 'button' && label(node) === `Use: ${words}`);
    assert.equal(use.props.className, 'option');
    use.props.onClick(); use.props.onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(sent.length, 1); assert.equal(sent[0].action, 'chat.reply'); assert.equal(sent[0].payload.text, words);
    assert.equal(composer.draft, '');
    message.id = 'edited-question';
    input.props.onChange('I want to change my saved answer.');
    render().find(node => node.props?.['data-usage-control'] === 'chat_options_submit').props.onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(sent.at(-1).payload.text, 'I want to change my saved answer.');
  }
});

test('chat and home settings load as chat-first; classic remains classic', async () => {
  for (const layout of ['chat', 'home', 'classic']) {
    setup(); Object.assign(window, {addEventListener() {}, removeEventListener() {}});
    global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
    const h = harness('lib/founderUI.tsx', {'./client': {
      api: async () => ({ok: true, data: {founder_ui: layout}}), verifiedSessionEpoch: () => 1, verifiedSessionSubject: () => 'owner',
    }});
    assert.deepEqual(h.render(h.exports.useFounderUIState, 'owner'), {layout: 'home', loaded: false});
    h.effects.forEach(effect => effect());
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(h.render(h.exports.useFounderUIState, 'owner'), {layout: layout === 'chat' ? 'home' : layout, loaded: true});
  }
});

test('company chat never follows a navigation reply in chat-first, but classic still can', () => {
  for (const layout of ['home', 'classic']) {
    setup(); let options;
    const h = harness('components/chat/VentureChatLauncher.tsx', {
      '@/lib/founderUI': {FounderUIContext: {value: layout}},
      './useVentureChat': {useVentureChat: (_venture, next) => { options = next; return {messages: [], conversationItems: [], managerName: 'Charlie'}; }},
      './ChatDock': {ChatDock: 'chat'}, './CharlieDock': {CharlieMascot: 'face'},
    });
    h.render(h.exports.VentureChatLauncher, {ventureId: 'idea-stage', size: {}, onSizeChange() {}});
    assert.equal(options.navigateOnResponse, layout === 'classic');
  }
});

test('missing idea fields offer one-tap chat prefill, never send or navigate', () => {
  setup(); const asks = [];
  const h = harness('app/v/[ventureId]/idea/current/page.tsx', {
    'next/navigation': {useParams: () => ({ventureId: 'idea-fields'})},
    '@/components/layout/AppShell': {AppShell: 'shell'}, '@/components/primitives/RelativeTime': {RelativeTime: 'time'},
    '@/lib/chatPrefill': {prefillChat: text => asks.push(text)},
  }, [], ';exports.Section = Section;');
  for (const summary of ['Clinic appointment reminders', 'Library loan reminders']) {
    h.state[1] = {summary, problem: ' Unknown ', offer: ''}; h.state[3] = false;
    const sections = nodes(h.render(h.exports.default)).filter(node => node.type === h.exports.Section);
    for (const title of ['Problem', 'Current offer']) {
      const section = sections.find(node => node.props.title === title);
      const all = nodes(h.render(h.exports.Section, section.props));
      assert.match(label(all[0]), /Not set yet\. Tell Charlie the (?:problem|offer)/);
      const buttons = all.filter(node => node.type === 'button');
      assert.equal(buttons.length, 1);
      assert.equal(buttons[0].props.type, 'button');
      const before = asks.length; buttons[0].props.onClick();
      assert.equal(asks.length, before + 1); assert.match(asks.at(-1), /Ask me what is missing, then update my idea/);
      for (const missing of [undefined, null, '', '  ', 'unknown', 'UNKNOWN', 'n/a'])
        assert.equal(nodes(h.render(h.exports.Section, {...section.props, text: missing})).filter(node => node.type === 'button').length, 1);
      const filled = nodes(h.render(h.exports.Section, {...section.props, text: summary}));
      assert.equal(filled.filter(node => node.type === 'button').length, 0); assert.ok(label(filled[0]).includes(summary));
    }
  }
});

test('U49 committed conversation keeps its known name while the saved venture read is pending', async () => {
  setup(); let finish; const statuses = [], chat = { messages: [], contextCurrent: () => true };
  const h = harness('app/new/ConfirmedConversation.tsx', {
    '@/lib/client': { api: () => new Promise(resolve => { finish = resolve; }) },
    '@/components/chat/useVentureChat': { useVentureChat: () => chat },
    '@/components/chat/ChatDock': { ChatDock: 'chat' },
  });
  const props = { initialName: 'Supymem', onStatusChange: (ventureId, status) => statuses.push({ventureId, status}), receipt: { status: 'committed', workspace_id: 'workspace-named', venture_id: 'venture-named', route: 'startup', next_step: 'idea', action: { kind: 'idea', venture_id: 'venture-named', href: '/v/venture-named/idea' } } };
  const render = () => nodes(h.render(h.exports.ConfirmedConversation, props));
  const first = render();
  assert.equal(label(first.find(node => node.type === 'h2')), 'Supymem');
  h.effects.forEach(effect => effect());
  finish({ ok: true, data: { name: 'Supymem Console' } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(label(render().find(node => node.type === 'h2')), 'Supymem Console');
  chat.messages = [{id: 'review-ready', role: 'assistant', kind: 'idea_validation_ready', body_md: 'Review your idea'}];
  render(); h.effects.at(-1)();
  assert.deepEqual(statuses.at(-1), {ventureId: 'venture-named', status: {state: 'needs_you', activity: 'Review your saved idea'}});
});

test('U55 clarification renders progress and idea once, before its transcript', () => {
  setup(); window.location.pathname = '/v/idea-visible/idea';
  const h = harness('app/v/[ventureId]/idea/page.tsx', {
    'next/navigation': { useParams: () => ({ ventureId: 'idea-visible' }), usePathname: () => window.location.pathname, useSearchParams: () => new URLSearchParams() },
    '@/components/layout/AppShell': { AppShell: 'shell' }, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
    '@/lib/apiCache': { cacheGet: () => null, cachePut() {} },
    '@/hooks/useDurableJob': { useDurableJob: () => ({}) },
    '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1 },
  });
  h.render(h.exports.default);
  h.state[7] = { id: 'session-visible', state: 'clarifying', case: 'know', idea_summary: 'A saved idea', turns: [
    { id: 'opening', role: 'founder', kind: 'opening', text: 'A saved idea' },
    { id: 'answer', role: 'founder', kind: 'answer', text: 'Teachers' },
    { id: 'question', role: 'agent', kind: 'question', text: 'Who needs it?' },
  ] };
  h.state[8] = { name: 'Supymem' }; h.state[21] = true;
  const all = nodes(h.render(h.exports.default));
  const progress = all.filter(node => node.props?.['aria-label'] === 'Journey progress');
  const ideas = all.filter(node => node.props?.className === 'ideaCard');
  const transcripts = all.filter(node => node.props?.['aria-label'] === 'Idea conversation');
  assert.equal(progress.length, 1); assert.equal(ideas.length, 1); assert.equal(transcripts.length, 1);
  assert.ok(all.indexOf(ideas[0]) < all.indexOf(transcripts[0]));
  assert.match(label(transcripts[0]), /Teachers/);
});

test('U56 journey options inherit the normal chat option type size and weight', () => {
  const css = readFileSync(path.join(root, 'app/v/[ventureId]/idea/idea.module.css'), 'utf8');
  const chatCss = readFileSync(path.join(root, 'components/chat/ChatDock.module.css'), 'utf8');
  const optionFont = chatCss.match(/\.option, \.optionSkip\s*\{[^}]*font:\s*([^;]+);/)[1];
  assert.ok(css.includes(`.composer label.option { font: ${optionFont}; }`));
  assert.match(css, /\.choiceLabel\s*\{\s*font: inherit;/);
});

test('U58 enabled journey Submit uses primary colors without overriding disabled buttons', () => {
  const css = readFileSync(path.join(root, 'app/v/[ventureId]/idea/idea.module.css'), 'utf8');
  assert.match(css, /\.answerActions button:global\(\.primary\):not\(:disabled\)\s*\{[^}]*background: var\(--accent\);[^}]*color: var\(--accent-ink\);/);
  assert.match(css, /\.answerActions button:global\(\.primary\):not\(:disabled\):hover\s*\{\s*background: var\(--accent-hover\);/);
});

test('U54 skip choices explain saved context in plain language', () => {
  setup(); window.location.pathname = '/v/venture-skip/idea';
  const h = harness('components/layout/NextStepStrip.tsx', {
    '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1 },
    'next/navigation': { usePathname: () => window.location.pathname },
  });
  const props = { step: { skip_step: 'clarification', receiptScope: { venture: 'venture-skip', owner: 'founder', epoch: 1 } } };
  const all = nodes(h.render(h.exports.JourneyChoices, props));
  assert.ok(all.some(node => node.type === 'p' && label(node) === "You can skip this; Supymem will use what you've told it so far."));
  const compact = nodes(h.render(h.exports.JourneyChoices, { ...props, compact: true }));
  assert.equal(compact.find(node => node.type === 'button' && label(node) === 'Skip this step').props.title, "You can skip this; Supymem will use what you've told it so far.");
});

test('U28 failed confirmation repeats neither its body nor its consequences', () => {
  setup();
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => ({ draft: '', pending: false, setDraft() {}, clearError() {} }) } });
  const failure = 'Validation did not finish. Your saved idea is kept; review the saved status before retrying.';
  const tree = h.render(h.exports.ChatDock, { messages: [{ id: 'failed', role: 'assistant', body_md: failure,
    actions: [{ label: 'Review status', confirmation: { action: 'idea.status', interpretation: failure, consequences: ['Nothing was sent.'] } }] }], onSend() {}, onAction() {} });
  assert.equal(nodes(tree).map(node => node.props?.dangerouslySetInnerHTML?.__html || '').join('').split(failure).length - 1, 1);
  assert.equal(nodes(tree).filter(node => node.type === 'p' && node.props.className === 'confirmLead').length, 0);
  assert.equal(label(tree).split('Nothing was sent.').length - 1, 0);
  assert.ok(nodes(tree).some(node => node.props?.['data-usage-control'] === 'chat_followup_action'));
});

test('Charlie header opens settings without edit or call tools, saved chats feed the sidebar', async () => {
  setup(); const actions = [], events = [], listeners = {};
  window.dispatchEvent = event => events.push(['supymem:chat-new', 'supymem-chat-talk', 'supymem-chat-voice', 'supymem:open-settings'].includes(event.type) ? event.type : event.detail);
  window.addEventListener = (type, fn) => { listeners[type] = fn; }; window.removeEventListener = () => {};
  const chat = { managerName: 'Ada', historyReady: true, messages: [{id: 'saved', role: 'assistant', body_md: 'Your saved plan'}],
    conversationsAvailable: true, conversationBusy: false, conversationId: 'private_one', conversationDraftId: 'private_one', conversationKey: 'private_one',
    conversationItems: [{id: 'private_one', title: 'Saved customer research', updated_at: '2026-10-21T10:00:00Z', pinned: true, archived: false, folder: 'Research'}, {id: 'shared', title: 'Saved plan', visibility: 'legacy_shared', created_at: '2026-10-01T10:00:00Z', pinned: false, archived: false, folder: null}], archivedConversationItems: [], conversationError: 'Could not load saved chats',
    updateConversation: async () => true,
    newConversation: async () => actions.push('new'), selectConversation: id => actions.push(id), reloadConversations: async () => actions.push('retry'),
    sendChat: async (body, options) => { actions.push([body, options]); options?.onReply?.({id: 'receipt', role: 'assistant', body_md: 'Saved reply'}); },
    confirmChatAction: async (action, payload) => {
      if (action === 'chat.new_conversation') return chat.newConversation();
      if (action === 'chat.prefill') return require('../../lib/chatPrefill.ts').applyChatPrefill(payload.text);
      actions.push([action, payload]);
    },
  };
  const h = harness('components/chat/VentureChatLauncher.tsx', {
    '@/lib/founderUI': { FounderUIContext: {value: 'home'} }, './useVentureChat': {useVentureChat: () => chat}, './ChatDock': {ChatDock: 'dock'},
  });
  const props = {ventureId: 'ven_owned', onMinimize: () => actions.push('dock'), pendingAsk: {action: require('../../lib/chatPrefill.ts').PREFILL_ACTION, payload: {text: 'Draft only'}}, onAskSent: () => actions.push('consumed'), open: true};
  const all = nodes(h.render(h.exports.VentureChatLauncher, props));
  assert.ok(all.some(node => node.props?.className === 'who' && label(node) === 'Ada'));
  assert.equal(all.some(node => node.type === 'details' || label(node) === 'Talk to me'), false);
  assert.equal(all.some(node => ['New chat', 'Talk to Charlie live'].includes(node.props?.['aria-label'])), false);
  all.find(node => node.props?.className === 'titleBtn').props.onClick();
  assert.deepEqual(events, ['supymem:open-settings']);
  events.length = 0;
  await all.find(node => node.type === 'button' && label(node) === 'Try again').props.onClick();
  all.find(node => node.props?.['aria-label'] === 'Dock Ada').props.onClick();
  assert.deepEqual(actions, ['retry', 'dock']);
  const dock = all.find(node => node.type === 'dock');
  assert.equal(dock.props.messages, chat.messages);
  assert.equal(dock.props.conversationId, 'private_one');
  assert.equal(dock.props.visible, true);
  const reply = await dock.props.onVoiceSend('Talk to customers', {idempotencyKey: 'voice_nonce'});
  assert.equal(reply.id, 'receipt');
  assert.equal(actions.at(-1)[1].idempotencyKey, 'voice_nonce');
  await dock.props.onAction('chat.new_conversation');
  assert.equal(actions.at(-1), 'new');
  h.effects.forEach(effect => effect());
  const history = events.find(detail => detail?.items);
  assert.equal(history.ventureId, 'ven_owned'); assert.equal(history.current, 'private_one');
  assert.equal(history.saved, true); assert.equal(history.update, chat.updateConversation); assert.equal(typeof history.loadArchived, 'function');
  assert.deepEqual(history.archivedItems, []);
  assert.deepEqual(history.items, [
    {id: 'private_one', text: 'Saved customer research', at: '2026-10-21T10:00:00Z', pinned: true, archived: false, folder: 'Research'},
    {id: 'shared', text: 'Saved plan', at: '2026-10-01T10:00:00Z', pinned: false, archived: false, folder: null}]);
  assert.ok(events.some(detail => detail?.text === 'Draft only'));
  assert.equal(actions.at(-1), 'consumed');
  listeners['supymem:chat-jump']({detail: {id: 'shared'}});
  events.length = 0; listeners['supymem:chat-new']();
  assert.deepEqual([actions.slice(-2), events], [['shared', 'new'], ['supymem-chat-talk']]);
  listeners['supymem:conversation']({detail: {title: 'Acme'}});
  assert.ok(nodes(h.render(h.exports.VentureChatLauncher, props)).some(node => node.type === 'strong' && label(node) === 'Acme'));
});

test('PR iMessage stacks preserve saved messages, attachments, voice, stop and real send', async () => {
  setup(); const sent = [], files = [];
  const composer = {draft: 'Find paying customers', pending: false, clearError() {}, setDraft(value) {this.draft = value;}, submit: async body => {sent.push(body || composer.draft); return true;}};
  const attachments = {files: [{id: 'evidence', source_label: 'notes/research.md', status: 'read'}], evidenceIds: ['evidence'], uploading: false,
    add: async selected => files.push(selected), remove() {}, restore() {}};
  const h = harness('components/chat/ChatDock.tsx', {
    '@/lib/founderUI': { FounderUIContext: {value: 'home'} }, './useChatComposer': {useChatComposer: () => composer},
    './IntakeAttachments': {useIntakeAttachments: () => attachments, IntakeAttachments: 'attachments'},
  });
  const props = {variant: 'panel', ventureId: 'ven_owned', managerName: 'Ada', onSend() {}, onVoiceSend() {}, onAction() {},
    messages: [{id: 'incoming', role: 'assistant', body_md: 'Your real plan'}, {id: 'own', role: 'user', body_md: 'My actual idea'}, {id: 'system', role: 'system', body_md: 'Connection restored'}],
    streaming: {messageId: 'live', speaker: 'Researcher', text: 'Looking at your evidence'}};
  const all = nodes(h.render(h.exports.ChatDock, props));
  const rows = all.filter(node => node.type?.name === 'ChatMessageRow').flatMap(node => nodes(node.type(node.props)));
  assert.equal(rows.filter(node => node.props?.className === 'imStack').length, 2);
  assert.ok(all.some(node => node.props?.className === 'imName' && label(node) === 'Charlie'));
  for (const audience of ['Warehouse dispatch teams', 'Home vegetable gardeners']) {
    const conversation = nodes(h.render(h.exports.ChatDock, {...props, messages: props.messages.map(message => ({...message, body_md: `${message.body_md} ${audience}`}))}));
    assert.ok(conversation.some(node => node.props?.className === 'imName' && label(node) === 'Charlie'), audience);
    assert.ok(conversation.filter(node => node.type?.name === 'ChatMessageRow').some(node => node.props.portrait?.type?.name === 'CharlieMascot'), audience);
  }
  assert.ok(all.some(node => node.props?.className === 'imName' && label(node) === 'Researcher'));
  assert.ok(all.some(node => node.props?.dangerouslySetInnerHTML?.__html.includes('Your real plan')));
  assert.ok(all.some(node => node.props?.dangerouslySetInnerHTML?.__html.includes('My actual idea')));
  assert.ok(all.some(node => node.props?.className === 'msgSystem' && label(node) === 'Connection restored'));
  assert.ok(rows.filter(node => node.type === 'portrait').every(node => node.props.size === 54 && node.props.className === 'imAvatar'));
  const menu = all.find(node => node.type === 'details');
  assert.equal(nodes(menu).find(node => node.type === 'summary').props['aria-label'], 'Attach files or folder');
  assert.equal(nodes(menu).find(node => node.type === 'attachments').props.attachments, attachments);
  assert.equal(all.filter(node => node.type === 'attachments').length, 1);
  for (const type of ['voice', 'dictate', 'stop']) assert.ok(all.some(node => node.type === type));
  assert.ok(all.some(node => node.props?.['aria-label'] === 'Stop Charlie'));
  const send = all.find(node => node.props?.['data-usage-control'] === 'chat_send');
  assert.equal(label(send), '↑'); assert.equal(send.props['aria-label'], 'Send');
  await send.props.onClick();
  assert.deepEqual(sent, ['Find paying customers']);
  const classic = nodes(h.render(h.exports.ChatDock, {...props, variant: 'dock'}));
  assert.ok(classic.some(node => node.props?.['data-usage-control'] === 'chat_send' && label(node) === 'Send'));
  assert.equal(classic.filter(node => node.props?.className === 'imStack').length, 0);
  props.messages = [{id: 'question', role: 'assistant', body_md: 'Who needs this?', actions: [{label: 'Teachers', text: 'Teachers', action: 'chat.reply', payload: {question_id: 'audience'}}]}];
  props.streaming = undefined;
  const question = nodes(h.render(h.exports.ChatDock, props));
  const input = question.find(node => node.props?.inputRef);
  let focused = false;
  input.props.inputRef.current = {focus() {focused = true;}};
  let talk;
  window.addEventListener = (name, listener) => {if (name === 'supymem-chat-talk') talk = listener;};
  h.effects.find(effect => effect.toString().includes('supymem-chat-talk'))();
  talk();
  assert.ok(focused);
  focused = false;
  question.find(node => node.type === 'button' && label(node) === 'Your own answer').props.onClick();
  assert.ok(focused);
  assert.ok(question.some(node => node.props?.['data-usage-control'] === 'chat_options_submit' && label(node) === '↑' && node.props['aria-label'] === 'Send'));
});

test('Charlie outside a venture has real choose/start/dock actions and classic stays unchanged', () => {
  const context = {value: 'home'}, actions = [];
  const h = harness('components/chat/CharlieDock.tsx', {'@/lib/founderUI': {FounderUIContext: context}});
  const props = {managerName: 'Ada', open: false, chatAvailable: false, onOpen: () => actions.push('open'), onClose: () => actions.push('close')};
  const closed = h.render(h.exports.CharlieDock, props);
  assert.equal(closed.props.className, 'dock homeDock'); closed.props.onClick();
  props.open = true;
  const opened = nodes(h.render(h.exports.CharlieDock, props));
  assert.equal(opened[0].props.id, 'supymem-chat-panel');
  assert.ok(opened.some(node => node.props?.href === '/'));
  assert.ok(opened.some(node => node.props?.href === '/new'));
  opened.find(node => node.props?.['aria-label'] === 'Dock Ada').props.onClick();
  assert.deepEqual(actions, ['open', 'close']);
  assert.equal(h.render(h.exports.CharlieDock, {...props, chatAvailable: true}), null);
  context.value = 'classic';
  assert.equal(h.render(h.exports.CharlieDock, {...props, open: false}).props.className, 'dock');
});

test('production chat chrome keeps its original face while local chat uses your animated mascot across domains', () => {
  const h = harness('components/chat/CharlieDock.tsx');
  for (const domain of ['procurement teams', 'home gardeners']) for (const local of [false, true]) for (const open of [false, true]) {
    const shown = nodes(h.render(h.exports.CharlieDock, {managerName: domain, local, open, chatAvailable: false, onOpen() {}, onClose() {}}));
    const expected = local ? h.exports.CharlieMascot : h.exports.CharlieFace;
    const excluded = local ? h.exports.CharlieFace : h.exports.CharlieMascot;
    assert.ok(expected, domain);
    assert.equal(shown.filter(node => node.type === expected).length, open ? 2 : 1, domain);
    assert.equal(shown.some(node => node.type === excluded), false, domain);
    if (!open) assert.equal(shown[0].props.className, local ? 'dock localDock' : 'dock', domain);
  }
});

test('Charlie module CSS matches the PR dimensions, colors and motion without changing classic selectors', () => {
  const css = file => readFileSync(path.join(__dirname, file), 'utf8');
  const chat = css('ChatDock.module.css'), launcher = css('VentureChatLauncher.module.css'), corner = css('CharlieDock.module.css');
  assert.match(launcher, /\.who > span \{ width: var\(--im-profile, 64px\); height: var\(--im-profile, 64px\);/);
  assert.match(launcher, /\.who strong \{ font: 600 var\(--im-text, 16px\)\/1\.15[^}]*letter-spacing: -0\.01em/);
  assert.doesNotMatch(launcher, /\.toolbar|\.toolRound|\.tool\s*\{/);
  assert.match(launcher, /\.homePanel \.header \{ position: absolute;[^}]*padding: 36px 12px 8px/);
  assert.match(launcher, /\.minimize \{ position: absolute; top: 12px; right: 12px;/);
  assert.match(launcher, /padding: 8px 12px 4px/);
  assert.match(launcher, /translateX\(26px\)/);
  assert.match(launcher, /prefers-reduced-motion: reduce/);
  assert.match(chat, /\.homePanel \.imStack \{[^}]*max-width: 78%/);
  assert.match(chat, /\.homePanel \.msgMeta \{\s*display: none/);
  assert.match(chat, /\.homePanel \.bubble \{[^}]*background: #e9e9eb/);
  assert.match(chat, /\.homePanel \.bubbleOwn \{[^}]*background: var\(--accent-hover\);[^}]*color: #fff/);
  assert.match(chat, /padding: 14px 18px;\s*border-radius: 18px/);
  assert.match(chat, /\.homePanel \.msgText \{\s*font: 400 var\(--im-text, 16px\)\/var\(--im-line, 21px\)/);
  assert.match(chat, /\.homePanel \.msgRow \{\s*grid-template-columns: var\(--im-avatar, 54px\) minmax\(0, 1fr\)/);
  assert.match(chat, /\.imPlus \{[^}]*width: var\(--im-field, 36px\);[^}]*font: 300 var\(--im-glyph, 22px\)\/1/);
  assert.match(chat, /\.homePanel \.composerBox \{[^}]*border: 1px solid #e5e5ea;[^}]*border-radius: calc\(var\(--im-field, 36px\) \/ 2\)/);
  assert.match(corner, /\.homeDock \{[^}]*position: fixed;[^}]*width: 178px;[^}]*height: 156px;[^}]*border-radius: 0 0 0 36px/);
  assert.match(corner, /\.homeEmptyPanel \{[^}]*position: fixed;[^}]*width: min\(340px, calc\(100vw - 36px\)\)/);
  assert.match(corner, /\.dock \{[^}]*position: relative;[^}]*background: #05070b/);
  assert.match(corner, /\.localDock \{[^}]*background: var\(--bg-card\)/);
});

test('Mac U2/U3: home header centers independently of classic, and the thread/composer shrink within the panel', () => {
  const launcher = readFileSync(path.join(__dirname, 'VentureChatLauncher.module.css'), 'utf8');
  const chat = readFileSync(path.join(__dirname, 'ChatDock.module.css'), 'utf8');
  assert.match(launcher, /\.homePanel \.header \{[^}]*grid-template-columns: minmax\(0, 1fr\);[^}]*justify-content: stretch;[^}]*justify-items: center/);
  assert.match(launcher, /\.header \{[^}]*justify-content: space-between;[^}]*flex: none/);
  assert.match(launcher, /\.minimize \{ position: absolute; top: 12px; right: 12px/);
  assert.match(launcher, /\.homePanel \.content \{[^}]*display: flex;[^}]*flex: 1 1 0;[^}]*overflow: hidden/);
  assert.match(chat, /\.homePanel \{[^}]*flex: 1;[^}]*height: auto;[^}]*min-width: 0/);
  assert.match(chat, /\.homePanel \.composerBox \{[^}]*min-width: 0;[^}]*flex-wrap: wrap;[^}]*flex: 1 1 0/);
  for (const selector of ['composerActions', 'composerTools']) {
    assert.match(chat, new RegExp(`\\.homePanel \\.${selector} \\{[^}]*min-width: 0;[^}]*max-width: 100%;[^}]*flex-wrap: wrap`));
  }
  assert.match(chat, /\.homePanel \.composerTools > span \{[^}]*min-width: 0;[^}]*max-width: 100%;[^}]*flex-wrap: wrap/);
  assert.match(chat, /button\[data-usage-control="chat_send"\],[\s\S]*width: var\(--im-inline, 28px\);\s*height: var\(--im-inline, 28px\)/);
});

function checkpointChat(state = 'unknown', storage = new Map(), confirm = true, ventureId = 'ven_owned', conversationDraftId = 'cnv_owned') {
  setup(); const calls = [], confirmations = [], statusCalls = [], signals = [];
  let active = true, status = state, statusReply, postReply;
  const checkpointScope = JSON.stringify(['usr_owned', ventureId, 'cnv_owned']);
  const checkpointKey = `supymem.chatRequest.v1:${encodeURIComponent(checkpointScope)}`;
  window.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) };
  window.confirm = message => { confirmations.push(message); return confirm; };
  window.setTimeout = () => {};
  const h = harness('components/chat/useVentureChat.ts', {
    'next/navigation': { useRouter: () => ({push() {}}) },
    './useChatConversations': { useChatConversations: () => ({ contextCurrent: () => active, conversationId: 'cnv_owned', conversationDraftId, conversationVersion: 1, conversationReady: true, conversationCurrent: () => active }) },
    '@/lib/client': { verifiedSessionSubject: () => 'usr_owned', verifiedSessionEpoch: () => 1, ensureSession: async () => {},
      api: async (url, init) => {
        if (url.includes('/chat/requests/')) { statusCalls.push(url); return statusReply ? statusReply() : {ok: true, data: {state: status}}; }
        if (!init) return {ok: true, data: {conversation_id: 'cnv_owned', messages: []}};
        calls.push(JSON.parse(init.body));
        signals.push(init.signal);
        if (url.endsWith('/chat/stop')) return {ok: true, data: {items: []}};
        if (postReply) return postReply;
        return {ok: true, status: 200, data: {id: `saved-${calls.length}`, role: 'assistant', conversation_id: 'cnv_owned', body_md: 'Saved reply'}};
      } },
    '@/lib/desktop': { isDesktopShell: () => false },
  });
  const render = () => h.render(h.exports.useVentureChat, ventureId);
  render();
  const save = (extra = {}) => { const raw = JSON.stringify({version: 1, scope: checkpointScope, nonce: 'old-nonce', payload_digest: 'a'.repeat(64), ...extra}); storage.set(checkpointKey, raw); return raw; };
  return {h, render, save, storage, checkpointKey, calls, signals, confirmations, statusCalls, retire: () => {active = false;}, setStatus: value => {status = value;}, setStatusReply: value => {statusReply = value;}, setPostReply: value => {postReply = value;}};
}

test('build-path startup review click keeps venture context through the chat bar and renders the in-chat review', async () => {
  for (const [ventureId, domain] of [['ven_factory', 'Equipment certification for factory teams'], ['ven_recipes', 'Recipe sharing for home cooks']]) {
    const chat = checkpointChat('unknown', new Map(), true, ventureId);
    window.dispatchEvent = () => {};
    let pendingAction;
    chat.setPostReply({ok: true, status: 200, data: {id: `review-${ventureId}`, role: 'assistant', conversation_id: 'cnv_owned',
      kind: 'startup_review', body_md: `Review your saved answers for ${domain}.`, actions: []}});
    const bar = harness('components/chat/ChatBar.tsx', {'./ChatDock': {ChatDock: 'dock'}}, [true]);
    const card = {id: `build-${ventureId}`, role: 'assistant', kind: 'navigation', body_md: domain,
      actions: [{label: 'Review saved answers', action: 'startup.review', style: 'primary'}]};
    const props = nodes(bar.render(bar.exports.ChatBar, {ventureId, subject: 'usr_owned', conversationId: 'cnv_owned',
      messages: [card], onSend() {}, onAction: (action, payload) => pendingAction = chat.render().confirmChatAction(action, payload)})).find(node => node.type === 'dock').props;
    assert.equal(props.ventureId, ventureId);
    assert.equal(props.subject, 'usr_owned');
    const dock = harness('components/chat/ChatDock.tsx', {'./StartupReview': {StartupReview: 'startup-review'},
      './useChatComposer': {useChatComposer: () => ({draft: '', pending: false, setDraft() {}, clearError() {}})}});
    const render = () => nodes(dock.render(dock.exports.ChatDock, props));
    const button = render().find(node => node.type === 'button' && label(node) === 'Review saved answers');
    assert.equal(button.props.disabled, false);
    button.props.onClick();
    assert.equal(await pendingAction, true);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(chat.calls.length, 1);
    assert.equal(chat.calls[0].action, 'startup.review');
    props.messages = [card, ...chat.render().messages];
    const review = render().find(node => node.type === 'startup-review');
    assert.equal(review.props.ventureId, ventureId);
    assert.equal(typeof review.props.onContinue, 'function');
    chat.setPostReply({ok: false, status: 500, data: {}});
    render().find(node => node.type === 'startup-review').props.onContinue();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(await pendingAction, false);
    await new Promise(resolve => setImmediate(resolve));
    const failures = chat.render().messages.filter(message => message.id.startsWith('local-err-'));
    assert.equal(failures.length, 1);
    const [failure] = failures;
    props.messages = [card, ...chat.render().messages];
    assert.equal(render().filter(node => node.type === 'div' && [node.props.children].flat().includes(failure.body_md)).length, 1);
    assert.equal(render().some(node => node.type === 'error' && node.props.source === 'chat.startup_review' && node.props.error), false);
    chat.setPostReply({ok: true, status: 200, data: {id: `next-${ventureId}`, role: 'assistant', conversation_id: 'cnv_owned',
      kind: 'navigation', body_md: 'You can research your market next.',
      actions: [{label: 'Research your market', action: 'research.prepare', style: 'primary'}]}});
    render().find(node => node.type === 'startup-review').props.onContinue();
    assert.equal(await pendingAction, true);
    assert.equal(chat.calls[2].action, 'whats_next');
    assert.equal(chat.render().messages.at(-1).actions[0].action, 'research.prepare');
  }
});

test('successful retries ignore a previous same-tick failure across unrelated domains', async context => {
  context.mock.method(Date, 'now', () => 1000);
  for (const [ventureId, domain] of [['ven_dispatch', 'Dispatch software for freight brokers'], ['ven_gardens', 'Balcony gardening for apartment renters']]) {
    const chat = checkpointChat('unknown', new Map(), true, ventureId);
    window.dispatchEvent = () => {};
    chat.setPostReply({ok: false, status: 500, data: {}});
    assert.equal(await chat.render().confirmChatAction('whats_next'), false);
    assert.equal(chat.render().messages.filter(message => message.id.startsWith('local-err-')).length, 1);
    chat.setPostReply({ok: true, status: 200, data: {id: `next-${ventureId}`, role: 'assistant', conversation_id: 'cnv_owned', body_md: domain}});
    assert.equal(await chat.render().confirmChatAction('whats_next'), true);
    const messages = chat.render().messages;
    assert.equal(messages.filter(message => message.id.startsWith('local-err-')).length, 0);
    assert.equal(messages.at(-1).body_md, domain);
    assert.equal(chat.calls.length, 2);
  }
});

test('unhandled followups and confirmations show a disabled reason and report bypassed clicks across domains', async () => {
  for (const domain of ['Equipment certification for factory teams', 'Recipe sharing for home cooks']) {
    setup(); const events = [], calls = [], reason = 'You cannot use this action in chat yet. Ask me for your next step.';
    const dock = harness('components/chat/ChatDock.tsx', {'@/lib/userError': {reportUserError: event => events.push(event)},
      './useChatComposer': {useChatComposer: () => ({draft: '', pending: false, setDraft() {}, clearError() {}})}});
    const props = {messages: [{id: 'unsupported', role: 'assistant', body_md: domain, actions: [
      {label: 'Unknown next step', action: 'unknown.next_step', disabled_reason: reason},
      {label: 'Unknown confirmation', confirmation: {action: 'unknown.confirm'}, disabled_reason: reason}]}],
      onSend() {}, onAction: action => calls.push(action)};
    const render = () => nodes(dock.render(dock.exports.ChatDock, props));
    for (const text of ['Unknown next step', 'Unknown confirmation']) {
      const button = render().find(node => node.type === 'button' && label(node) === text);
      assert.equal(button.props.disabled, true);
      assert.equal(button.props.title, reason);
      button.props.onClick();
      await new Promise(resolve => setImmediate(resolve));
    }
    assert.equal(calls.length, 0);
    assert.equal(events.length, 2);
    assert(events.every(event => event.serverCode === 'unhandled_chat_action' && event.source === 'chat.unhandled_action'));
    assert.equal(render().filter(node => node.type === 'error' && node.props.error === reason).length, 2);
  }
});

test('Stop aborts real chat transport, sends the original nonce, and accepts corrected text without replay', async () => {
  const chat = checkpointChat(); let finish;
  chat.setPostReply(new Promise(resolve => {finish = resolve;}));
  const original = chat.render().sendChat('Research teachers');
  // Wait for the request to be recorded; sending takes a few async steps before the POST.
  for (let tick = 0; tick < 400 && !chat.calls.length; tick++) await new Promise(resolve => setTimeout(resolve, 5));
  const nonce = chat.calls[0].idempotency_key;
  assert.equal(chat.render().thinking, true);
  const stopped = chat.render().confirmChatAction('chat.stop', {job_ids: ['job_original']});
  assert.equal(chat.signals[0].aborted, true);
  assert.equal(chat.render().thinking, false);
  assert.equal(chat.storage.has(chat.checkpointKey), false);
  assert.deepEqual(chat.calls[1], {conversation_id: 'cnv_owned', job_ids: ['job_original'], nonce});
  chat.setPostReply(undefined);
  assert.equal(await chat.render().sendChat('Research dentists'), true);
  assert.notEqual(chat.calls[2].idempotency_key, nonce);
  finish({ok: false, status: 0});
  assert.equal(await original, false);
  await stopped;
  assert.equal(chat.render().thinking, false);
  assert.equal(chat.render().messages.some(message => message.id.startsWith('local-uncertain-')), false);
});

test('checkpoint on load resolves done without replay; pending and unknown offer Work', async () => {
  for (const state of ['done', 'pending', 'unknown']) {
    const chat = checkpointChat(state); const raw = chat.save();
    await chat.render().reloadHistory();
    assert.equal(chat.calls.length, 0);
    assert.equal(chat.statusCalls.length, 1);
    if (state === 'done') assert.equal(chat.storage.size, 0);
    else {
      assert.equal(chat.storage.get(chat.checkpointKey), raw);
      const notice = chat.render().messages.at(-1);
      assert.equal(notice.actions[0].action, 'status');
      assert.deepEqual(notice.actions.map(action => action.label), state === 'pending' ? ['Show status'] : ['Show status', 'Start fresh']);
      if (state === 'pending') assert.equal(notice.body_md, 'Your earlier request is still running. Tap Show status for progress.');
      assert.doesNotMatch(notice.body_md, /resend|exact/i);
    }
  }
});

test('done checkpoint lets a different message through with a new nonce', async () => {
  const chat = checkpointChat('done'); chat.save();
  assert.equal(await chat.render().sendChat('retry research', {idempotencyKey: 'old-nonce'}), true);
  assert.equal(chat.calls.length, 1);
  assert.notEqual(chat.calls[0].idempotency_key, 'old-nonce');
  assert.equal(chat.storage.size, 0);
});

test('a checkpoint missing for two minutes fails with Retry without blocking new messages', async () => {
  for (const [ventureId, draft] of [['ven_dispatch', 'Find freight brokers'], ['ven_gardens', 'Find balcony gardeners']]) {
    const conversationDraftId = ventureId === 'ven_dispatch' ? 'cnv_owned' : null;
    const chat = checkpointChat('unknown', new Map(), true, ventureId, conversationDraftId);
    window.__SUPYMEM__ = {mode: 'local'};
    const digest = require('node:crypto').createHash('sha256').update(JSON.stringify({question_id: null, reply_to_question: false, source: null, text: draft})).digest('hex');
    const draftKey = `supymem.chatDraft.v1:usr_owned:${ventureId}${conversationDraftId ? `:${conversationDraftId}` : ''}`;
    chat.storage.set(draftKey, draft);
    chat.save({sent_at: Date.now() - 121_000, payload_digest: digest});
    await chat.render().reloadHistory();
    assert.ok(chat.storage.has(chat.checkpointKey), 'an ambiguous response is not proof the message was never sent');
    chat.setStatusReply(() => ({ok: true, data: {state: 'unknown', missing: true}}));
    chat.save({sent_at: Date.now() - 60_000, payload_digest: digest});
    await chat.render().reloadHistory();
    assert.ok(chat.storage.has(chat.checkpointKey), 'a recent request can still arrive');
    chat.save({sent_at: Date.now() - 121_000, payload_digest: digest});
    await chat.render().reloadHistory();
    const notice = chat.render().messages.at(-1);
    assert.match(notice.body_md, /not sent/i);
    assert.equal(chat.storage.has(chat.checkpointKey), false);
    assert.equal(await chat.render().sendChat(`Tell me about ${draft.toLowerCase()}`), true);
    assert.notEqual(chat.calls[0].idempotency_key, 'old-nonce');
    const retry = notice.actions.find(action => action.label === 'Retry');
    assert.equal(await chat.render().confirmChatAction(retry.action, retry.payload), true);
    assert.equal(chat.calls[1].message, draft);
    assert.equal(chat.calls[1].idempotency_key, 'old-nonce');
  }
});

test('a rejected Enter send shows recovery beside the preserved draft without scrolling', async () => {
  for (const [ventureId, draft, alreadyUncertain] of [['ven_freight', 'Find freight brokers', true], ['ven_balcony', 'Find balcony gardeners', false]]) {
    const chat = checkpointChat('unknown', new Map(), true, ventureId);
    window.__SUPYMEM__ = {mode: 'local'};
    if (alreadyUncertain) {
      chat.save();
      await chat.render().reloadHistory();
    } else chat.setPostReply({ok: false, status: 409, data: {detail: {error: {code: 'chat_action_failed'}}}});
    const dock = harness('components/chat/ChatDock.tsx', {'@/lib/usageControl': {announceUsageControl() {}}});
    let sending;
    let top = 100, refreshed = false;
    const scroller = {scrollHeight: 800, clientHeight: 400, get scrollTop() {return top;}, set scrollTop(value) {top = value;},
      getBoundingClientRect: () => ({top: 0}), querySelectorAll: () => []};
    const props = {subject: 'usr_owned', ventureId, conversationId: 'cnv_owned', historyReady: true,
      onSend: body => sending = chat.render().sendChat(body), onAction: (...args) => sending = chat.render().confirmChatAction(...args)};
    const bar = !alreadyUncertain ? harness('components/chat/ChatBar.tsx', {
      './ChatDock': {ChatDock: 'dock'}, '@/lib/usageControl': {announceUsageControl() {}},
    }, [true]) : null;
    const render = () => {
      const current = {...props, messages: [{id: 'previous', role: 'assistant', body_md: 'Your notes are saved.'}, ...chat.render().messages,
        ...(refreshed ? [{id: 'provider-error', role: 'assistant', body_md: 'Check your AI connection.'}] : [])]};
      const visible = bar ? nodes(bar.render(bar.exports.ChatBar, current)) : [];
      const thread = nodes(dock.render(dock.exports.ChatDock, bar ? visible.find(node => node.type === 'dock').props : current));
      const log = thread.find(node => node.props.role === 'log');
      log.props.ref.current = scroller;
      for (const layout of dock.layouts) layout();
      return [...visible, ...thread];
    };
    const field = () => render().find(node => node.type === 'textarea');
    const log = render().find(node => node.props.role === 'log');
    top = 100;
    log.props.onScroll({currentTarget: scroller});
    field().props.onChange(draft);
    field().props.onSubmit(draft);
    render();
    assert.equal(await sending, false);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(field().props.value, draft);
    assert.equal(top, 100);
    const priorPosts = alreadyUncertain ? 0 : 1;
    assert.equal(chat.calls.length, priorPosts);
    refreshed = !alreadyUncertain;
    const alert = render().find(node => node.props.role === 'alert');
    assert.match(label(alert), /earlier request may still finish.*Show status or start fresh/);
    assert.ok(chat.storage.has(chat.checkpointKey));
    chat.setPostReply(undefined);
    const recovery = bar ? render() : nodes(alert);
    assert.ok(recovery.some(node => node.type === 'button' && label(node) === 'Show status' && !node.props.disabled));
    const fresh = recovery.find(node => node.type === 'button' && label(node) === 'Start fresh');
    fresh.props.onClick();
    assert.equal(await sending, true);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(chat.calls[priorPosts].message, draft);
    assert.notEqual(chat.calls[priorPosts].idempotency_key, alreadyUncertain ? 'old-nonce' : chat.calls[0].idempotency_key);
    assert.equal(field().props.value, '');
    assert.equal(chat.storage.has(`supymem.chatDraft.v1:usr_owned:${ventureId}:cnv_owned`), false);
  }
});

test('checkpoint recovery actions render and run in the actual chat dock', async () => {
  const chat = checkpointChat(); chat.save();
  await chat.render().sendChat('retry research');
  const notice = chat.render().messages.at(-1), actions = [];
  const dock = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => ({draft: '', pending: false, setDraft() {}, clearError() {}}) } });
  const rendered = nodes(dock.render(dock.exports.ChatDock, {messages: [notice], onSend() {}, onAction: async (action, payload) => {actions.push(action); return chat.render().confirmChatAction(action, payload);} }));
  assert.ok(rendered.some(node => node.type === 'button' && label(node) === 'Show status'));
  const fresh = rendered.find(node => node.type === 'button' && label(node) === 'Start fresh');
  assert.ok(fresh);
  fresh.props.onClick();
  while (!chat.calls.length) await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(actions, ['chat.startFresh']);
  assert.equal(chat.calls[0].message, 'retry research');
});

test('pending checkpoint blocks even the exact original without replay or fresh action', async () => {
  const chat = checkpointChat('pending'); const checkpoint = JSON.parse(chat.save());
  checkpoint.payload_digest = require('node:crypto').createHash('sha256').update(JSON.stringify({question_id: null, reply_to_question: false, source: null, text: 'retry research'})).digest('hex');
  const raw = JSON.stringify(checkpoint); chat.storage.set(chat.checkpointKey, raw);
  assert.equal(await chat.render().sendChat('retry research'), false);
  assert.equal(chat.calls.length, 0);
  assert.equal(chat.storage.get(chat.checkpointKey), raw);
  assert.deepEqual(chat.render().messages.at(-1).actions.map(action => action.label), ['Show status']);
});

test('unknown checkpoint warns; your explicit Start fresh sends the blocked draft once without another approval', async () => {
  for (const domain of ['Warehouse planning', 'Family holidays']) {
    const chat = checkpointChat('unknown', new Map(), false); chat.save();
    const draft = `retry research for ${domain}`;
    assert.equal(await chat.render().sendChat(draft, {idempotencyKey: 'old-nonce'}), false);
    const notice = chat.render().messages.at(-1);
    assert.deepEqual(notice.actions.map(action => action.label), ['Show status', 'Start fresh']);
    assert.match(notice.body_md, /may still finish/);
    assert.match(notice.body_md, /won't cancel/);
    await chat.render().reloadHistory();
    assert.equal(chat.render().messages.at(-1).actions[1].payload.text, draft);
    const action = notice.actions[1];
    assert.equal(await chat.render().confirmChatAction(action.action, action.payload), true);
    assert.deepEqual(chat.confirmations, []);
    assert.equal(chat.calls.length, 1);
    assert.equal(chat.calls[0].message, draft);
    assert.notEqual(chat.calls[0].idempotency_key, 'old-nonce');
    assert.equal(await chat.render().confirmChatAction(action.action, action.payload), false);
    assert.equal(chat.calls.length, 1);
  }
});

test('server status resolves the retained in-memory attempt before a changed message', async () => {
  const chat = checkpointChat('unknown');
  chat.setPostReply({ok: false, status: 502});
  assert.equal(await chat.render().sendChat('retry research'), false);
  assert.equal(chat.calls.length, 1);
  const nonce = chat.calls[0].idempotency_key;
  chat.setPostReply(null); chat.setStatus('done');
  assert.equal(await chat.render().sendChat('retry research again'), true);
  assert.equal(chat.calls.length, 2);
  assert.notEqual(chat.calls[1].idempotency_key, nonce);
});

test('unreachable checkpoint status stays unknown with explicit recovery actions', async () => {
  const chat = checkpointChat(); chat.save();
  chat.setStatusReply(() => { throw new Error('offline'); });
  assert.equal(await chat.render().sendChat('retry research'), false);
  assert.equal(chat.calls.length, 0);
  const action = chat.render().messages.at(-1).actions[1];
  assert.equal(await chat.render().confirmChatAction(action.action, action.payload), true);
  assert.equal(chat.calls.length, 1);
});

test('retired scope or a replaced checkpoint cannot be cleared by an old status response', async () => {
  for (const replace of [false, true]) {
    const chat = checkpointChat('done'); const raw = chat.save(); let finish;
    chat.setStatusReply(() => new Promise(resolve => {finish = resolve;}));
    const pending = chat.render().sendChat('retry research');
    while (!finish) await new Promise(resolve => setImmediate(resolve));
    if (replace) chat.storage.set(chat.checkpointKey, raw.replace('old-nonce', 'new-nonce'));
    else chat.retire();
    const expected = chat.storage.get(chat.checkpointKey);
    finish({ok: true, data: {state: 'done'}});
    assert.equal(await pending, false);
    assert.equal(chat.calls.length, 0);
    assert.equal(chat.storage.get(chat.checkpointKey), expected);
  }
});

test('retained chat focuses its composer on each open, never while hidden, without scrolling', () => {
  setup();
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => ({ draft: '', pending: false, setDraft() {}, clearError() {} }) } });
  let focused = 0;
  for (const visible of [false, true, false, true]) {
    const rendered = nodes(h.render(h.exports.ChatDock, { messages: [], onSend() {}, resizable: true, visible }));
    const composer = rendered.find(node => node.props?.inputRef);
    composer.props.inputRef.current = { focus(options) { assert.equal(options.preventScroll, true); focused++; } };
    const effect = h.effects.find(callback => callback.toString().includes('!resizable'));
    assert.ok(effect);
    const previous = focused;
    effect();
    assert.equal(focused, previous + Number(visible));
  }
  assert.equal(focused, 2);
});

test('founder chat bubbles show onboarding words on readable lines, not intake labels', () => {
  setup();
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => ({ draft: '', pending: false, setDraft() {}, clearError() {} }) } });
  const all = nodes(h.render(h.exports.ChatDock, { messages: [{ id: 'answer', role: 'user', body_md: 'Coffee subscriptions\nAudience: local drinkers\nClarification: We have paying customers' }], onSend() {} }));
  const prose = all.filter(node => node.props?.dangerouslySetInnerHTML).map(node => node.props.dangerouslySetInnerHTML.__html).join('');
  assert.match(prose, /Coffee subscriptions/);
  assert.match(prose, /local drinkers/);
  assert.match(prose, /We have paying customers/);
  assert.doesNotMatch(prose, /Audience:|Clarification:/);
});

test('empty conversation updates and composer focus do not reset your scroll', async () => {
  setup(); const requests = [], focused = [];
  const h = harness('app/new/page.tsx', {
    'next/navigation': { useRouter: () => ({ replace() {} }) },
    '@/components/layout/AppShell': { AppShell: 'shell' }, './ConfirmedConversation': {},
    '@/components/ventures/BetaPathStrip': {}, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
    '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
      api: async (url, options) => { requests.push({ url, options }); return { ok: true, data: { status: 'pending', id: 'proposal', route: 'startup', message: 'Review your idea' } }; } },
    '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': { DictateButton: 'dictate' },
  }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
  const render = () => nodes(h.render(h.exports.Conversation, {}));
  let all = render(); h.state[12] = true; all = render();
  const thread = { scrollTop: 140, scrollHeight: 800 };
  all.find(node => node.props?.ref && node.props.className === 'messages').props.ref.current = thread;
  all.find(node => node.type === 'textarea').props.ref.current = { focus: options => focused.push(options) };
  h.effects[1](); h.effects.find(effect => effect.toString().includes('scrollDecision'))();
  assert.equal(thread.scrollTop, 140);
  assert.deepEqual(focused, [{ preventScroll: true }]);
});

test('website name is a confirmable chip and summary corrections share one Continue', async () => {
  setup(); const requests = [], proposal = { id: 'site-proposal', route: 'gtm', state: 'pending', message: 'Your website is saved',
    venture_name: 'Slotwise', website_answers: {offer: 'Lesson bookings', customer: 'Independent tutors'},
    skipped_steps: [], next_step: 'gtm', expires_at: '2099-01-01T00:00:00Z' };
  const receipt = {status: 'committed', route: 'gtm', venture_id: 'ven_site', workspace_id: 'ws_site', next_step: 'gtm',
    action: {kind: 'gtm_review', venture_id: 'ven_site', href: '/gtm?venture=ven_site'}, message: 'Saved'};
  const h = harness('app/new/page.tsx', {
    'next/navigation': { useRouter: () => ({ replace() {} }) }, '@/components/layout/AppShell': {AppShell: 'shell'},
    './ConfirmedConversation': {}, '@/components/ventures/BetaPathStrip': {}, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
    '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ok: true}),
      api: async (url, options) => { if (url.endsWith('/history')) return {ok: true, data: {items: []}}; requests.push({url, options}); return {ok: true, data: url.endsWith('/preview') ? {status:'proposal', proposal} : url.includes('/proposals/') ? {proposal} : receipt}; } },
    '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': {DictateButton: 'dictate'},
  }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
  const render = () => nodes(h.render(h.exports.Conversation, {}));
  render(); h.effects[2](); await new Promise(resolve => setImmediate(resolve));
  render().find(node => node.type === 'textarea').props.onChange({target: {value: 'https://slotwise.example'}});
  render().find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  let all = render(); const name = all.find(node => node.props?.id === 'entry-message');
  assert.equal(name.props.value, ''); assert.equal(name.props.disabled, false);
  assert.ok(all.some(node => node.type === 'button' && label(node) === 'Slotwise'));
  assert.ok(all.some(node => node.type === 'summary' && label(node).includes('From your website')));
  assert.equal(all.find(node => node.props?.['data-usage-control'] === 'onboarding_continue').props.disabled, false);
  name.props.onChange({target: {value: 'Lesson Loop'}});
  all.find(node => node.props?.id === 'website-customer').props.onChange({target: {value: 'Studio managers'}});
  all = render(); all.find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  const body = JSON.parse(requests.find(request => request.url.endsWith('/confirm')).options.body);
  assert.equal(body.name, 'Lesson Loop'); assert.deepEqual(body.website_answers, {customer: 'Studio managers'});
});

test('new clarification follows only bottom readers while history readers keep their place', () => {
  setup(); const starts = [], selector = [], composer = { draft: '', pending: false, setDraft() {}, clearError() {} };
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer } });
  const props = { hideComposer: true, messages: [{ id: 'own', role: 'user', body_md: 'My idea' }], onSend() {} };
  const list = { scrollTop: 0, scrollHeight: 1200, clientHeight: 400, getBoundingClientRect: () => ({top: 0}), querySelectorAll: () => [], querySelector: value => { selector.push(value); return { scrollIntoView: options => starts.push(options) }; } };
  const render = () => {
    const all = nodes(h.render(h.exports.ChatDock, props));
    all.find(node => node.props?.ref && node.props.className === 'messages').props.ref.current = list;
    h.layouts[0](); return all;
  };
  render(); assert.equal(list.scrollTop, 1200);
  props.messages = [...props.messages, { id: 'question', role: 'assistant', kind: 'idea_clarification_ready', body_md: 'Who uses it?',
    actions: [{ label: 'Clinics', action: 'chat.reply', payload: { question_id: 'q1', text: 'Clinics' } }] }];
  let all = render(); assert.equal(list.scrollTop, 1200); assert.deepEqual(starts, []); assert.deepEqual(selector, []);
  assert.equal(all.filter(node => node.props?.['data-latest-message']).length, 1);
  render(); assert.equal(starts.length, 0, 'polls do not reposition the same question');
  list.scrollTop = 100;
  all.find(node => node.props?.ref && node.props.className === 'messages').props.onScroll({ currentTarget: list });
  props.messages = [...props.messages, { id: 'another-question', role: 'assistant', kind: 'idea_clarification_ready', body_md: 'What is the problem?' }];
  render(); assert.equal(starts.length, 0); assert.equal(list.scrollTop, 100);
});

test('sidebar suggestions use its selected venture and count only new cards', async () => {
  setup(); const paths = [], callbacks = new Map();
  window.setInterval = () => 1; window.clearInterval = () => {};
  window.addEventListener = (name, callback) => callbacks.set(name, callback);
  window.removeEventListener = name => callbacks.delete(name);
  const nav = harness('components/layout/SideNav.tsx', { '@/components/guide/GuideNavLink': {} });
  const sections = nodes(nav.render(nav.exports.SideNav, { pathname: '/new', ventureId: 'clinic / UK' })).filter(node => node.props?.group);
  const links = sections.flatMap(section => nodes(section.type(section.props))).filter(node => node.props?.item?.badge === 'suggestions');
  const badge = nodes(links[0].type(links[0].props)).find(node => node.type?.name === 'SuggestionsBadge');
  assert.equal(badge.props.ventureId, 'clinic / UK', 'the shell venture, not just a /v path, owns the badge');
  const h = harness('components/layout/SuggestionsBadge.tsx', { '@/lib/client': { api: async path => { paths.push(path); return { ok: true, data: { new: 2, saved: 9, items: [] } }; } } });
  h.render(h.exports.SuggestionsBadge, badge.props); const cleanup = h.effects[0](); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(paths, ['/api/suggestions?venture_id=clinic+%2F+UK']);
  const rendered = h.render(h.exports.SuggestionsBadge, badge.props);
  assert.equal(label(rendered), '2'); assert.match(rendered.props['aria-label'], /^2 waiting in this venture$/);
  cleanup(); assert.equal(callbacks.size, 0);
});
for (const confirmation of [false, true]) {
  test(`one-shot chat action locks immediately, retries failure, and stays settled (confirmation=${confirmation})`, async () => {
    setup(); let finish, calls = 0, fail = true;
    const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => ({ draft: '', pending: false, setDraft() {}, clearError() {} }) } });
    const action = { label: confirmation ? 'Looks right' : 'Continue to research', action: 'idea.confirm',
      ...(confirmation ? { confirmation: { action: 'idea.confirm', resulting_state: { session_id: 'idea1' } } } : { payload: { session_id: 'idea1' } }) };
    const props = { hideComposer: true, messages: [{ id: 'summary', role: 'assistant', body_md: 'Your idea', actions: [action] }], onSend() {},
      onAction: () => { calls++; return new Promise((resolve, reject) => { finish = fail ? reject : resolve; }); } };
    const render = () => nodes(h.render(h.exports.ChatDock, props));
    const button = () => render().find(node => node.type === 'button' && node.props['data-usage-control'] === 'chat_followup_action');
    let click = button(); click.props.onClick(); click.props.onClick();
    assert.equal(calls, 1); assert.equal(button().props.disabled, true); assert.equal(button().props['aria-busy'], true); assert.equal(label(button()), 'Starting…');
    finish(new Error('Research service is unavailable.')); await new Promise(resolve => setImmediate(resolve));
    assert.equal(button().props.disabled, false); assert.equal(button().props['aria-busy'], false); assert.equal(label(button()), action.label);
    assert.ok(render().some(node => node.type === 'error' && node.props.error === 'Research service is unavailable.'));
    fail = false; click = button(); click.props.onClick(); click.props.onClick(); assert.equal(calls, 2);
    finish(true); await new Promise(resolve => setImmediate(resolve));
    assert.equal(button().props.disabled, true); assert.equal(button().props['aria-busy'], false); assert.equal(label(button()), action.label);
    click.props.onClick(); assert.equal(calls, 2);
  });
}

test('Connect Gmail opens once per authorization tap and never on chat replay', async () => {
  for (const [ventureId, idea] of [['ven_billing', 'Invoice reconciliation for purchasing teams'],
    ['ven_meals', 'Meal planning for busy households']]) for (const native of [true, false]) {
    setup(); window.location.origin = 'https://app.supymem.com';
    const opened = [], listeners = new Set(), previousDocument = global.document;
    const desktop = { isDesktopShell: () => native,
      desktopInvoke: async (command, args) => opened.push({ command, args }),
      desktopInvokeOrThrow: async (command, args) => opened.push({ command, args }) };
    global.document = {
      addEventListener(type, listener, capture = false) { if (type === 'click') listeners.add({ listener, capture }); },
      removeEventListener(type, listener) { for (const entry of listeners) if (entry.listener === listener) listeners.delete(entry); },
    };
    const boot = harness('components/desktop/DesktopBoot.tsx', { '@/lib/desktop': desktop }, [native]);
    const dock = harness('components/chat/ChatDock.tsx', { '@/lib/desktop': desktop,
      './useChatComposer': { useChatComposer: () => ({ draft: '', pending: false, setDraft() {}, clearError() {} }) } });
    const url = 'https://connect.composio.dev/link/gmail-request';
    const reply = { id: 'gmail-request', role: 'assistant', kind: 'navigation', body_md: 'Approve Gmail in your browser.',
      actions: [{ label: 'Connect Gmail', href: url, style: 'primary' }] };
    let confirms = 0, bootCleanup, dockCleanups = [];
    const props = { ventureId, hideComposer: true, messages: [{ id: 'connect-action', role: 'assistant', body_md: `${idea}. Connect your mailbox.`,
      actions: [{ label: 'Connect Gmail', action: 'connections.connect', payload: { query: 'connect gmail' } }] }], onSend() {},
      onAction: async (action, payload) => {
        assert.equal(action, 'connections.connect'); assert.deepEqual(payload, { query: 'connect gmail', reply_to_message_id: 'connect-action' });
        confirms++; props.messages = [...props.messages, reply];
      } };
    const render = () => {
      bootCleanup?.(); boot.render(boot.exports.DesktopBoot); bootCleanup = boot.effects[2]();
      dockCleanups.forEach(cleanup => cleanup());
      const rendered = nodes(dock.render(dock.exports.ChatDock, props));
      dockCleanups = [...dock.effects, ...dock.layouts].map(effect => effect()).filter(cleanup => typeof cleanup === 'function');
      return rendered;
    };
    const tap = link => {
      const event = { defaultPrevented: false, target: { closest: () => ({ getAttribute: () => link.props.href }) },
        preventDefault() { this.defaultPrevented = true; } };
      for (const entry of listeners) if (entry.capture) entry.listener(event);
      link.props.onClick?.(event);
      for (const entry of listeners) if (!entry.capture) entry.listener(event);
      if (!event.defaultPrevented) opened.push({ command: 'browser', args: { path: link.props.href } });
    };
    try {
      const button = render().find(node => node.type === 'button' && label(node) === 'Connect Gmail');
      button.props.onClick(); button.props.onClick(); await new Promise(resolve => setImmediate(resolve));
      assert.equal(confirms, 1); assert.equal(opened.length, 0);
      const authorization = () => render().find(node => node.type === 'a' && node.props.href === url);
      tap(authorization());
      assert.deepEqual(opened, [{ command: native ? 'open_composio_connect_link' : 'browser', args: native ? { url } : { path: url } }]);
      for (let poll = 0; poll < 3; poll++) { props.messages = props.messages.map(message => ({ ...message })); render(); }
      assert.equal(confirms, 1);
      assert.equal(opened.length, 1);
      tap(authorization()); assert.equal(opened.length, 2);
      render(); assert.equal(opened.length, 2);
      const ordinaryUrl = 'https://accounts.google.com/o/oauth2/auth';
      props.messages = [{ ...reply, actions: [{ label: 'Authorize', href: ordinaryUrl }] }];
      tap(render().find(node => node.type === 'a' && node.props.href === ordinaryUrl));
      assert.deepEqual(opened.at(-1), { command: native ? 'plugin:shell|open' : 'browser', args: { path: ordinaryUrl } });
      assert.equal(opened.length, 3);
    } finally {
      bootCleanup?.(); dockCleanups.forEach(cleanup => cleanup()); global.document = previousDocument;
    }
    assert.equal(listeners.size, 0);
  }
});

test('handled false and synchronous failure release one-shot actions with one clear message across domains', async () => {
  for (const domain of ['Dental equipment servicing', 'Pottery sessions for retirees']) {
    for (const result of [false, new Error('You can try again in a moment.')]) {
      setup();
      const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => ({ draft: '', pending: false, setDraft() {}, clearError() {} }) } });
      const reason = 'You can check your saved conversation and try again.';
      const props = { hideComposer: true, messages: [{ id: 'summary', role: 'assistant', body_md: domain, actions: [{ label: 'Looks right', action: 'idea.confirm' }] }], onSend() {},
        onAction: () => {
          if (result instanceof Error) throw result;
          props.messages = [...props.messages, {id: 'failed', role: 'system', body_md: reason}];
          return result;
        } };
      const render = () => nodes(h.render(h.exports.ChatDock, props));
      render().find(node => node.type === 'button' && label(node) === 'Looks right').props.onClick(); await new Promise(resolve => setImmediate(resolve));
      assert.equal(render().find(node => node.type === 'button' && label(node) === 'Looks right').props.disabled, false);
      if (result instanceof Error) {
        assert.equal(render().filter(node => node.type === 'error' && node.props.error === result.message).length, 1);
      } else {
        assert.equal(render().some(node => node.type === 'error' && node.props.error), false);
        assert.equal(render().filter(node => node.type === 'div' && [node.props.children].flat().includes(reason)).length, 1);
      }
    }
  }
});

test('summary and approval bubbles render attached founder references without fetching', () => {
  setup();
  const references = { startups: [], yc_advice: [{id: 'yc-a', title: 'Talk to users', line: 'Watch a workflow', url: 'https://youtube.com/watch?v=a'}] };
  const h = harness('components/chat/ChatDock.tsx', {
    './useChatComposer': {useChatComposer: () => ({draft: '', pending: false, setDraft() {}, clearError() {}})},
    './IdeaReferences': {IdeaReferences: 'founder-references'},
  });
  const messages = ['idea_clarification_ready', 'idea_clarification_pending'].map((kind, index) =>
    ({id: `summary-${index}`, role: 'assistant', kind, body_md: 'Your idea summary', founder_references: references}));
  messages.push({id: 'founder', role: 'user', body_md: 'Looks right', founder_references: references});
  const cards = nodes(h.render(h.exports.ChatDock, {messages, onSend() {}})).filter(node => node.type === 'founder-references');
  assert.equal(cards.length, 2);
  assert.ok(cards.every(node => node.props.references === references));
});

test('chat composer groups attachment controls without rewriting its draft', async () => {
  setup();
  const composer = { draft: '', pending: false, setDraft: next => { composer.draft = next; }, clearError() {} };
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer } });
  const render = () => nodes(h.render(h.exports.ChatDock, { messages: [], onSend() {}, ventureId: 'venture1', onVoiceSend() {} }));
  const row = render().find(node => node.props?.className === 'composerActions'), groups = row.props.children;
  assert.equal(groups[0].props.className, 'composerTools'); assert.equal(groups[1].props.className, 'composerTools');
  const attachment = nodes(groups[0]).find(node => typeof node.type === 'function' && node.type.name === 'IntakeAttachments');
  assert.ok(attachment);
  assert.ok(nodes(groups[0]).some(node => node.type === 'voice')); assert.ok(nodes(groups[1]).some(node => node.type === 'dictate')); assert.ok(nodes(groups[1]).some(node => label(node) === 'Send'));
  const block = nodes(attachment.type(attachment.props));
  const button = block.find(node => typeof node.type === 'function' && node.type.name === 'AttachFileButton');
  const rendered = button.type(button.props);
  assert.equal(rendered.props['aria-label'], 'Attach files');
  assert.ok(block.some(node => node.type === 'input' && node.props.multiple));
  assert.ok(block.some(node => node.type === 'input' && 'webkitdirectory' in node.props));
  assert.equal(composer.draft, '');
});

test('multi-file attachments show reading, read and skip reasons, preserve paths, and remove independently', async () => {
  setup(); const calls = []; let finish;
  const h = harness('components/chat/IntakeAttachments.tsx', {
    '@/lib/client': { verifiedSessionEpoch: () => 1, verifiedSessionSubject: () => 'founder',
      authenticatedFetch: async (path, options) => {
        calls.push({path, options});
        if (calls.length === 1) await new Promise(resolve => { finish = resolve; });
        return { ok: true, json: async () => ({files: [{source_label: String(options.body.get('source_labels')),
          status: calls.length === 1 ? 'read' : 'skipped', ...(calls.length === 1 ? {evidence_id: 'ev_saved'} : {note: 'Image skipped. Share text instead.'})}]}) };
      }, api: async () => ({ok: true}) },
  });
  const render = () => h.render(h.exports.useIntakeAttachments, 'scope');
  const pending = render().add([{file: new File(['notes'], 'brief.md'), name: 'folder/brief.md'},
    {file: new File(['image'], 'logo.png'), name: 'folder/logo.png'}]);
  assert.deepEqual(render().files.map(file => file.status), ['reading', 'reading']);
  assert.equal(calls[0].path, '/api/entry/evidence/files');
  assert.equal(calls[0].options.body.get('source_labels'), 'folder/brief.md');
  finish(); await pending;
  assert.deepEqual(render().files.map(file => file.status), ['read', 'skipped']);
  assert.deepEqual(render().evidenceIds, ['ev_saved']);
  const view = h.render(h.exports.IntakeAttachments, {attachments: render()});
  assert.match(label(view), /folder\/logo.png.*skipped.*Share text/);
  const remove = nodes(view).find(node => node.props?.['aria-label'] === 'Remove folder/brief.md');
  remove.props.onClick(); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(render().evidenceIds, []);
  assert.equal(render().files[0].source_label, 'folder/logo.png');
});

test('removing a venture file during reading deletes its eventual saved evidence', async () => {
  setup(); let finish; const deleted = [];
  const h = harness('components/chat/IntakeAttachments.tsx', {
    '@/lib/client': { verifiedSessionEpoch: () => 1, verifiedSessionSubject: () => 'founder',
      authenticatedFetch: async () => { await new Promise(resolve => {finish = resolve;});
        return {ok: true, json: async () => ({files: [{source_label: 'notes.txt', status: 'read', evidence_id: 'ev_late'}]})}; },
      api: async (path, options) => {deleted.push([path, options.method]); return {ok: true};} },
  });
  const render = () => h.render(() => h.exports.useIntakeAttachments('scope', 'ven_one'), {});
  const pending = render().add([{file: new File(['notes'], 'notes.txt'), name: 'notes.txt'}]);
  await render().remove(render().files[0].id);
  finish(); await pending;
  assert.equal(render().files.length, 0);
  assert.deepEqual(deleted, [['/api/entry/evidence/files/ev_late?venture_id=ven_one', 'DELETE']]);
});
for (const streaming of [false, true]) {
  test(`Charlie shows plain long-wait text after four seconds (${streaming ? 'typing' : 'sending'})`, () => {
    setup();
    const nativeTimer = global.setTimeout, nativeClear = global.clearTimeout, timers = [];
    global.setTimeout = (run, delay) => { const timer = { run, delay, cleared: false }; timers.push(timer); return timer; };
    global.clearTimeout = timer => { timer.cleared = true; };
    let cleanup;
    try {
      const composer = { draft: '', pending: !streaming, setDraft() {}, clearError() {} };
      const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer } });
      const props = { messages: [], onSend() {}, ventureId: 'wait-one',
        streaming: streaming ? { messageId: 'reply-one', speaker: 'Charlie', text: '' } : undefined };
      const render = () => nodes(h.render(h.exports.ChatDock, props));
      assert.equal(render().some(node => label(node) === 'Thinking a little longer…'), false);
      cleanup = h.effects.find(effect => effect.toString().includes('setLongWaitKey'))();
      assert.equal(timers[0].delay, 4000);
      timers[0].run();
      const notice = render().find(node => label(node) === 'Thinking a little longer…' && node.props.role === 'status');
      assert.ok(notice); assert.equal(notice.props['aria-hidden'], undefined);
      assert.equal(/credit|token|\d/i.test(label(notice)), false);
      composer.pending = false;
      props.streaming = { messageId: 'reply-one', speaker: 'Charlie', text: '' };
      assert.equal(render().some(node => label(node) === 'Thinking a little longer…'), true);
      cleanup(); props.ventureId = 'wait-two';
      assert.equal(render().some(node => label(node) === 'Thinking a little longer…'), false);
      cleanup = h.effects.find(effect => effect.toString().includes('setLongWaitKey'))(); cleanup();
      composer.pending = false;
      if (props.streaming) props.streaming = { ...props.streaming, text: 'The reply arrived.' };
      timers.at(-1).run();
      assert.equal(render().some(node => label(node) === 'Thinking a little longer…'), false);
      cleanup = h.effects.find(effect => effect.toString().includes('setLongWaitKey'))();
    } finally { cleanup?.(); global.setTimeout = nativeTimer; global.clearTimeout = nativeClear; }
  });
}
test('first-venture fallback has one idea action and no legacy journey cards', () => {
  setup(); const h = harness('components/ventures/VentureHub.tsx', { '@/lib/client': {}, './DeleteVentureDialog': {}, './VentureCard': {}, './BetaPathStrip': { BetaPathStrip: 'steps' } }, [false, [], [], 7, false]);
  const all = nodes(h.render(h.exports.VentureHub)), start = all.findIndex(n => n.props?.['data-usage-control'] === 'onboarding_start'), strip = all.findIndex(n => n.type === 'steps');
  assert.ok(start >= 0); assert.equal(strip, -1); assert.equal(label(all[start]), 'What is your idea?→');
});

test('zero ventures redirects after a successful load, never on a failed list', async () => {
  for (const failedList of [null, '/api/ventures?summary=true', '/api/ventures/deleted?summary=true']) {
    setup(); const paths = [];
    const h = harness('components/ventures/VentureHub.tsx', {
      'next/navigation': {useRouter: () => ({replace: path => paths.push(path)})},
      './DeleteVentureDialog': {}, './VentureCard': {},
      '@/lib/desktop': {isDesktopShell: () => false},
      '@/lib/client': {ensureSession: async () => ({}), verifiedSessionSubject: () => null, verifiedSessionEpoch: () => 1,
        apiSwr: async (url, update) => update({ok: url !== failedList, data: {items: []}})},
    });
    h.render(h.exports.VentureHub); for (const effect of [...h.effects]) effect();
    await new Promise(resolve => setImmediate(resolve));
    h.effects.length = 0; h.render(h.exports.VentureHub); h.effects[1]();
    assert.deepEqual(paths, failedList ? [] : ['/new']);
  }
});

test('blank confirmed direction never renders Question N and refetches saved text', async () => {
  setup(); const calls = [];
  const chat = {conversationKey: 'chat', contextCurrent: () => true, messages: [], reloadHistory: async () => {}, sendChat: async () => true};
  const h = harness('app/new/ConfirmedConversation.tsx', {
    '@/components/chat/ChatDock': {ChatDock: 'chat'}, '@/components/chat/useVentureChat': {useVentureChat: () => chat},
    '@/lib/client': {api: async url => {calls.push(url); return {ok: true, data: url.endsWith('/history')
      ? {items: [{text: 'Change direction', proposal: {id: 'blank', state: 'clarify', message: 'Who needs collision repair estimates?', choices: ['Body shops']}}]}
      : {status: 'clarify', id: 'blank', question: '   ', choices: ['Body shops']}};}},
  });
  const props = {receipt: {status: 'committed', route: 'startup', venture_id: 'venture-one', workspace_id: 'workspace-one', next_step: 'idea', action: {kind: 'idea', venture_id: 'venture-one', href: '/v/venture-one/idea'}}};
  const render = () => nodes(h.render(h.exports.ConfirmedConversation, props));
  assert.equal(await render().find(node => node.type === 'chat').props.onSend('Change direction'), false);
  let all = render();
  assert.equal(all.some(node => node.type === 'p' && /^Question \d+$/.test(label(node))), false);
  assert.equal(all.find(node => node.type === 'chat').props.messages.some(message => !message.body_md?.trim()), false);
  all.find(node => node.type === 'button' && label(node) === 'Ask me the questions again').props.onClick();
  await new Promise(resolve => setImmediate(resolve)); all = render();
  assert.equal(all.find(node => node.type === 'chat').props.messages.at(-1).body_md, 'Who needs collision repair estimates?');
  assert.deepEqual(calls, ['/api/entry/chat/preview', '/api/entry/chat/history']);
});

test('blank chat question retains a working refetch instead of numbered empty choices', () => {
  setup(); let reads = 0;
  const composer = {draft: '', pending: false, setDraft() {}, clearError() {}};
  const h = harness('components/chat/ChatDock.tsx', {'./useChatComposer': {useChatComposer: () => composer}});
  const all = nodes(h.render(h.exports.ChatDock, {ventureId: 'venture-one', messages: [{id: 'blank', role: 'assistant', body_md: ' ', actions: [{label: 'Shops', action: 'chat.reply', payload: {question_id: 'blank', text: 'Shops'}}]}], onReloadHistory: () => reads++}));
  assert.equal(all.some(node => node.props?.['aria-label'] === 'Answer options'), false);
  all.find(node => node.type === 'button' && label(node) === 'Ask me the questions again').props.onClick();
  assert.equal(reads, 1);
  const freeText = nodes(h.render(h.exports.ChatDock, {ventureId: 'venture-one', messages: [{id: 'blank-free-text', role: 'assistant', kind: 'idea_clarification_ready', body_md: ''}], onReloadHistory: () => reads++}));
  freeText.find(node => node.type === 'button' && label(node) === 'Ask me the questions again').props.onClick();
  assert.equal(reads, 2);
});

test('venture hub resumes only pending or clarify history after the last commit', async () => {
  for (const [states, progress] of [[[], false], [['pending'], true], [['clarify'], true], [['pending', 'committed'], false], [['committed', 'pending'], true], [['committed', 'cancelled'], false]]) {
    setup(); const calls = [];
    const h = harness('components/ventures/VentureHub.tsx', {
      './DeleteVentureDialog': {}, './VentureCard': {VentureCard: 'venture'}, './BetaPathStrip': {BetaPathStrip: 'beta'},
      '@/lib/client': {ensureSession: async () => ({ok: true}), verifiedSessionSubject: () => 'usr_one',
        apiSwr: async (url, update) => update({ok: true, data: {items: []}}),
        api: async url => {calls.push(url); return {ok: true, data: {items: states.map((state, index) => ({text: `answer ${index}`, proposal: {state}}))}};}},
    });
    const render = () => nodes(h.render(h.exports.VentureHub, {}));
    render(); for (const effect of h.effects) effect(); await new Promise(resolve => setImmediate(resolve));
    const all = render();
    assert.deepEqual(calls, ['/api/entry/chat/history']);
    const card = all.find(node => node.props?.['aria-label'] === 'Continue where you left off');
    assert.equal(card.props.style.visibility === 'visible', progress);  // the slot is reserved so tiles don't move
    if (progress) assert.equal(nodes(card).find(node => node.type === 'a').props.href, '/new');
    assert.ok(all.some(node => label(node) === 'Share your idea to find your first customers.'));
    if (progress) assert.equal(label(nodes(card).find(node => node.type === 'a')), 'Continue your draft→');
  }
});

test('shared composer restores local drafts on relaunch and keeps sends until acknowledgement', async () => {
  setup(); const values = new Map();
  window.localStorage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
  window.addEventListener = window.removeEventListener = () => {};
  const extra = {'@/lib/usageControl': {announceUsageControl() {}}};
  const create = send => {
    const h = harness('components/chat/useChatComposer.ts', extra);
    const render = key => h.render(() => h.exports.useChatComposer(send, key), {});
    render('usr_one:ven_one'); for (const effect of h.effects) effect();
    return {h, render};
  };
  const first = create(async () => true);
  first.render('usr_one:ven_one').setDraft('Unsent chat text');
  let finish;
  const reopened = create(() => new Promise(resolve => {finish = resolve;}));
  assert.equal(reopened.render('usr_one:ven_one').draft, 'Unsent chat text');
  const sending = reopened.render('usr_one:ven_one').submit();
  assert.equal(values.get('usr_one:ven_one'), 'Unsent chat text');
  reopened.render('usr_one:ven_one').setDraft('New typing'); finish(true); await sending;
  assert.equal(values.get('usr_one:ven_one'), 'New typing');
  const accepted = create(async () => true); await accepted.render('usr_one:ven_one').submit();
  assert.equal(values.has('usr_one:ven_one'), false);
  accepted.render('usr_one:ven_one').setDraft('Another draft');
  accepted.render('usr_two:ven_one'); for (const effect of accepted.h.layouts) effect();
  assert.equal(accepted.render('usr_two:ven_one').draft, '');
  assert.equal(values.get('usr_one:ven_one'), 'Another draft');
  const rejected = create(async () => false); await rejected.render('usr_one:ven_one').submit();
  assert.equal(rejected.render('usr_one:ven_one').draft, 'Another draft');
  assert.equal(values.get('usr_one:ven_one'), 'Another draft');
});

test('restored failed sends select the rendered text once in both composers across domains', async () => {
  for (const body of ['Summarize invoice exceptions for dental suppliers', 'Plan a quiet weekend of birdwatching']) {
    for (const component of ['ChatBar', 'ChatDock']) {
      for (const outcome of [false, {ok: false}, new Error('Unconfirmed request')]) {
        for (const newerDraft of ['', 'Keep this separate request']) {
          setup(); let finish, selections = 0;
          const input = {value: '', selectionStart: 4, selectionEnd: 4, focus() {}, select() {
            assert.equal(this.value, body, 'selection waits for the restored value to render');
            this.selectionStart = 0; this.selectionEnd = this.value.length; selections++;
          }};
          const onSend = () => new Promise((resolve, reject) => {finish = () => outcome instanceof Error ? reject(outcome) : resolve(outcome);});
          const h = harness(`components/chat/${component}.tsx`, {'@/lib/usageControl': {announceUsageControl() {}}});
          const render = () => {
            const field = nodes(h.render(h.exports[component], {messages: [], ventureId: 'ven_selection', onSend})).find(node => node.type === 'textarea');
            input.value = field.props.value;
            if (field.props.inputRef) field.props.inputRef.current = input;
            for (const layout of h.layouts) layout();
            return field;
          };
          render().props.onChange(body);
          const sending = render().props.onSubmit(body);
          assert.equal(render().props.value, '');
          if (newerDraft) { render().props.onChange(newerDraft); render(); }
          finish(); await sending; await new Promise(resolve => setImmediate(resolve));
          assert.equal(selections, 0, 'selection does not run before the render');
          assert.equal(render().props.value, newerDraft || body);
          assert.equal(selections, newerDraft ? 0 : 1);
          render();
          assert.equal(selections, newerDraft ? 0 : 1, 'ordinary renders do not select again');
          if (!newerDraft) {
            const replacement = 'Try a different approach';
            const next = input.value.slice(0, input.selectionStart) + replacement + input.value.slice(input.selectionEnd);
            render().props.onChange(next);
            assert.equal(render().props.value, replacement, 'typing replaces the restored text');
            assert.equal(selections, 1);
          }
        }
      }
    }
  }
});

test('first-open ChatDock keeps typing through identity, saved chat discovery, and remount before sending', async () => {
  setup(); const values = new Map(), sent = [];
  window.localStorage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
  window.addEventListener = window.removeEventListener = () => {};
  const extra = {'@/lib/usageControl': {announceUsageControl() {}}};
  const props = {messages: [], onSend: async body => {sent.push(body); return true;}, ventureId: 'ven_one', historyReady: false};
  let h = harness('components/chat/ChatDock.tsx', extra);
  const render = () => nodes(h.render(h.exports.ChatDock, props));
  const change = value => render().find(node => node.type === 'textarea').props.onChange(value);
  const part1 = 'A booking tool for dog walkers priced at $', part2 = '8/month.';
  render(); change(part1);
  props.subject = 'usr_one'; render(); for (const run of h.layouts) run();
  assert.equal(render().find(node => node.type === 'textarea').props.value, part1);
  const sharedKey = [...values.keys()][0];
  assert.equal(values.get(sharedKey), part1, 'late verification durably adopts pre-session typing');
  props.conversationId = 'private_one'; render(); for (const run of h.layouts) run();
  assert.equal(render().find(node => node.type === 'textarea').props.value, part1);
  const privateKey = `${sharedKey}:private_one`;
  assert.equal(values.get(privateKey), part1, 'first saved-chat discovery adopts only the live draft');
  assert.equal(values.has(sharedKey), false);
  props.historyReady = true;
  h = harness('components/chat/ChatDock.tsx', extra);
  assert.equal(render().find(node => node.type === 'textarea').props.value, part1, 'remount restores before any effects');
  change(part1 + part2);
  assert.equal(values.get(privateKey), part1 + part2, 'each edit is synchronous');
  await render().find(node => node.type === 'textarea').props.onSubmit(part1 + part2);
  assert.deepEqual(sent, [part1 + part2]);
  assert.equal(values.has(privateKey), false);
});

test('shared composer refuses a send while its new scope has not restored, and never migrates a different owner', async () => {
  setup(); const values = new Map(), sent = [];
  window.localStorage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
  const h = harness('components/chat/useChatComposer.ts', {'@/lib/usageControl': {announceUsageControl() {}}});
  const render = (key, loading = false) => h.render(() => h.exports.useChatComposer(async body => {sent.push(body); return true;}, key, loading), {});
  render('usr_one:ven_one', true).setDraft('First account');
  const stale = render('usr_two:ven_one', false);
  assert.equal(await stale.submit(), false);
  assert.deepEqual(sent, []);
  for (const run of h.layouts) run();
  assert.equal(render('usr_two:ven_one').draft, '');
  assert.equal(values.get('usr_one:ven_one'), 'First account');
});

test('Stop releases the real composer immediately and late failure cannot restore old text', async () => {
  setup(); const values = new Map();
  window.localStorage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
  const finishes = [];
  const h = harness('components/chat/useChatComposer.ts', {'@/lib/usageControl': {announceUsageControl() {}}});
  const send = () => new Promise(resolve => finishes.push(resolve));
  const render = () => h.render(() => h.exports.useChatComposer(send, 'founder:venture'), {});
  render().setDraft('Old request');
  const old = render().submit();
  assert.equal(render().pending, true);
  render().stop();
  assert.equal(render().pending, false);
  render().setDraft('Corrected request');
  const corrected = render().submit();
  finishes[0](false); await old;
  assert.equal(render().pending, true);
  assert.equal(render().draft, '');
  finishes[1](true); await corrected;
  assert.equal(render().pending, false);
});

test('Stop button stops chat, clears queued messages and leaves typing available', () => {
  setup(); const actions = [];
  const composer = {draft: 'Correction', pending: true, stop() {this.pending = false;}, setDraft() {}, clearError() {}};
  const h = harness('components/chat/ChatDock.tsx', {'./useChatComposer': {useChatComposer: () => composer}});
  const props = {messages: [], onSend() {}, onAction: action => actions.push(action)};
  const render = () => nodes(h.render(h.exports.ChatDock, props));
  const stop = render().find(node => node.props?.['aria-label'] === 'Stop Charlie');
  assert.ok(stop); assert.notEqual(stop.props.disabled, true);
  stop.props.onClick();
  assert.deepEqual(actions, ['chat.stop']);
  assert.equal(composer.pending, false);
  assert.equal(composer.draft, 'Correction');
  assert.equal(render().some(node => node.props?.['aria-label'] === 'Stop Charlie'), false);
  assert.equal(render().find(node => node.type === 'textarea').props.disabled, false);
});

test('composer Stop is hidden while idle, follows reply activity, and confirms only Stopped across domains', async () => {
  for (const [ventureId, idea] of [['ven_inventory', 'Inventory audits for warehouse teams'], ['ven_pets', 'Find local playmates for puppies']]) {
    setup(); const actions = [];
    const composer = {draft: idea, pending: false, stop() {this.pending = false;}, setDraft() {}, clearError() {}};
    const h = harness('components/chat/ChatDock.tsx', {
      './useChatComposer': {useChatComposer: () => composer},
      '@/lib/founderUI': {FounderUIContext: {value: 'home'}},
      '@/lib/client': {verifiedSessionEpoch: () => 1, verifiedSessionSubject: () => 'owned',
        api: async () => ({ok: true, data: {items: [{id: `background-${ventureId}`}]}})},
    });
    const props = {variant: 'panel', ventureId, messages: [{id: 'idea', role: 'user', body_md: idea}], onSend() {}, onAction: (action, payload) => actions.push({action, payload})};
    const render = () => nodes(h.render(h.exports.ChatDock, props));
    assert.equal(render().some(node => node.props?.['aria-label'] === 'Stop Charlie'), false);
    h.effects.find(effect => String(effect).includes('/chat/work'))();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(render().some(node => node.props?.['aria-label'] === 'Stop Charlie'), false);
    for (const phase of ['sending', 'thinking', 'streaming', 'idle']) {
      composer.pending = phase === 'sending';
      props.streaming = ['thinking', 'streaming'].includes(phase) ? {messageId: 'reply', text: phase === 'streaming' ? 'Reading your idea' : ''} : undefined;
      const all = render();
      assert.equal(all.some(node => node.props?.['aria-label'] === 'Stop Charlie'), phase !== 'idle');
      assert.equal(all.filter(node => node.props?.['data-usage-control'] === 'chat_send').length, 1);
      assert.equal(actions.length, 0);
    }
    composer.pending = true;
    render().find(node => node.props?.['aria-label'] === 'Stop Charlie').props.onClick();
    assert.deepEqual(actions, [{action: 'chat.stop', payload: {job_ids: [`background-${ventureId}`]}}]);
    assert.equal(render().some(node => node.props?.['aria-label'] === 'Stop Charlie'), false);
    const chat = checkpointChat('unknown', new Map(), true, ventureId);
    await chat.render().confirmChatAction('chat.stop', {job_ids: [`background-${ventureId}`]});
    assert.equal(chat.render().messages.at(-1).body_md, 'Stopped.');
  }
});
test('dictation fills the draft without fetching or sending; unsupported browsers hide it', async () => {
  setup(); let speech, value = 'Existing idea', calls = 0;
  global.fetch = () => { calls++; throw new Error('must never fetch'); };
  window.webkitSpeechRecognition = class { constructor() { speech = this; } start() {} stop() { this.onend(); } abort() {} };
  const h = harness('components/chat/DictateButton.tsx', { '@/lib/desktop': { isDesktopShell: () => false } });
  const render = () => h.render(h.exports.DictateButton, { value, onChange: next => { value = next; } }); render(); h.effects[0]();
  await nodes(render()).find(n => n.type === 'button').props.onClick();
  speech.onresult({ results: [[{ transcript: 'for teachers' }]] }); assert.equal(value, 'Existing idea for teachers'); render();
  speech.onresult({ results: [[{ transcript: 'for school teachers' }]] }); assert.equal(value, 'Existing idea for school teachers'); assert.equal(calls, 0);
  delete window.webkitSpeechRecognition; const noApi = harness('components/chat/DictateButton.tsx', { '@/lib/desktop': { isDesktopShell: () => false } });
  noApi.render(noApi.exports.DictateButton, { value: '', onChange() {} }); noApi.effects[0](); assert.equal(noApi.render(noApi.exports.DictateButton, { value: '', onChange() {} }), null);
});
test('two choices send nothing until Submit combines both with typed context exactly once', async () => {
  setup(); const sent = [], choices = ['Teachers '.repeat(20), 'School administrators'];
  const composer = { draft: 'In local schools', pending: false, setDraft(next) { this.draft = next; }, clearError() {}, submit: async () => true };
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer } });
  const props = { messages: [{ id: 'q1', role: 'assistant', body_md: 'Who uses it?', actions: choices.map(text => ({ label: text, text, action: 'chat.reply', payload: { question_id: 'question1', text } })) }], onSend() {}, onAction: async (action, payload) => { sent.push({ action, payload }); } };
  const render = () => nodes(h.render(h.exports.ChatDock, props));
  for (const choice of choices) render().find(n => n.props?.['data-usage-control'] === 'chat_followup_action' && label(n) === choice).props.onClick();
  assert.equal(sent.length, 0); const all = render(), submit = all.find(n => n.props?.['data-usage-control'] === 'chat_options_submit');
  await submit.props.onClick(); await submit.props.onClick(); assert.equal(sent.length, 1); assert.equal(sent[0].payload.text, `Assumed: ${choices.join(', ')}\nIn local schools`); assert.equal(sent[0].payload.question_id, 'question1');
  assert.ok(all.some(n => n.type === 'button' && label(n) === 'Your own answer'));
});
test('recommendation is not preselected, unsure explicitly submits it, and long chat text stays full', async () => {
  setup(); const sent = [], long = 'A long explanation. '.repeat(80), composer = { draft: '', pending: false, setDraft() {}, clearError() {} };
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer } });
  const all = nodes(h.render(h.exports.ChatDock, { messages: [{ id: 'q2', role: 'assistant', body_md: long, actions: [{ label: 'Teachers', action: 'chat.reply', payload: { question_id: 'q2', text: 'Teachers', recommended: true, selected: true, recommendation_reason: 'Start with teachers.' } }] }], onSend() {}, onAction: async (_a, p) => sent.push(p) }));
  const option = all.find(n => n.props?.['data-usage-control'] === 'chat_followup_action');
  assert.equal(option.props['aria-pressed'], false);
  assert.match(label(option), /Recommended: Start with teachers\./);
  assert.equal(all.find(n => n.props?.['data-usage-control'] === 'chat_options_submit').props.disabled, true);
  await all.find(n => n.props?.['data-usage-control'] === 'chat_use_recommended').props.onClick(); assert.equal(sent[0].text, 'Assumed: Teachers');
  assert.ok(all.some(n => n.props?.dangerouslySetInnerHTML?.__html.includes(long.trim()))); assert.equal(all.filter(n => label(n) === 'Show more').length, 0);
});
test('the pick preserves typed founder context and displays the option as assumed', async () => {
  setup(); const sent = [], composer = { draft: 'I have not spoken to any yet', pending: false, setDraft() {}, clearError() {} };
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer } });
  const props = { messages: [{ id: 'assumed-question', role: 'assistant', body_md: 'Who is it for?', actions: [{ label: 'Home cooks', action: 'chat.reply', payload: { question_id: 'assumed-question', recommended: true } }] }], onSend() {}, onAction: async (_action, payload) => sent.push(payload) };
  const all = nodes(h.render(h.exports.ChatDock, props));
  await all.find(node => node.props?.['data-usage-control'] === 'chat_use_recommended').props.onClick();
  assert.equal(sent[0].text, 'Assumed: Home cooks\nI have not spoken to any yet');
  props.messages.push({ id: 'assumed-answer', role: 'user', body_md: sent[0].text });
  const saved = nodes(h.render(h.exports.ChatDock, props));
  assert.ok(saved.some(node => node.props?.dangerouslySetInnerHTML?.__html.includes('Your pick: Home cooks')));
  assert.ok(!saved.some(node => node.props?.dangerouslySetInnerHTML?.__html.includes('(assumed)')));
  assert.ok(saved.some(node => node.type === 'small' && node.props.children === 'Suggested option, treated as an assumption until you have evidence'));
});

test('answer prefills exclude audience context but confirm audience answers and relevant urgency details across unrelated domains', async () => {
  for (const [audience, detail] of [['Payroll administrators', 'A missed filing triggered a penalty last week.'],
    ['Urban cyclists', 'A flat tyre left me stranded yesterday.']]) {
    setup(); const sent = [], composer = { draft: '', pending: false, setDraft(next) { this.draft = next; }, clearError() {} };
    const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer },
      '@/lib/founderUI': { FounderUIContext: { value: 'home' } } });
    const question = { id: 'question', role: 'assistant', body_md: 'What makes this problem urgent?', actions: [
      { label: 'An incident made it urgent', action: 'chat.reply', payload: { question_id: 'turn', prefill: audience } }] };
    const props = { variant: 'panel', messages: [question], onSend() {}, onAction: async (_action, payload) => { sent.push(payload.text); return true; } };
    const render = () => nodes(h.render(h.exports.ChatDock, props));
    assert.equal(render().some(node => node.type === 'button' && label(node) === `Use: ${audience}`), false);
    question.body_md = `For ${audience.toLowerCase()}, what makes this problem urgent?`;
    assert.equal(render().some(node => node.type === 'button' && label(node) === `Use: ${audience}`), false);
    assert.ok(render().some(node => node.type === 'button' && label(node) === 'An incident made it urgent'));
    assert.equal(composer.draft, '');
    question.actions[0].payload.prefill = detail;
    question.actions[0].text = detail;
    const urgencyPrefill = render().find(node => node.type === 'button' && label(node) === `Use: ${detail}`);
    assert.ok(urgencyPrefill); urgencyPrefill.props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(composer.draft, ''); assert.deepEqual(sent, [detail]);
    question.id = 'audience-question';
    question.body_md = `Is ${audience} the audience you want to focus on?`;
    question.actions[0] = { label: audience, text: audience, action: 'chat.reply', payload: { question_id: 'turn', prefill: audience } };
    const audiencePrefill = render().find(node => node.type === 'button' && label(node) === `Use: ${audience}`);
    assert.ok(audiencePrefill); audiencePrefill.props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(composer.draft, ''); assert.deepEqual(sent, [detail, audience]);
    question.id = 'explicit-question';
    question.actions[0].label = `Use: ${audience}`;
    const explicitPrefill = render().find(node => node.type === 'button' && label(node) === `Use: ${audience}`);
    assert.ok(explicitPrefill); explicitPrefill.props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(composer.draft, ''); assert.deepEqual(sent, [detail, audience, audience]);
  }
});

test('saved defaults stay visible as Use chips, confirm on tap and never silently prefill chat', async () => {
  setup(); const words = 'The receptionist phones people on a paper waitlist one by one.';
  const sent = [], composer = { draft: '', pending: false, setDraft(value) { composer.draft = value; }, clearError() {} };
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer } });
  const props = { messages: [{ id: 'workaround', role: 'assistant', body_md: 'What do they do today?', actions: [
    { label: 'A named workaround', action: 'chat.reply', payload: { question_id: 'workaround', prefill: words, recommended: true } },
  ] }], onSend() {}, onAction: async (_action, payload) => { sent.push(payload.text); return true; } };
  const all = nodes(h.render(h.exports.ChatDock, props)); h.effects.forEach(effect => effect());
  assert.equal(composer.draft, '');
  const use = all.find(node => node.type === 'button' && label(node) === `Use: ${words}`);
  assert.ok(use); use.props.onClick(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(composer.draft, ''); assert.deepEqual(sent, [words]);
  composer.draft = 'My unsent correction';
  h.render(h.exports.ChatDock, props); h.effects.forEach(effect => effect());
  assert.equal(composer.draft, 'My unsent correction');
  props.messages[0].id = 'default-only'; props.messages[0].actions[0].label = `Use: ${words}`; props.messages[0].actions[0].text = words;
  const defaultOnly = nodes(h.render(h.exports.ChatDock, props)).find(node => node.type === 'button' && node.props['aria-label'] === `Use: ${words}`);
  assert.equal(defaultOnly.props['aria-label'], `Use: ${words}`);
  defaultOnly.props.onClick(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(composer.draft, 'My unsent correction'); assert.deepEqual(sent, [words, words]);
});

test('IdeaCanvas keeps an audience default out of the composer until its Use chip is clicked', () => {
  setup(); window.location.pathname = '/v/gym-default/idea';
  const oldLocal = global.localStorage, oldSession = global.sessionStorage, oldDocument = global.document;
  const storage = () => { const values = new Map(); return { getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }; };
  global.localStorage = storage(); global.sessionStorage = storage();
  global.document = { getElementById: () => null };
  try {
    const words = 'Independent gym owners';
    const h = harness('app/v/[ventureId]/idea/page.tsx', {
      'next/navigation': { useParams: () => ({ ventureId: 'gym-default' }), usePathname: () => window.location.pathname, useSearchParams: () => new URLSearchParams() },
      '@/components/layout/AppShell': { AppShell: 'shell' }, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
      '@/lib/apiCache': { cacheGet: () => null, cachePut() {} }, '@/hooks/useDurableJob': { useDurableJob: () => ({}) },
      '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1 },
    });
    const render = () => nodes(h.render(h.exports.default));
    render(); h.state[7] = { id: 'gym-session', state: 'clarifying', case: 'know', current_revision: { id: 'gym-revision', structured: {} },
      turns: [{ id: 'gym-q3', role: 'agent', kind: 'question', text: 'Who needs this most?', options: { field: 'customer', prefill: words, options: ['Independent gyms', 'Fitness studios'] } }] };
    h.state[21] = true;
    let all = render(); h.effects.slice(1).forEach(effect => effect()); all = render();
    assert.equal(all.find(node => node.props?.id === 'idea-answer').props.value, '');
    all.find(node => node.type === 'button' && label(node) === `Use: ${words}`).props.onClick();
    all = render(); assert.equal(all.find(node => node.props?.id === 'idea-answer').props.value, words);
    all.find(node => node.props?.id === 'idea-answer').props.onChange({ target: { value: 'My own correction' } });
    assert.equal(render().find(node => node.props?.id === 'idea-answer').props.value, 'My own correction');
  } finally { global.localStorage = oldLocal; global.sessionStorage = oldSession; global.document = oldDocument; }
});
test('clarification has one instruction and keeps Send accessible across unrelated audiences', () => {
  for (const audience of ['Procurement teams', 'Dog walkers']) {
  setup();
  const h = harness('components/chat/ChatDock.tsx', { '@/lib/founderUI': { FounderUIContext: { value: 'home' } } });
  const question = id => ({ id, role: 'assistant', body_md: 'Question 1 of 3 · Demand Reality\n\nSelect any that fit, then submit, or type or speak your own answer below.\n\nYou can choose Continue clarifying whenever you\'re ready.', actions: [
    { label: audience, action: 'chat.reply', payload: { question_id: id } },
    { label: 'Retry', action: 'idea.review', payload: { retry: true } },
    { label: 'Continue clarifying', action: 'idea.review' }] });
  const all = nodes(h.render(h.exports.ChatDock, { variant: 'panel', messages: [
    { id: 'legacy', role: 'assistant', body_md: 'Your answer is saved.', actions: [{ label: 'Continue clarifying', action: 'idea.review' }, { label: 'Retry', action: 'idea.review', payload: { retry: true } }] }, question('old'),
    { id: 'reply', role: 'user', body_md: 'Typed answer' }, question('current')], onSend() {}, onAction() {} }));
  const submit = all.find(node => node.props?.['data-usage-control'] === 'chat_options_submit');
  assert.equal(submit.props.children, '↑');
  assert.equal(submit.props['aria-label'], 'Send');
  assert.equal(submit.props.title, 'Send');
  assert.ok(all.some(node => label(node) === 'Select one or more, then press send'));
  assert.ok(!all.some(node => label(node) === 'Pick an answer or type your own.'));
  assert.ok(!all.some(node => node.type === 'button' && label(node) === 'Continue clarifying'));
  assert.ok(all.some(node => node.type === 'button' && label(node) === 'Retry'));
  assert.ok(!all.some(node => node.props?.dangerouslySetInnerHTML?.__html.includes('Select any that fit')));
  assert.ok(!all.some(node => node.props?.dangerouslySetInnerHTML?.__html.includes('Continue clarifying')));
  assert.equal(all.filter(node => node.props?.['data-usage-control'] === 'chat_options_submit').length, 1);
  for (const props of [{hideComposer: true, variant: 'panel'}, {variant: 'dock'}]) {
    const inline = nodes(h.render(h.exports.ChatDock, {...props, messages: [question('current')], onSend() {}}));
    assert.ok(inline.some(node => label(node) === 'Select one or more, then Send'));
    assert.ok(inline.some(node => node.type === 'button' && label(node) === 'Retry'));
    assert.ok(inline.some(node => node.props?.['data-usage-control'] === 'chat_options_submit' && label(node) === 'Send'));
  }
  assert(!all.some(node => node.props?.role === 'status' && /Question [23]/.test([node.props.children].flat().join(''))));
  assert(all.some(node => node.props?.dangerouslySetInnerHTML?.__html.includes('Question 1 of 3')));
  const css = readFileSync(path.join(__dirname, 'ChatDock.module.css'), 'utf8');
  assert.match(css, /button\[data-usage-control="chat_options_submit"\] \{[^}]*width: var\(--im-inline, 28px\);[^}]*border-radius: 50%/);
  }
});

for (const hideComposer of [false, true]) {
  test(`only a user-picked choice is selected and empty deselection has recovery guidance (inline=${hideComposer})`, async () => {
    setup(); const sent = [], composer = {draft: '', pending: false, setDraft(next) {this.draft = next;}, clearError() {}};
    const h = harness('components/chat/ChatDock.tsx', {'./useChatComposer': {useChatComposer: () => composer}});
    const question = id => ({id, role: 'assistant', body_md: 'Who uses it?', actions: [
      {label: 'Tutors', action: 'chat.reply', payload: {question_id: id, text: 'Tutors', recommended: true}},
      {label: 'Schools', action: 'chat.reply', payload: {question_id: id, text: 'Schools'}}]});
    const props = {hideComposer, messages: [question('question1')], onSend() {}, onAction: async (_action, payload) => {sent.push(payload);}};
    const render = () => nodes(h.render(h.exports.ChatDock, props));
    let all = render(), pick = all.find(node => node.props?.['data-usage-control'] === 'chat_followup_action' && node.props['aria-label'] === 'Tutors');
    assert.equal(pick.props['aria-pressed'], false);
    assert.doesNotMatch(pick.props.className, /optionPicked/);
    assert.doesNotMatch(label(pick), /✓ Selected/);
    assert.equal(all.find(node => node.props?.['data-usage-control'] === 'chat_options_submit').props.disabled, true);
    pick.props.onClick(); all = render();
    pick = all.find(node => node.props?.['data-usage-control'] === 'chat_followup_action' && node.props['aria-label'] === 'Tutors');
    assert.equal(pick.props['aria-pressed'], true);
    assert.match(pick.props.className, /optionPicked/);
    assert.match(label(pick), /✓ Selected/);
    assert.ok(!all.some(node => node.props?.role === 'status' && label(node) === 'Question 1'));
    pick.props.onClick(); all = render();
    const submit = all.find(node => node.props?.['data-usage-control'] === 'chat_options_submit');
    assert.equal(submit.props.disabled, true);
    assert.equal(submit.props['aria-describedby'], 'answer-hint-question1');
    assert.ok(all.some(node => node.props?.id === 'answer-hint-question1' && /Select one or more/.test(label(node))));
    const text = all.find(node => node.type === 'textarea');
    text.props.onChange(hideComposer ? {target: {value: 'Architects'}} : 'Architects');
    all = render();
    const ready = all.find(node => node.props?.['data-usage-control'] === 'chat_options_submit');
    assert.equal(ready.props.disabled, false); await ready.props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(sent[0].text, 'Architects');
    props.messages = [question('question1'), {id: 'answer1', role: 'user', body_md: 'Architects'}, question('question2')];
    all = render(); assert.ok(!all.some(node => node.props?.role === 'status' && label(node) === 'Question 2'));
  });
}

test('IdeaCanvas Submit saves a suggested pick and typed conversations on separate lines', async () => {
  setup(); window.location.pathname = '/v/idea-mixed/idea'; const posts = [];
  const oldLocal = global.localStorage, oldSession = global.sessionStorage;
  const storage = () => { const values = new Map(); return { getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }; };
  global.localStorage = storage(); global.sessionStorage = storage();
  try {
    const words = 'Two clinic owners I spoke to said cancelled slots stay empty.';
    const saved = { id: 'mixed-session', state: 'clarifying', case: 'know', current_revision: { id: 'mixed-revision', structured: {} },
      turns: [{ id: 'mixed-question', role: 'agent', kind: 'question', text: 'Who have you talked to?', options: { options: ['A few people'] } }] };
    const h = harness('app/v/[ventureId]/idea/page.tsx', {
      'next/navigation': { useParams: () => ({ ventureId: 'idea-mixed' }), usePathname: () => window.location.pathname, useSearchParams: () => new URLSearchParams() },
      '@/components/layout/AppShell': { AppShell: 'shell' }, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
      '@/lib/apiCache': { cacheGet: () => null, cachePut() {} }, '@/hooks/useDurableJob': { useDurableJob: () => ({ job: null }) },
      '@/lib/durableJobs': { restoreIdeaSessionJob: async () => ({ id: null, error: null }), reportJobFailure() {}, jobFailure: () => '' },
      '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
        apiSwr: async () => ({ ok: true, data: { name: 'Clinic waitlist', workspace_id: 'mixed-workspace' } }),
        api: async (url, init) => { if (url.endsWith('/messages')) posts.push({ url, body: JSON.parse(init.body) });
          return { ok: true, data: url.endsWith('/messages') ? { session: saved } : url.endsWith('/plans') ? [] : saved }; } },
    });
    const render = () => nodes(h.render(h.exports.default));
    render(); h.effects[0](); await new Promise(resolve => setImmediate(resolve));
    render(); h.effects[2](); render().find(node => node.type === 'input' && node.props.name === 'idea-choice').props.onChange();
    render().find(node => node.props?.id === 'idea-answer').props.onChange({ target: { value: words } });
    const submit = render().find(node => node.type === 'form' && label(node).includes('Submit'));
    submit.props.onSubmit({ preventDefault() {} }); await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(posts, [{ url: '/api/ventures/idea-mixed/idea/sessions/mixed-session/messages', body: { text: `Assumed: A few people\n${words}` } }]);
  } finally { global.localStorage = oldLocal; global.sessionStorage = oldSession; }
});

test('IdeaCanvas recommendation rejects combined overflow, preserves the draft, and sends the full boundary once', async () => {
  setup(); window.location.pathname = '/v/idea-limit/idea';
  const oldLocal = global.localStorage, oldSession = global.sessionStorage;
  const storage = () => { const values = new Map(); return { getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }; };
  global.localStorage = storage(); global.sessionStorage = storage();
  try {
    const recommended = 'Independent teachers', posts = [];
    const session = { id: 'idea-session', state: 'clarifying', case: 'know',
      current_revision: { id: 'idea-revision', structured: {} },
      turns: [{ id: 'idea-question', role: 'agent', kind: 'question', text: 'Who uses it?',
        options: { options: [recommended], recommended_option: recommended, selected: true, recommendation_reason: 'Teach locally.' } }] };
    const h = harness('app/v/[ventureId]/idea/page.tsx', {
      'next/navigation': { useParams: () => ({ ventureId: 'idea-limit' }), usePathname: () => window.location.pathname,
        useSearchParams: () => new URLSearchParams() },
      '@/components/layout/AppShell': { AppShell: 'shell' }, '@/components/layout/NextStepStrip': { NextStepStrip: 'steps' },
      '@/components/chat/DictateButton': { DictateButton: 'dictate' }, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
      '@/lib/apiCache': { cacheGet: () => null, cachePut() {} },
      '@/hooks/useDurableJob': { useDurableJob: () => ({ job: null }) },
      '@/lib/durableJobs': { restoreIdeaSessionJob: async () => ({ id: null, error: null }), reportJobFailure() {}, jobFailure: () => '' },
      '@/lib/client': { verifiedSessionSubject: () => 'idea-owner', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
        apiSwr: async () => ({ ok: true, data: { name: 'Answer limit', workspace_id: 'idea-workspace' } }),
        api: async (url, init) => {
          if (url.endsWith('/messages')) { posts.push(JSON.parse(init.body)); return { ok: true, data: { session, job_id: 'answer-job' } }; }
          return { ok: true, data: url.endsWith('/plans') ? { items: [] } : session };
        } },
    });
    const render = () => nodes(h.render(h.exports.default, {}));
    render(); h.effects[0](); await new Promise(resolve => setImmediate(resolve));
    render(); h.effects[2]();
    const initial = render();
    assert.equal(initial.find(node => node.type === 'input' && node.props.name === 'idea-choice').props.checked, false);
    assert.equal(initial.find(node => node.props?.['data-usage-control'] === 'idea_options_submit').props.disabled, true);
    assert.ok(initial.some(node => label(node) === 'Recommended'));
    assert.ok(initial.some(node => label(node) === 'Recommended: Teach locally.'));
    initial.find(node => node.type === 'input' && node.props.name === 'idea-choice').props.onChange();
    assert.ok(render().some(node => label(node) === '✓ Selected'));
    render().find(node => node.type === 'input' && node.props.name === 'idea-choice').props.onChange();
    const typed = 'x'.repeat(4000);
    render().find(node => node.props?.id === 'idea-answer').props.onChange({ target: { value: typed } });
    let all = render(), pick = all.find(node => node.props?.['data-usage-control'] === 'idea_use_recommended');
    pick.props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(posts.length, 0);
    assert.equal(pick.props.disabled, true);
    all = render();
    assert.equal(all.find(node => node.props?.id === 'idea-answer').props.value, typed);
    assert.equal(all.find(node => node.props?.['data-usage-control'] === 'idea_options_submit').props.disabled, false);
    assert.ok(all.some(node => node.props?.role === 'status' && /recommended.*shorten/i.test(label(node))));
    const context = 'x'.repeat(4000 - `Assumed: ${recommended}\n`.length);
    all.find(node => node.props?.id === 'idea-answer').props.onChange({ target: { value: context } });
    pick = render().find(node => node.props?.['data-usage-control'] === 'idea_use_recommended');
    assert.equal(pick.props.disabled, false);
    pick.props.onClick(); pick.props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(posts, [{ text: `Assumed: ${recommended}\n${context}` }]);
    assert.equal(posts[0].text.length, 4000);
  } finally { global.localStorage = oldLocal; global.sessionStorage = oldSession; }
});

test('IdeaCanvas offers a secondary free confirmation beside research and opens Website', async () => {
  setup(); window.location.pathname = '/v/idea-visible/idea';
  const posts = [], navigations = [];
  window.location.assign = path => navigations.push(path);
  const saved = { id: 'session-visible', state: 'validating', case: 'explore', turns: [],
    current_revision: { id: 'revision-visible', structured: {} },
    validation_runs: ['gstack_office_hours', 'gstack_ceo_review', 'gbrain_think'].map(method => ({
      id: method, method, revision_id: 'revision-visible', state: 'complete', report: {} })) };
  const h = harness('app/v/[ventureId]/idea/page.tsx', {
    'next/navigation': { useParams: () => ({ ventureId: 'idea-visible' }), usePathname: () => window.location.pathname,
      useSearchParams: () => new URLSearchParams() },
    '@/components/layout/AppShell': { AppShell: 'shell' }, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
    '@/lib/apiCache': { cacheGet: () => null, cachePut() {} },
    '@/hooks/useDurableJob': { useDurableJob: () => ({ job: null }) },
    '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async (url, init) => { posts.push({ url, body: JSON.parse(init.body) }); return { ok: true,
        data: { dossier_job_id: null, session: { ...saved, state: 'confirmed' } } }; } },
  });
  h.render(h.exports.default); h.state[7] = saved; h.state[8] = { name: 'Teachers' }; h.state[21] = true;
  const render = () => nodes(h.render(h.exports.default));
  const button = render().find(node => node.type === 'button' && label(node) === 'Build my landing page now');
  assert.ok(button); assert.equal(button.props.className, 'secondary');
  assert.ok(render().some(node => node.props?.['data-usage-control'] === 'idea_continue_research'));
  button.props.onClick(); button.props.onClick(); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(posts, [{ url: '/api/ventures/idea-visible/idea/sessions/session-visible/confirm',
    body: { revision_id: 'revision-visible', start_research: false, accept_validation: true } }]);
  assert.deepEqual(navigations, ['/v/idea-visible/site']);
});

test('IdeaCanvas shares its visible question or review with the banner and formats saved idea fields', () => {
  setup(); window.location.pathname = '/v/idea-visible/idea';
  for (const state of ['clarifying', 'reviewing', 'validating']) {
    const saved = { id: 'session-visible', state, case: 'know', idea_summary: 'Customer: Teachers\nOffer: Lesson plans\nAn unlabelled note',
      current_revision: { id: 'revision-visible', structured: {} },
      turns: state === 'clarifying' ? [{ id: 'question-visible', role: 'agent', kind: 'question', text: 'Who uses it?' }] : state === 'validating'
        ? [{ id: 'old-question', role: 'agent', kind: 'question', text: 'Who uses it?', options: { phase: 'gstack' } }] : [],
      validation_runs: state === 'validating' ? ['gstack_office_hours', 'gstack_ceo_review', 'gbrain_think'].map(method => ({
        id: method, revision_id: 'revision-visible', method, state: 'complete', report: {} })) : [] };
    const h = harness('app/v/[ventureId]/idea/page.tsx', {
      'next/navigation': { useParams: () => ({ ventureId: 'idea-visible' }), usePathname: () => window.location.pathname,
        useSearchParams: () => new URLSearchParams() },
      '@/components/layout/AppShell': { AppShell: 'shell' }, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
      '@/lib/apiCache': { cacheGet: () => null, cachePut() {} },
      '@/hooks/useDurableJob': { useDurableJob: () => ({ job: { kind: 'idea.clarify_turn' }, needsRecovery: true, delayed: true }) },
      '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1 },
    });
    h.render(h.exports.default);
    h.state[7] = saved; h.state[8] = { name: 'Teachers' }; h.state[21] = true; h.state[27] = 'old-job';
    const all = nodes(h.render(h.exports.default));
    assert.equal(all[0].props.ideaStatus.state, 'needs_you');
    assert.doesNotMatch(label(all[0]), /Preparing|taking longer/);
    assert.ok(all.some(node => node.type === 'strong' && label(node) === 'Customer:'));
    assert.ok(all.some(node => node.type === 'strong' && label(node) === 'Offer:'));
    assert.ok(all.some(node => node.type === 'p' && label(node) === 'An unlabelled note'));
    assert.equal(all[0].props.ideaStatus.activity, state === 'clarifying' ? 'Answer the next question about your idea' : 'Review your saved idea');
    assert.equal(all.some(node => node.type === 'p' && label(node) === 'Answer the guided question below to define this idea.'), state === 'clarifying');
    if (state === 'clarifying') {
      const oldDocument = global.document, scrolled = [];
      global.document = { getElementById: id => { assert.equal(id, 'idea-question'); assert.ok(all.some(node => node.props?.id === id)); return { scrollIntoView: options => scrolled.push(options) }; } };
      try { h.effects.at(-1)(); assert.deepEqual(scrolled, [{ block: 'start' }]); }
      finally { global.document = oldDocument; }
    }
    if (state === 'reviewing') assert.ok(all.some(node => node.type === 'button' && label(node) === 'Validate this direction'));
  }
});
test('a fresh draft stays resumable when an older send is acknowledged', async () => {
  const {unsentEntryDraftHref} = await import('../../lib/chatEntry.ts');
  for (const [domain, first, next] of [
    ['packing', 'Scheduling for warehouse teams', 'Include loading dock availability'],
    ['cycling', 'Weekend routes for families', 'Include paths away from traffic'],
  ]) {
    setup();
    window.location.search = `?fresh=${domain}`;
    const values = new Map();
    const storage = window.localStorage = {getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
    let acknowledge;
    const h = harness('app/new/page.tsx', {
      '@/components/layout/AppShell': {AppShell: 'shell'}, './ConfirmedConversation': {},
      '@/lib/client': {verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ok: true}),
        api: url => url.endsWith('/history') ? Promise.resolve({ok: true, data: {items: []}}) : new Promise(resolve => {acknowledge = resolve;})},
    }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
    const render = () => nodes(h.render(h.exports.Conversation, {freshStart: true}));
    const setDraft = value => render().find(node => node.props?.id === 'entry-message').props.onChange({target: {value}});
    const send = async () => {
      render().find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
      await new Promise(resolve => setImmediate(resolve));
    };
    const reply = async () => {
      acknowledge({ok: true, data: {status: 'reply', body_md: 'Tell me more.', actions: []}});
      await new Promise(resolve => setImmediate(resolve));
    };
    render(); await h.effects[2](); await new Promise(resolve => setImmediate(resolve));
    setDraft(first); await send(); setDraft(next);
    await reply();
    assert.equal(render().find(node => node.props?.id === 'entry-message').props.value, next);
    assert.equal(unsentEntryDraftHref(storage, 'founder'), `/new?fresh=${domain}`);
    await send(); await reply();
    assert.equal(unsentEntryDraftHref(storage, 'founder'), null);
  }
});

test('Back cancels each uncommitted step and restores the previous question and draft', async () => {
  setup(); const cancelled = []; let count = 0;
  const h = harness('app/new/page.tsx', {
    'next/navigation': { useRouter: () => ({ replace() {} }) },
    '@/components/layout/AppShell': { AppShell: 'shell' }, './ConfirmedConversation': {},
    '@/components/ventures/BetaPathStrip': {}, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
    '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }), api: async (url) => {
      if (url.endsWith('/history')) return { ok: true, data: { items: [] } };
      if (url.endsWith('/cancel')) { cancelled.push(url); return { ok: true, data: { status: 'cancelled' } }; }
      return { ok: true, data: { status: 'clarify', id: `q${++count}`, question: `Idea question ${count}`, choices: ['An app for dog walkers'] } };
    } },
    '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': { DictateButton: 'dictate' },
  }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
  const render = () => nodes(h.render(h.exports.Conversation, {}));
  render(); h.effects[2](); await new Promise(resolve => setImmediate(resolve));
  for (const text of ['An app for dog walkers', 'hello', 'how are you']) {
    let all = render(); all.find(n => n.type === 'textarea').props.onChange({ target: { value: text } }); all = render();
    all.find(n => n.type === 'form').props.onSubmit({ preventDefault() {} }); await new Promise(resolve => setImmediate(resolve));
  }
  let all = render(); assert.ok(all.some(n => label(n) === 'Idea question 3'));
  all.find(n => n.props?.['data-usage-control'] === 'onboarding_back').props.onClick(); await new Promise(resolve => setImmediate(resolve));
  all = render(); assert.ok(all.some(n => label(n) === 'Idea question 2')); assert.equal(all.find(n => n.type === 'textarea').props.value, 'how are you');
  all.find(n => n.props?.['data-usage-control'] === 'onboarding_back').props.onClick(); await new Promise(resolve => setImmediate(resolve));
  all = render(); assert.equal(all.find(n => n.type === 'textarea').props.value, 'hello'); assert.equal(cancelled.length, 2); assert.ok(cancelled[0].includes('q3')); assert.ok(cancelled[1].includes('q2'));
});
test('attachment-free Continue waits for session and history, then sends one preview', async () => {
  setup(); const requests = []; let subject = null, finishHistory;
  const history = new Promise(resolve => { finishHistory = resolve; });
  const h = harness('app/new/page.tsx', {
    'next/navigation': { useRouter: () => ({ replace() {} }) },
    '@/components/layout/AppShell': { AppShell: 'shell' }, './ConfirmedConversation': {},
    '@/components/ventures/BetaPathStrip': {}, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
    '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': { DictateButton: 'dictate' },
    '@/lib/client': { verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => subject ? 1 : 0,
      ensureSession: async () => ({ ok: true }), api: async (url, options) => {
        if (url.endsWith('/history')) return history;
        requests.push({ url, body: JSON.parse(options.body) });
        return { ok: true, data: { status: 'clarify', id: 'hello', question: 'What does it do?', choices: [] } };
      } },
  }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
  const render = () => nodes(h.render(h.exports.Conversation, {}));
  let all = render();
  assert.equal(all.find(node => node.type === 'textarea').props.disabled, true);
  assert.equal(all.find(node => node.props?.['data-usage-control'] === 'onboarding_continue').props.disabled, true);
  subject = 'founder'; render(); h.effects[2](); await new Promise(resolve => setImmediate(resolve));
  all = render();
  assert.equal(all.find(node => node.type === 'textarea').props.disabled, true);
  assert.equal(all.find(node => node.type === 'voice').props.disabled, true);
  assert.equal(all.find(node => node.type === 'attachments').props.disabled, true);
  all.find(node => node.type === 'textarea').props.onChange({ target: { value: 'hello' } });
  all = render();
  assert.equal(all.find(node => node.props?.['data-usage-control'] === 'onboarding_continue').props.disabled, true);
  all.find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve)); assert.equal(requests.length, 0);
  finishHistory({ ok: true, data: { items: [] } }); await new Promise(resolve => setImmediate(resolve));
  all = render();
  assert.equal(all.find(node => node.type === 'textarea').props.disabled, false);
  assert.equal(all.find(node => node.props?.['data-usage-control'] === 'onboarding_continue').props.disabled, false);
  const form = all.find(node => node.type === 'form');
  form.props.onSubmit({ preventDefault() {} }); form.props.onSubmit({ preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/entry/chat/preview');
  assert.equal(requests[0].body.text, 'hello');
  assert.deepEqual(requests[0].body.evidence_ids, []);
  assert.ok(requests[0].body.request_id);
  assert.ok(render().some(node => node.type === 'article' && label(node) === 'Youhello'));
});
for (const skip of [false, true]) {
  test(`smalltalk keeps an editable composer and reaches naming via ${skip ? 'skip' : 'answers'}`, async () => {
    setup(); const requests = [], paths = [];
    const idea = 'A tool that helps bakeries track flour inventory', audience = 'Neighbourhood bakery owners';
    const offer = 'What would it actually do? Describe one task it would help with.';
    const response = field => ({ id: field, route: 'startup', state: 'pending', message: 'Saved',
      next_step: 'idea', skipped_steps: [], expires_at: '',
      ...(field === 'customer' ? { onboarding_question: 'Who is it for?', onboarding_field: field } : {}) });
    const h = harness('app/new/page.tsx', {
      'next/navigation': { useRouter: () => ({ replace: href => paths.push(href) }) },
      '@/components/layout/AppShell': { AppShell: 'shell' }, './ConfirmedConversation': {},
      '@/components/ventures/BetaPathStrip': {}, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
      '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': { DictateButton: 'dictate' },
      '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
        api: async (url, options) => {
          if (url.endsWith('/history')) return { ok: true, data: { items: [] } };
          requests.push({ url, body: JSON.parse(options.body) });
          return { ok: true, data: requests.length === 1
            ? { status: 'clarify', id: 'hello', question: offer, choices: [], onboarding_question: offer, onboarding_field: 'offer' }
            : { status: 'proposal', proposal: response(requests.length === 2 ? 'customer' : 'ready') } };
        } },
    }, [], '\nmodule.exports.Conversation = NewVentureConversation;');
    const render = () => nodes(h.render(h.exports.Conversation, {}));
    render(); h.effects[2](); await new Promise(resolve => setImmediate(resolve));
    const send = async text => {
      assert.equal(render().filter(node => node.type === 'textarea').length, 1);
      assert.equal(render().find(node => node.type === 'textarea').props.disabled, false);
      render().find(node => node.type === 'textarea').props.onChange({ target: { value: text } });
      assert.equal(render().find(node => node.props?.['data-usage-control'] === 'onboarding_continue').props.disabled, false);
      render().find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
      await new Promise(resolve => setImmediate(resolve));
    };
    const skipAnswer = async () => {
      assert.equal(render().find(node => node.props?.['data-usage-control'] === 'onboarding_continue').props.disabled, true);
      const button = render().find(node => node.props?.['data-usage-control'] === 'onboarding_skip');
      assert.equal(button.props.disabled, false); button.props.onClick();
      await new Promise(resolve => setImmediate(resolve));
    };
    await send('hello');
    assert.ok(render().some(node => node.type === 'label' && label(node) === offer));
    assert.equal(render().filter(node => node.props?.['data-usage-control'] === 'onboarding_name').length, 0);
    if (skip) await skipAnswer(); else await send(idea);
    assert.ok(render().some(node => node.type === 'label' && label(node) === 'Who is it for?'));
    if (skip) await skipAnswer(); else await send(audience);
    assert.equal(render().filter(node => node.type === 'textarea').length, 1);
    assert.equal(render().find(node => node.props?.['data-usage-control'] === 'onboarding_name').props.disabled, false);
    assert.deepEqual(requests.map(request => request.body.text), ['hello',
      `hello\nClarification: ${skip ? 'Skip this question' : idea}`,
      `hello\nClarification: ${skip ? 'Skip this question' : idea}\nAudience: ${skip ? 'Skip this question' : audience}`]);
    assert.deepEqual(paths, [], 'core answers stay on the conversational page');
  });
}
test('core answers stay visible when the next question is appended and after reload', async () => {
  setup(); let count = 0, finish; const paths = [];
  const idea = 'A tool to organize meal plans', audience = 'Independent tutors';
  const responses = [
    { id: 'idea', route: 'startup', state: 'pending', onboarding_question: 'Who is it for?', onboarding_field: 'customer', message: 'Saved', next_step: 'idea', skipped_steps: [], expires_at: '' },
    { id: 'audience', route: 'startup', state: 'pending', message: 'Your direction is saved', next_step: 'idea', skipped_steps: [], expires_at: '' },
  ];
  const extra = {
    'next/navigation': { useRouter: () => ({ replace: path => paths.push(path) }) },
    '@/components/layout/AppShell': { AppShell: 'shell' }, './ConfirmedConversation': {},
    '@/components/ventures/BetaPathStrip': {}, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
    '@/components/chat/VoiceMic': { VoiceMic: 'voice' }, '@/components/chat/DictateButton': { DictateButton: 'dictate' },
    '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
      api: async (url, options) => {
        if (url.includes('/history')) return { ok: true, data: { items: count === 0 ? [] : [
          { text: idea, proposal: responses[0] }, { text: `${idea}\nAudience: ${audience}`, proposal: responses[1] }] } };
        paths.push(JSON.parse(options.body).text); count++;
        if (count === 2) await new Promise(resolve => { finish = resolve; });
        return { ok: true, data: { status: 'proposal', proposal: responses[count - 1] } };
      } },
  };
  const h = harness('app/new/page.tsx', extra, [], '\nmodule.exports.Conversation = NewVentureConversation;');
  const render = () => nodes(h.render(h.exports.Conversation, {}));
  render(); h.effects[2](); await new Promise(resolve => setImmediate(resolve));
  render().find(node => node.type === 'textarea').props.onChange({ target: { value: idea } });
  render().find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(render().filter(node => node.type === 'article').map(label), ['You' + idea, 'CharlieWho is it for?']);
  render().find(node => node.type === 'textarea').props.onChange({ target: { value: audience } });
  render().find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(render().some(node => node.type === 'article' && label(node) === 'You' + idea));
  assert.ok(render().some(node => node.type === 'article' && label(node) === 'You' + audience), 'submitted answer is visible while waiting');
  assert.equal(render().find(node => node.props?.['data-usage-control'] === 'onboarding_skip')?.props.disabled, true);
  finish(); await new Promise(resolve => setImmediate(resolve));
  let bubbles = render().filter(node => node.type === 'article').map(label);
  assert.ok(bubbles.includes('You' + idea)); assert.ok(bubbles.includes('You' + audience));
  assert.equal(bubbles.filter(text => text === 'You' + idea).length, 1);
  assert.deepEqual(paths, [idea, `${idea}\nAudience: ${audience}`], 'advancing never navigates');
  const reloaded = harness('app/new/page.tsx', extra, [], '\nmodule.exports.Conversation = NewVentureConversation;');
  reloaded.render(reloaded.exports.Conversation, {}); await reloaded.effects[2](); await new Promise(resolve => setImmediate(resolve));
  bubbles = nodes(reloaded.render(reloaded.exports.Conversation, {})).filter(node => node.type === 'article').map(label);
  assert.ok(bubbles.includes('You' + idea)); assert.ok(bubbles.includes('You' + audience));
});
test('an answer request cannot erase a newer draft typed while it is pending', async () => {
  setup(); let finish;
  const composer = { draft: 'First draft', pending: false, setDraft(value) { this.draft = value; }, clearError() {} };
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer } });
  const props = { messages: [{ id: 'q3', role: 'assistant', body_md: 'Who uses it?', actions: [{ label: 'Teachers', action: 'chat.reply', payload: { question_id: 'q3', recommended: true } }] }], onSend() {}, onAction: () => new Promise(resolve => { finish = resolve; }) };
  const render = () => nodes(h.render(h.exports.ChatDock, props));
  render().find(n => n.props?.['data-usage-control'] === 'chat_options_submit').props.onClick();
  composer.draft = 'A newer correction'; render(); finish(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(composer.draft, 'A newer correction');
});
test('Stop during native listener preparation cannot start a microphone later', async () => {
  setup(); let resolveListener, detached = false; const commands = [];
  const h = harness('components/chat/DictateButton.tsx', { '@/lib/desktop': {
    isDesktopShell: () => true, desktopInvokeQuiet: async () => true,
    desktopListenOrThrow: () => new Promise(resolve => { resolveListener = resolve; }),
    desktopInvokeOrThrow: async command => commands.push(command),
  } });
  const render = () => h.render(h.exports.DictateButton, { value: '', onChange() {} });
  render(); h.effects[0](); await new Promise(resolve => setImmediate(resolve));
  nodes(render()).find(n => n.type === 'button').props.onClick();
  nodes(render()).find(n => n.type === 'button').props.onClick();
  resolveListener(() => { detached = true; }); await new Promise(resolve => setImmediate(resolve));
  assert.equal(commands.includes('dictation_start'), false); assert.equal(detached, true);
});
test('a delayed end event from an old browser recognizer cannot stop a new dictation', async () => {
  setup(); const recognizers = []; let value = '';
  window.webkitSpeechRecognition = class { constructor() { recognizers.push(this); } start() {} stop() {} abort() {} };
  const h = harness('components/chat/DictateButton.tsx', { '@/lib/desktop': { isDesktopShell: () => false } });
  const render = () => h.render(h.exports.DictateButton, { value, onChange: next => { value = next; } });
  render(); h.effects[0](); nodes(render()).find(n => n.type === 'button').props.onClick();
  recognizers[0].onerror(); nodes(render()).find(n => n.type === 'button').props.onClick();
  recognizers[0].onend(); recognizers[1].onresult({ results: [[{ transcript: 'A tool for teachers' }]] });
  assert.equal(value, 'A tool for teachers'); delete window.webkitSpeechRecognition;
});
test('failed native Stop keeps the active listener and offers the same Stop action again', async () => {
  setup(); let handler, sessionId, failStop = true, detached = false, value = '';
  const h = harness('components/chat/DictateButton.tsx', { '@/lib/desktop': {
    isDesktopShell: () => true, desktopInvokeQuiet: async () => true,
    desktopListenOrThrow: async (_event, callback) => { handler = callback; return () => { detached = true; }; },
    desktopInvokeOrThrow: async (command, args) => {
      if (command === 'dictation_start') sessionId = args.sessionId;
      if (command === 'dictation_stop') {
        if (failStop) throw new Error('unconfirmed stop');
        handler({ sessionId, text: 'A tool for teachers', isFinal: true });
      }
    },
  } });
  const render = () => nodes(h.render(h.exports.DictateButton, { value, onChange: next => { value = next; } }));
  render(); h.effects[0](); await new Promise(resolve => setImmediate(resolve));
  render().find(n => n.type === 'button').props.onClick(); await new Promise(resolve => setImmediate(resolve));
  render().find(n => n.type === 'button').props.onClick(); await new Promise(resolve => setImmediate(resolve));
  let all = render(); assert.equal(detached, false); assert.equal(all.find(n => n.type === 'button').props['aria-label'], 'Stop dictation');
  assert.ok(all.some(n => n.props?.error === 'Could not stop dictation. Try Stop again.'));
  failStop = false; all.find(n => n.type === 'button').props.onClick(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(detached, true); assert.equal(value, 'A tool for teachers'); assert.equal(render().find(n => n.type === 'button').props['aria-label'], 'Dictate');
});
test('native startup rejection preserves permission guidance even before its terminal event', async () => {
  setup(); let detached = false;
  const guidance = 'Allow Speech Recognition in System Settings, or type your answer.';
  const h = harness('components/chat/DictateButton.tsx', { '@/lib/desktop': {
    isDesktopShell: () => true, desktopInvokeQuiet: async () => true,
    desktopListenOrThrow: async () => () => { detached = true; },
    desktopInvokeOrThrow: async () => { throw new Error(guidance); },
  } });
  const render = () => nodes(h.render(h.exports.DictateButton, { value: '', onChange() {} }));
  render(); h.effects[0](); await new Promise(resolve => setImmediate(resolve));
  render().find(n => n.type === 'button').props.onClick(); await new Promise(resolve => setImmediate(resolve));
  assert.ok(render().some(n => n.props?.error === guidance)); assert.equal(detached, true);
});
test('late dictation cannot fill a different question or account draft', () => {
  setup(); let speech, value = '';
  window.webkitSpeechRecognition = class { constructor() { speech = this; } start() {} stop() {} abort() {} };
  const h = harness('components/chat/DictateButton.tsx', { '@/lib/desktop': { isDesktopShell: () => false } });
  const render = scope => h.render(h.exports.DictateButton, { value, scope, onChange: next => { value = next; } });
  render('old-question'); h.effects[0](); nodes(render('old-question')).find(n => n.type === 'button').props.onClick();
  render('new-question'); speech.onresult({ results: [[{ transcript: 'Words from the earlier question' }]] });
  assert.equal(value, ''); delete window.webkitSpeechRecognition;
});
test('voice screen context names the visible prepared action for the current venture', () => {
  setup(); const composer = { draft: '', pending: false, setDraft() {}, clearError() {} };
  const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer } });
  const all = nodes(h.render(h.exports.ChatDock, { ventureId: 'test-venture', onVoiceSend: async () => null, onSend() {},
    messages: [{ id: 'coding-plan', role: 'assistant', body_md: 'Review your saved plan.', actions: [{ label: 'Review coding plan', action: 'plan.review', style: 'primary' }] }] }));
  const screen = all.find(n => n.type === 'voice').props.screen;
  assert.deepEqual(screen.visible_actions, ['Review coding plan', 'Send']);
  assert.equal(screen.page, '/new'); assert.ok(screen.expected_next.includes('Review coding plan'));
});


test('confirmed direction Back cancels its own clarification and restores the previous question and draft', async () => {
  setup();const cancelled=[];let count=0;
  const chat={messages:[],conversationKey:'chat-one',conversationId:'cnv-one',contextCurrent:()=>true,reloadHistory:async()=>{},sendChat:async()=>true};
  const h=harness('app/new/ConfirmedConversation.tsx',{
    '@/components/chat/ChatDock':{ChatDock:'chat'},'@/components/chat/useVentureChat':{useVentureChat:()=>chat},
    '@/lib/client':{api:async(url,init)=>{
      if(url.endsWith('/cancel')){cancelled.push(url);return {ok:true,data:{status:'cancelled'}};}
      if(url.endsWith('/preview'))return {ok:true,data:{status:'clarify',id:`direction-q${++count}`,question:`Direction question ${count}`,choices:['Just me','Customers']}};
      return {ok:true,data:{}};
    }},
  });
  const props={receipt:{status:'committed',route:'startup',venture_id:'venture-one',workspace_id:'workspace-one',next_step:'idea',action:{kind:'idea',venture_id:'venture-one',href:'/v/venture-one/idea'},message:'Saved'}};
  const render=()=>nodes(h.render(h.exports.ConfirmedConversation,props)).find(node=>node.type==='chat');
  await render().props.onSend('Change direction');
  assert.ok(nodes(h.render(h.exports.ConfirmedConversation,props)).some(node=>node.props?.role==='status' && label(node)==='Question 1'));
  let dock=render();const first=dock.props.messages.at(-1);
  await dock.props.onAction('chat.reply',{question_id:first.id,text:'Just me. A personal planner'});
  dock=render();assert.equal(dock.props.messages.at(-1).body_md,'Direction question 2');
  assert.ok(nodes(h.render(h.exports.ConfirmedConversation,props)).some(node=>node.props?.role==='status' && label(node)==='Question 2'));
  assert.equal(typeof dock.props.onQuestionBack,'function');
  await dock.props.onQuestionBack();dock=render();
  assert.equal(dock.props.messages.at(-1).body_md,'Direction question 1');
  assert.ok(nodes(h.render(h.exports.ConfirmedConversation,props)).some(node=>node.props?.role==='status' && label(node)==='Question 1'));
  assert.equal(dock.props.draftPrefill.text,'Just me. A personal planner');
  await dock.props.onQuestionBack();dock=render();
  assert.equal(dock.props.messages.length,0);assert.equal(dock.props.draftPrefill.text,'Change direction');
  assert.deepEqual(cancelled,['/api/entry/chat/proposals/direction-q2/cancel','/api/entry/chat/proposals/direction-q1/cancel']);
});

test('owned direction Back replaces the generic idea link and restores submitted choices for editing', async () => {
  setup();let backs=0;const sent=[];
  const composer={draft:'Context',pending:false,setDraft(value){composer.draft=value;},clearError(){}};
  const h=harness('components/chat/ChatDock.tsx',{'./useChatComposer':{useChatComposer:()=>composer}});
  const props={subject:'founder',ventureId:'venture-one',conversationId:'cnv-one',onSend(){},onQuestionBack:async()=>{backs++;},onAction:async(_action,payload)=>{sent.push(payload);return true;},
    messages:[{id:'entry-direction:q1',role:'assistant',body_md:'Who uses it?',actions:[{label:'Just me',action:'chat.reply',payload:{question_id:'entry-direction:q1',text:'Just me'}}]}]};
  const render=()=>nodes(h.render(h.exports.ChatDock,props));
  let all=render();assert.equal(all.some(node=>node.props?.href?.includes('edit_previous')),false);
  await all.find(node=>node.type==='button'&&label(node)==='Back').props.onClick();assert.equal(backs,1);
  all.find(node=>node.props?.['data-usage-control']==='chat_followup_action').props.onClick();all=render();
  await all.find(node=>node.props?.['data-usage-control']==='chat_options_submit').props.onClick();
  props.draftPrefill={id:'back-one',text:'Just me. Changed context',questionId:'entry-direction:q1'};render();for(const effect of h.effects)effect();all=render();
  assert.equal(composer.draft,'Changed context');
  assert.equal(all.find(node=>node.props?.['data-usage-control']==='chat_followup_action').props['aria-pressed'],true);
  await all.find(node=>node.props?.['data-usage-control']==='chat_options_submit').props.onClick();
  assert.equal(sent.length,2);assert.equal(sent[1].text,'Assumed: Just me\nChanged context');
});

test('shipping entry and chat dictation scopes retire when their current clarification changes', async () => {
  setup();const composer={draft:'',pending:false,setDraft(){},clearError(){}};
  const dock=harness('components/chat/ChatDock.tsx',{'./useChatComposer':{useChatComposer:()=>composer}});
  const props={subject:'founder',ventureId:'ven_one',conversationId:'cnv_one',onSend(){},messages:[]};
  const question=id=>({id,role:'assistant',body_md:'Who uses it?',actions:[{label:'Teachers',action:'chat.reply',payload:{question_id:id,text:'Teachers'}}]});
  for(const hideComposer of [false,true]) {
    props.hideComposer=hideComposer;
    props.messages=[question('question-one')];const first=nodes(dock.render(dock.exports.ChatDock,props)).find(node=>node.type==='dictate').props.scope;
    props.messages=[question('question-two')];const second=nodes(dock.render(dock.exports.ChatDock,props)).find(node=>node.type==='dictate').props.scope;
    assert.notEqual(first,second);
  }
  let count=0;const entry=harness('app/new/page.tsx',{
    'next/navigation':{useRouter:()=>({replace(){}})},'@/components/layout/AppShell':{AppShell:'shell'},'./ConfirmedConversation':{},
    '@/components/ventures/BetaPathStrip':{},'@/lib/detailLevel':{useDetailLevel:()=>['brief']},
    '@/lib/client':{verifiedSessionSubject:()=> 'founder',verifiedSessionEpoch:()=>1,ensureSession:async()=>({ok:true}),api:async url=>url.endsWith('/history')?{ok:true,data:{items:[]}}:{ok:true,data:{status:'clarify',id:`entry-q${++count}`,question:'What is the idea?',choices:['An app for teachers']}}},
    '@/components/chat/VoiceMic':{VoiceMic:'voice'},'@/components/chat/DictateButton':{DictateButton:'dictate'},
  },[],'\nmodule.exports.Conversation=NewVentureConversation;');
  const render=()=>nodes(entry.render(entry.exports.Conversation,{}));
  render();entry.effects[2]();await new Promise(resolve=>setImmediate(resolve));
  const opening=render().find(node=>node.type==='dictate').props.scope;
  render().find(node=>node.type==='textarea').props.onChange({target:{value:'Hello'}});
  render().find(node=>node.type==='form').props.onSubmit({preventDefault(){}});await new Promise(resolve=>setImmediate(resolve));
  assert.notEqual(opening,render().find(node=>node.type==='dictate').props.scope);
});


test('confirmed direction buttons and voice actions ignore French model labels', async () => {
  setup();
  const chat={messages:[],conversationKey:'chat-one',conversationId:'cnv-one',contextCurrent:()=>true,reloadHistory:async()=>{},sendChat:async()=>true};
  const h=harness('app/new/ConfirmedConversation.tsx',{
    '@/components/chat/ChatDock':{ChatDock:'chat'},'@/components/chat/useVentureChat':{useVentureChat:()=>chat},
    '@/lib/client':{api:async()=>({ok:true,data:{status:'proposal',proposal:{id:'direction1',route:'startup',state:'pending',message:'New direction',button_label:'Continuer'}}})},
  });
  const props={receipt:{status:'committed',route:'startup',venture_id:'venture-one',workspace_id:'workspace-one',next_step:'idea',action:{kind:'idea',venture_id:'venture-one',href:'/v/venture-one/idea'},message:'Saved'}};
  const render=()=>nodes(h.render(h.exports.ConfirmedConversation,props));
  await render().find(node=>node.type==='chat').props.onSend('Change direction');
  const all=render();
  // The proposal renders as a Charlie message inside the conversation, with fixed labels.
  const labels=all.find(node=>node.type==='chat').props.messages.flatMap(message=>(message.actions||[]).map(action=>action.label));
  assert.ok(labels.includes('Continue'));
  assert.equal(labels.includes('Continuer'),false);
  assert.deepEqual(all.find(node=>node.type==='chat').props.voiceScreen.visible_actions,['Continue','Back']);
});

test('New venture links produce a new scoped route on every deliberate click, including on /new', () => {
  setup(); const paths = [];
  const h = harness('components/chat/NewVentureLink.tsx', {'next/navigation': {useRouter: () => ({push: path => paths.push(path)})}});
  const link = h.render(h.exports.NewVentureLink, {children: 'New venture'});
  assert.equal(link.props.href, '/new?fresh=1');
  let prevented = 0;
  const click = {button: 0, preventDefault() {prevented++;}};
  link.props.onClick(click); link.props.onClick(click);
  assert.equal(prevented, 2); assert.equal(paths.length, 2); assert.notEqual(paths[0], paths[1]);
  assert.ok(paths.every(path => /^\/new\?fresh=[a-f0-9-]+$/.test(path)));
  link.props.onClick({...click, metaKey: true}); assert.equal(paths.length, 2);
});

test('strip and in-card GStack skips submit the identical message through the real idea endpoint', async () => {
  setup(); window.location.pathname = '/v/idea-visible/idea';
  const posts = [], saved = {id: 'session-visible', state: 'clarifying', case: 'know', turns: [{id: 'question-visible', role: 'agent', kind: 'question', text: 'Who uses it?', options: {phase: 'gstack', options: [], allow_free_text: true}}]};
  const h = harness('app/v/[ventureId]/idea/page.tsx', {
    'next/navigation': {useParams: () => ({ventureId: 'idea-visible'}), usePathname: () => window.location.pathname, useSearchParams: () => new URLSearchParams()},
    '@/components/layout/AppShell': {AppShell: 'shell'}, '@/lib/detailLevel': {useDetailLevel: () => ['brief']},
    '@/lib/apiCache': {cacheGet: () => null, cachePut() {}},
    '@/hooks/useDurableJob': {useDurableJob: () => ({job: null, needsRecovery: false})},
    '@/lib/client': {verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, api: async (url, init) => {posts.push({url, body: JSON.parse(init.body)}); return {ok: true, data: {session: saved}};}},
  });
  const render = () => nodes(h.render(h.exports.default));
  render(); h.state[7] = saved; h.state[21] = true;
  let all = render();
  assert.ok(all[0].props.clarificationSkip);
  assert.equal(await all[0].props.clarificationSkip.skip(), true);
  all = render(); all.find(node => node.props?.['data-usage-control'] === 'idea_gstack_skip').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(posts.length, 2); assert.deepEqual(posts[0], posts[1]);
  assert.deepEqual(posts[0], {url: '/api/ventures/idea-visible/idea/sessions/session-visible/messages', body: {text: 'Skip, run the review with what you have'}});
});

test('confirmed conversation uses the current idea projection when preparing a go-to-market quote', async () => {
  setup();const calls=[];
  const latest='A scheduling tool for independent architects';
  const chat={messages:[],conversationKey:'chat-one',conversationId:'cnv-one',contextCurrent:()=>true,reloadHistory:async()=>{},sendChat:async()=>true};
  const h=harness('app/new/ConfirmedConversation.tsx',{
    '@/components/chat/ChatDock':{ChatDock:'chat'},'@/components/chat/useVentureChat':{useVentureChat:()=>chat},
    '@/lib/gtmConversationSend':{gtmSendMode:()=> 'quote'},'@/lib/gtmDiagnosisApi':{founderProductContext:text=>text},
    '@/lib/client':{api:async(url,init)=>{calls.push({url,init});return url.endsWith('/quote')?{ok:true,data:{}}:{ok:true,data:{name:'Scheduler',current_idea:latest,raw_idea:'An app for AI engineers'}};}},
  });
  const props={receipt:{status:'committed',route:'gtm',venture_id:'venture-one',workspace_id:'workspace-one',next_step:'gtm',action:{kind:'gtm_review',venture_id:'venture-one',href:'/gtm?venture=venture-one'},message:'Saved'}};
  const render=()=>nodes(h.render(h.exports.ConfirmedConversation,props)).find(node=>node.type==='chat');
  render();h.effects[1]();await new Promise(resolve=>setImmediate(resolve));
  await render().props.onSend('Help find the first users');
  const quote=calls.find(call=>call.url.endsWith('/quote'));
  assert.equal(JSON.parse(quote.init.body).idea,latest);
});

for (const kind of ['idea.clarify_turn', 'research.deep']) {
  test(`restored credits offer one primary Continue for ${kind} with clean idea text`, async () => {
    setup(); const calls = [];
    const statement = 'A quiet cafe with bookable tables for remote workers';
    let saved = { id: 'ses-one', state: kind === 'research.deep' ? 'confirmed' : 'clarifying', case: 'know', turns: [],
      current_idea: `Latest founder statement (wins over earlier fields): ${statement}\nEarlier fields (may be outdated): hello`,
      idea_summary: statement, current_revision: { id: 'rev-one', summary: 'hello', structured: {} },
      credits_continue_job_id: 'job-one', credits_continue_kind: kind };
    const h = harness('app/v/[ventureId]/idea/page.tsx', {
      'next/navigation': { useParams: () => ({ ventureId: 'venture-one' }), useSearchParams: () => new URLSearchParams(), usePathname: () => '/v/venture-one/idea' },
      '@/components/layout/AppShell': { AppShell: 'shell' }, '@/components/layout/NextStepStrip': { NextStepStrip: 'strip' },
      '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
      '@/hooks/useDurableJob': { useDurableJob: () => ({ job: null, label: '', needsRecovery: false }), jobSucceeded: () => true },
      '@/lib/apiCache': { cacheGet: () => null, cachePut() {} },
      '@/lib/durableJobs': { restoreIdeaSessionJob: async () => ({ id: null, error: '', lastFailure: { id: 'job-one', kind, revision_id: 'rev-one', error_code: 'clarification_insufficient_credits' } }),
        jobFailure: () => 'There are not enough available credits to clarify this idea', reportJobFailure() {} },
      '@/lib/client': { verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
        apiSwr: async () => ({ ok: true, data: { name: 'Coffee Nest', raw_idea: 'hello' } }),
        api: async (url, init) => { calls.push({ url, init }); return { ok: true, data: url.endsWith('/plans') ? [] : url.endsWith('/idea/sessions') ? saved : { job_id: 'job-one' } }; } },
    });
    const render = () => nodes(h.render(h.exports.default));
    render();
    h.state[7] = saved; h.state[8] = { name: 'Coffee Nest', raw_idea: 'hello' }; h.state[21] = true;
    h.state[25] = 'There are not enough available credits to clarify this idea';
    let all = render();
    assert.ok(all.some(node => label(node) === statement));
    assert.doesNotMatch(label(all[0]), /Earlier fields|Latest founder statement|hello|not enough available credits/);
    const button = all.find(node => node.type === 'button' && label(node) === 'Continue');
    assert.equal(button.props.className, 'primary');
    assert.equal(button.props['data-usage-control'], 'idea_credits_continue');
    assert.equal(button.props.disabled, false);
    assert.equal(all[0].props.ideaStatus.action.label, 'Continue');
    all[0].props.ideaStatus.action.onClick(); button.props.onClick(); await new Promise(resolve => setImmediate(resolve));
    const posts = calls.filter(call => call.init?.method === 'POST');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, kind === 'research.deep' ? '/api/ventures/venture-one/research/dossier/regenerate' : '/api/ventures/venture-one/idea/sessions/ses-one/retry-clarification');
    assert.deepEqual(JSON.parse(posts[0].init.body), { failed_job_id: 'job-one' });
    assert.equal(render().filter(node => node.type === 'button' && label(node) === 'Continue').length, 0);

    h.state[27] = null; h.state[3] = { current: false }; h.state[7] = { ...saved, credits_continue_job_id: null }; h.state[25] = 'There are not enough available credits to clarify this idea';
    all = render();
    assert.equal(all.filter(node => node.type === 'button' && label(node) === 'Continue').length, 0);
    all.find(node => node.type === 'button' && label(node) === 'Check saved journey status').props.onClick();
    await new Promise(resolve => setImmediate(resolve));
    all = render();
    assert.ok(all.some(node => node.type === 'button' && label(node) === 'Continue'));
    assert.doesNotMatch(label(all[0]), /not enough available credits|Earlier fields|hello/);
    assert.equal(calls.filter(call => call.url.endsWith('/retry-clarification') || call.url.endsWith('/regenerate')).length, 1);
  });
}


test('an entry direction with no choices retains its own Back and free-text composer', async () => {
  setup();const cancelled=[];
  const chat={conversationKey:'chat-one',conversationId:'cnv-one',contextCurrent:()=>true,reloadHistory:async()=>{},sendChat:async()=>true,
    messages:[{id:'old-idea-question',role:'assistant',body_md:'Old venture question',actions:[{label:'Teachers',action:'chat.reply',payload:{question_id:'old-idea-question',text:'Teachers'}}]}]};
  const h=harness('app/new/ConfirmedConversation.tsx',{
    '@/components/chat/ChatDock':{ChatDock:'chat'},'@/components/chat/useVentureChat':{useVentureChat:()=>chat},
    '@/lib/client':{api:async(url)=>{if(url.endsWith('/cancel')){cancelled.push(url);return {ok:true,data:{status:'cancelled'}};}return {ok:true,data:{status:'clarify',id:'empty-direction',question:'Describe your new direction',choices:[]}};}},
  });
  const props={receipt:{status:'committed',route:'startup',venture_id:'venture-one',workspace_id:'workspace-one',next_step:'idea',action:{kind:'idea',venture_id:'venture-one',href:'/v/venture-one/idea'},message:'Saved'}};
  const render=()=>nodes(h.render(h.exports.ConfirmedConversation,props));
  await render().find(node=>node.type==='chat').props.onSend('Change direction');
  let all=render();const dock=all.find(node=>node.type==='chat');
  assert.equal(dock.props.messages[0].actions.length,0);
  assert.deepEqual(dock.props.voiceScreen.visible_actions,['Send','Back']);
  assert.equal(all.some(node=>node.props?.href?.includes('edit_previous')),false);
  all.find(node=>node.type==='button'&&label(node)==='Back').props.onClick();await new Promise(resolve=>setImmediate(resolve));
  all=render();assert.deepEqual(cancelled,['/api/entry/chat/proposals/empty-direction/cancel']);
  assert.equal(all.find(node=>node.type==='chat').props.draftPrefill.text,'Change direction');
});

for (const succeeds of [true, false]) {
  test(`question answer leaves the box at once and returns only on failure (succeeds=${succeeds})`, async () => {
    setup(); let finish; const drafts = [], composer = { draft: 'Dog walkers in Leeds', pending: false, setDraft(v) { drafts.push(v); composer.draft = v; }, clearError() {} };
    const h = harness('components/chat/ChatDock.tsx', { './useChatComposer': { useChatComposer: () => composer } });
    const props = { messages: [{ id: 'q', role: 'assistant', kind: 'idea_clarification_ready', body_md: 'Who is it for?',
      actions: [{ label: 'Clinics', action: 'chat.reply', payload: { question_id: 'q1', text: 'Clinics' } }] }],
      onSend() {}, onAction: () => new Promise(resolve => { finish = resolve; }) };
    const submit = nodes(h.render(h.exports.ChatDock, props)).filter(node => node.type === 'button' && label(node) === 'Send').at(-1);
    const done = submit.props.onClick({ preventDefault() {} });
    assert.equal(composer.draft, '', 'the box is empty while the answer is being processed');
    h.render(h.exports.ChatDock, props); // React re-renders after the box clears
    finish(succeeds ? undefined : false); await done;
    assert.equal(composer.draft, succeeds ? '' : 'Dog walkers in Leeds');
  });
}

test('completed validation names idea confirmation on the pinned new path', async () => {
  for (const newPath of [false, true]) {
    setup(); window.location.pathname = '/v/validation-next/idea';
    const { validationMethods } = require('../../lib/validationCopy.ts');
    const session = { id: 'idea-validation', state: 'validating', case: 'know', turns: [],
      current_revision: { id: 'reviewed-revision', structured: {} },
      validation_runs: validationMethods.map(method => ({ id: method, method, state: 'complete', revision_id: 'reviewed-revision', report: {} })),
      ...(newPath ? { next_href: '/v/validation-next/context' } : {}) };
    const h = harness('app/v/[ventureId]/idea/page.tsx', {
      'next/navigation': { useParams: () => ({ ventureId: 'validation-next' }), usePathname: () => window.location.pathname, useSearchParams: () => new URLSearchParams() },
      '@/components/layout/AppShell': { AppShell: 'shell' }, '@/components/layout/NextStepStrip': { NextStepStrip: 'steps' },
      '@/components/chat/DictateButton': { DictateButton: 'dictate' }, '@/lib/detailLevel': { useDetailLevel: () => ['brief'] },
      '@/lib/apiCache': { cacheGet: () => null, cachePut() {} }, '@/hooks/useDurableJob': { useDurableJob: () => ({ job: null }) },
      '@/lib/durableJobs': { restoreIdeaSessionJob: async () => ({ id: null, error: null }), reportJobFailure() {}, jobFailure: () => '' },
      '@/lib/client': { verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => 1, ensureSession: async () => ({ ok: true }),
        apiSwr: async () => ({ ok: true, data: { name: 'Validation', workspace_id: 'workspace' } }),
        api: async url => ({ ok: true, data: url.endsWith('/plans') ? { items: [] } : session }) },
    });
    h.render(h.exports.default, {}); h.effects[0](); await new Promise(resolve => setImmediate(resolve));
    const tree = h.render(h.exports.default, {}), all = nodes(tree);
    const primary = all.find(node => node.props?.['data-usage-control'] === 'idea_continue_research');
    assert.equal(label(primary), newPath ? 'Confirm your idea' : 'Continue to research');
    assert.equal(all.some(node => node.props?.['data-usage-control'] === 'idea_confirm_for_website'), !newPath);
    if (newPath) assert.equal(tree.props.ideaStatus.activity, 'Review your startup and priorities');
  }
});
