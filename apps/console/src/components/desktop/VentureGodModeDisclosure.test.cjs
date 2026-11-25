const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

function loadLibrary(name) {
  const exports = {};
  const compiled = ts.transpileModule(readFileSync(join(__dirname, `../../lib/${name}.ts`), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compiled, { exports, require: id => loadLibrary(id.replace(/^\.\//, '').replace(/\.ts$/, '')) });
  return exports;
}
const betaPath = loadLibrary('betaPath'), nextStep = loadLibrary('nextStep');

function harness({ cachedSnapshot = true, overviewSnapshot, nextStepReceipt, copyFails = false } = {}) {
  let venture = 'venture-a', owner = 'account-a', epoch = 1, context, tree, effects = [], active;
  const contexts = new Map(), listeners = new Map(), viewed = [];
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const react = {
    useContext: () => 'classic',
    useState(initial) {
      const index = context.cursor++;
      const hook = context.hooks[index] ||= { value: typeof initial === 'function' ? initial() : initial };
      return [hook.value, next => { hook.value = typeof next === 'function' ? next(hook.value) : next; }];
    },
    useRef(initial) { const index = context.cursor++; return context.hooks[index] ||= { current: initial }; },
    useEffect(fn, deps) {
      const index = context.cursor++, hooks = context.hooks, before = hooks[index];
      if (!before || !same(before.deps, deps)) effects.push(() => {
        before?.cleanup?.(); hooks[index] = { deps, cleanup: fn() };
      });
    },
  };
  // Preserve the same keyed component lifetime React uses; discard unmounted hook state.
  function jsx(type, props, key) {
    if (typeof type !== 'function') return { type, props: props || {}, key };
    const id = `${type.name}:${key ?? ''}`, parent = context;
    context = contexts.get(id) || { hooks: [], cursor: 0 };
    contexts.set(id, context); active.add(id); context.cursor = 0;
    try { return { ...type(props || {}), key }; } finally { context = parent; }
  }
  const snapshot = overviewSnapshot || { company: { name: 'Owned cached venture', workspace_id: 'ws-fixture', stage: 'draft' },
    ideas: null, plans: null, products: null, jobs: null, hasAcceptedBuild: false, websitePublished: false };
  const requireModule = id => {
    if (id === '@/components/chat/ChatLink') return { default: props => ({ type: 'a', props }), ChatAnchor: 'a', captureChatLink() {} };
    if (id === 'react') return react;
    if (id === '@/lib/founderUI') return {FounderUIContext: {}};
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
    if (id === 'next/navigation') return { useParams: () => ({ ventureId: venture }) };
    if (id === 'next/link') return { default: props => ({ type: 'a', props }) };
    if (id === '@/lib/client') return {
      verifiedSessionSubject: () => owner, verifiedSessionEpoch: () => epoch,
      // Hold fresh reads pending; this test exercises an owned cached snapshot with no next-step decision.
      ensureSession: () => new Promise(() => {}), api: () => { throw new Error('unexpected fresh read'); }, prefetchApi: () => {},
    };
    if (id === '@/lib/apiCache') return { cacheGet: () => cachedSnapshot ? ({ data: snapshot }) : null, cachePut: () => {} };
    if (id === '@/lib/companyJourney') return { companyJourneyStep: () => null, companyWorkState: () => null };
    if (id === '@/lib/nextStep') return overviewSnapshot ? nextStep : { selectNextStep: () => null };
    if (id === '@/lib/betaPath') return overviewSnapshot ? betaPath : { BETA_PATH: [], betaStageIndex: () => null };
    if (id === '@/lib/gtmDiagnosisApi') return { founderProductContext: () => '' };
    if (id === '@/lib/founderAssetsApi') return { safeSiteUrl: value => value || undefined };
    if (id === '@/lib/apiFailure') return { apiFailure: () => 'fixture unavailable' };
    if (id === '@/components/layout/NextStepStrip') return { useNextStep: () => nextStepReceipt || null,
      JourneyChoices: ({ restore }) => ({ type: 'button', props: { children: restore ? 'Restore this step' : 'Skip this step' } }),
      NextStepButton: ({ step }) => ({ type: 'button', props: { children: nextStep.hasPageChoices(step) ? 'Skip this step' : step.cta_label } }) };
    if (id === '@/components/layout/AppShell') return { AppShell: props => ({ type: 'shell', props }) };
    if (id === '@/components/layout/LocalModeBoundary') return { useLocalMode: () => false };
    if (id === '@/components/primitives/OneCard') return {
      OneCard: props => ({ type: 'step-card', props: { ...props, children: [props.children, props.details, props.actions] } }),
      OneCardStack: props => ({ type: 'stack', props }),
    };
    if (id === '@/components/settings/GodModeRequestCard') return { GodModeRequestCard: function GodModeRequestCard({ ventureId }) {
      react.useEffect(() => { viewed.push(ventureId); }, []);
      return { type: 'request-card', props: { ventureId } };
    } };
    if (id === '@/components/primitives/ErrorNotice') return { ErrorNotice: () => null };
    if (id === '@/components/ui/InlineSpinner') return { InlineSpinner: () => null };
    if (id === '@/components/ventures/BetaPathStrip') return { BetaPathStrip: () => null };
    if (id === '@/components/ventures/FounderStageCard') return { FounderStageCard: props => ({ type: 'stage-card', props }) };
    if (id === '@/components/credits/CompanyCreditSummary') return { CompanyCreditSummary: () => null };
    if (id === '@/components/connections/GmailReconnectBanner') return { GmailReconnectBanner: () => null };
    if (id.endsWith('.module.css')) return { default: {} };
    throw new Error(`unexpected import: ${id}`);
  };
  const file = join(__dirname, '../../app/v/[ventureId]/page.tsx');
  const output = ts.transpileModule(readFileSync(file, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const exports = {};
  vm.runInNewContext(output, { exports, require: requireModule, URLSearchParams,
    navigator: { clipboard: { writeText: async () => { if (copyFails) throw new Error('Copy denied'); } } },
    window: { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) },
    document: { getElementById: () => null },
  });
  function render() {
    active = new Set(); tree = jsx(exports.default, {});
    for (const [id, value] of contexts) if (!active.has(id)) {
      value.hooks.forEach(hook => hook?.cleanup?.()); contexts.delete(id);
    }
    const pending = effects; effects = []; pending.forEach(fn => fn());
    return tree;
  }
  return { viewed, render, get tree() { return tree; },
    venture(next) { venture = next; render(); render(); },
    session(nextOwner, nextEpoch) {
      owner = nextOwner; epoch = nextEpoch; listeners.get('supymem-session-changed')?.(); render(); render();
    },
  };
}
function nodes(tree) {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return [tree, ...nodes(tree.props?.children)];
}
const card = h => nodes(h.tree).find(node => node.type === 'request-card');
const disclosure = h => nodes(h.tree).find(node => node.type === 'details');
function toggle(h, open) { disclosure(h).props.onToggle({ currentTarget: { open } }); h.render(); }

test('actual VenturePage only mounts God mode interest after explicit opening and retires every scope', () => {
  const h = harness(); h.render(); h.render();
  assert.ok(disclosure(h), 'the independent access control exists without a next-step decision');
  assert.equal(card(h), undefined); assert.deepEqual(h.viewed, []);
  toggle(h, true); assert.equal(card(h).props.ventureId, 'venture-a'); assert.deepEqual(h.viewed, ['venture-a']);
  toggle(h, false); assert.equal(card(h), undefined);
  toggle(h, true); h.venture('venture-b'); assert.equal(card(h), undefined);
  const count = h.viewed.length;
  h.venture('venture-a'); assert.equal(card(h), undefined); assert.equal(h.viewed.length, count, 'A→B→A must not retain the old open state');
  toggle(h, true); h.session('account-b', 2); assert.equal(card(h), undefined);
  toggle(h, true); h.session('account-b', 3); assert.equal(card(h), undefined, 'same-account epoch retirement also closes the disclosure');
  toggle(h, true); assert.equal(card(h).props.ventureId, 'venture-a');
});

test('actual VenturePage retains the clicked access request when journey state has never loaded', () => {
  const h = harness({ cachedSnapshot: false }); h.render(); h.render();
  assert.ok(disclosure(h)); assert.equal(card(h), undefined); assert.deepEqual(h.viewed, []);
  toggle(h, true); assert.equal(card(h).props.ventureId, 'venture-a');
  h.venture('venture-b'); assert.equal(card(h), undefined);
  toggle(h, true); assert.equal(card(h).props.ventureId, 'venture-b');
  h.session('account-b', 2); assert.equal(card(h), undefined);
});

test('U53 a queued clarification retains progress and opens its same-venture conversation', () => {
  const step = { key: 'work.running', stage: 'Clarify', title: 'Preparing your next question',
    detail: 'Preparing your next question — usually under a minute.', cta_label: 'View progress',
    href: '/v/venture-a/now', chat_action: null, payload: null };
  const h = harness({ nextStepReceipt: step, overviewSnapshot: {
    company: { name: 'Supymem', workspace_id: 'ws-fixture', stage: 'draft' },
    ideas: [{ id: 'idea-a', state: 'clarifying' }], plans: [], products: [],
    jobs: [{ id: 'clarify-a', kind: 'idea.clarify_turn', status: 'scheduled' }],
    serverNextStep: step, hasAcceptedBuild: false, websitePublished: false,
  } });
  h.render();
  assert.ok(nodes(h.tree).some(node => node.type === 'a' && node.props.href === '/new?venture=venture-a' && node.props.children === 'Open conversation'));
  assert.ok(nodes(h.tree).some(node => node.type === 'button' && node.props.children === 'View progress'));
});

test('Overview has one stage card and distinct sibling keys throughout refresh and scope changes', () => {
  const h = harness({ overviewSnapshot: overview() });
  for (let refresh = 0; refresh < 5; refresh++) {
    h.render(); h.render();
    const children = nodes(h.tree).find(node => node.type === 'div' && Array.isArray(node.props?.children)).props.children;
    const keys = children.filter(child => child && child.key != null).map(child => child.key);
    assert.equal(new Set(keys).size, keys.length, 'sibling key collisions can leave orphaned cards during reconciliation');
    assert.equal(nodes(h.tree).filter(node => node.type === 'stage-card').length, 1);
  }
  h.session('account-a', 2);
  assert.equal(nodes(h.tree).filter(node => node.type === 'stage-card').length, 1);
});

function overview({ key = 'gtm.outreach', published = true, intakePath = 'startup' } = {}) {
  return { company: { name: 'RecallDesk', raw_idea: 'Rebook patients instead of losing them\nAudience: Independent dental clinics in the US',
      workspace_id: 'ws-fixture', stage: 'draft', intake_path: intakePath },
    ideas: [{ id: 'idea-fixture', state: 'confirmed' }], plans: [], products: [], jobs: [], hasAcceptedBuild: false,
    websitePublished: published, websiteUrl: published ? 'https://recalldesk.supymem.com' : undefined,
    serverNextStep: { key, stage: 'Outreach', title: key === 'gtm.outreach' ? 'Find your first customers' : 'Your landing page is ready',
      detail: 'Visit the landing page, copy its link, or continue to Find customers.', cta_label: 'Find customers', href: '/gtm?venture=venture-a',
      site_url: published ? 'https://recalldesk.supymem.com' : undefined,
      founder_stage: { stage: published ? 'outreach' : 'building', confirmed_by_founder: false } } };
}
const stepCard = (h, key) => nodes(h.tree).find(node => node.type === 'step-card' && node.props.id === `step-${key}`);
test('landing-page next step has one skip control, and other steps retain theirs', () => {
  for (const key of ['site.build', 'research.start']) {
    const snapshot = overview({ key, published: false });
    snapshot.serverNextStep.title = 'Choose your next step';
    snapshot.serverNextStep.skip_step = key === 'site.build' ? 'landing_page' : 'research';
    if (key === 'site.build') snapshot.serverNextStep.alternatives = [
      { choice: 'build', label: 'Build the landing page', href: '/v/venture-a/site' },
      { choice: 'skip', label: 'Find customers', href: '/gtm?venture=venture-a' },
    ];
    const h = harness({ overviewSnapshot: snapshot, nextStepReceipt: snapshot.serverNextStep }); h.render(); h.render();
    const card = nodes(h.tree).find(node => node.type === 'step-card' && node.props.title === 'Choose your next step');
    assert.ok(card, key);
    assert.equal(nodes(card.props.actions).filter(node => node.props?.children === 'Skip this step').length, 1, key);
  }
});
test('overview remains usable when all prior records and the idea summary are absent', () => {
  const snapshot = overview({ key: 'idea.review_validation', published: false });
  snapshot.company.raw_idea = '';
  snapshot.ideas = [];
  snapshot.serverNextStep.skip_step = 'validation';
  snapshot.serverNextStep.journey_overrides = { target: 'validation', skipped: { clarification: { who: 'account-a', when: '2026-10-12', why: 'Skip the interview' } } };
  const h = harness({ overviewSnapshot: snapshot, nextStepReceipt: snapshot.serverNextStep }); h.render(); h.render();
  assert.match(text(h.tree), /No idea summary is saved; you can continue with drafts/);
  assert.match(stepCard(h, 'idea').props.body, /Skipped by you/);
  assert.ok(nodes(stepCard(h, 'page').props.actions).some(node => node.props.href === '/v/venture-a/site?create=1'));
});
test('founder overrides show Skipped by you, never Done, and offer restoration', () => {
  const snapshot = overview();
  snapshot.serverNextStep.journey_overrides = { skipped: {
    clarification: { who: 'account-a', when: '2026-10-12', why: 'Use my website' },
    research: { who: 'account-a', when: '2026-10-12', why: 'I insist' },
    landing_page: { who: 'account-a', when: '2026-10-12', why: 'Already built' },
  }};
  const h = harness({ overviewSnapshot: snapshot, nextStepReceipt: snapshot.serverNextStep }); h.render(); h.render();
  for (const key of ['idea', 'page', 'publish', 'waitlist', 'research']) {
    const card = stepCard(h, key);
    assert.match(card.props.body, /Skipped by you/);
    assert.doesNotMatch(card.props.body, /Done/);
    assert.equal(card.props.status, 'idle');
    assert.ok(nodes(card.props.actions).some(node => node.props.children === 'Restore this step'));
  }
});
test('restored validation remains actionable while clarification is skipped', () => {
  const snapshot = overview({ key: 'idea.review_validation', published: false });
  snapshot.serverNextStep.cta_label = 'Review idea';
  snapshot.serverNextStep.journey_overrides = { target: 'validation', skipped: { clarification: { who: 'account-a', when: '2026-10-12', why: 'Use summary' } } };
  const h = harness({ overviewSnapshot: snapshot, nextStepReceipt: snapshot.serverNextStep }); h.render(); h.render();
  const actions = nodes(stepCard(h, 'idea').props.actions);
  assert.ok(actions.some(node => node.props.children === 'Review idea'));
  assert.ok(actions.some(node => node.props.children === 'Restore this step'));
});
test('a single skipped later step does not move an unfinished idea out of the current card', () => {
  const snapshot = overview({ key: 'idea.clarify', published: false });
  snapshot.ideas[0].state = 'clarifying';
  snapshot.serverNextStep.title = 'Answer the next question';
  snapshot.serverNextStep.skip_step = 'clarification';
  snapshot.serverNextStep.journey_overrides = { target: 'landing_page', skipped: { research: { who: 'account-a', when: '2026-10-12', why: 'No research' } } };
  const h = harness({ overviewSnapshot: snapshot, nextStepReceipt: snapshot.serverNextStep }); h.render(); h.render();
  assert.equal(stepCard(h, 'idea').props.title, 'Answer the next question');
  assert.doesNotMatch(stepCard(h, 'idea').props.body, /Done/);
});
function text(tree) {
  if (tree == null) return '';
  if (typeof tree !== 'object') return String(tree);
  return Array.isArray(tree) ? tree.map(text).join(' ') : text(tree.props?.children);
}

for (const intakePath of ['startup', 'landing_gtm']) {
  test(`live ${intakePath} overview completes page steps and opens outreach and optional research`, () => {
    const h = harness({ overviewSnapshot: overview({ intakePath }) }); h.render(); h.render();
    for (const key of ['idea', 'page', 'publish', 'waitlist']) assert.equal(stepCard(h, key).props.status, 'done', key);
    assert.equal(stepCard(h, 'outreach').props.status, 'idle');
    assert.equal(stepCard(h, 'research').props.status, 'idle');
    assert.equal(stepCard(h, 'waitlist').props.title, 'Your landing page is live');
    const pageActions = nodes(stepCard(h, 'waitlist').props.actions);
    assert.ok(pageActions.some(node => node.type === 'a' && node.props.children === 'Find customers' && node.props.className === 'primary'));
    assert.ok(pageActions.some(node => node.type === 'a' && node.props.href === 'https://recalldesk.supymem.com'));
    assert.ok(pageActions.some(node => node.type === 'button' && node.props.children === 'Copy link'));
    assert.ok(nodes(stepCard(h, 'research').props.actions).some(node => node.props.href === '/v/venture-a/research'));
    const details = nodes(stepCard(h, 'outreach').props.details).filter(node => node.type === 'p').map(node => text(node));
    assert.ok(details.includes('For: Independent dental clinics in the US'));
    assert.ok(details.includes(intakePath === 'startup' ? 'Idea confirmed.' : 'Next: review your go-to-market context.'));
    assert.doesNotMatch(details.join(' '), /Audience:|Current stage: confirmed/);
    assert.equal(nodes(h.tree).find(node => node.type === 'stage-card').props.stageRecord.stage, 'outreach');
  });
}
test('optional research is usable before publication without unlocking unfinished outreach', () => {
  const h = harness({ overviewSnapshot: overview({ key: 'site.ready', published: false }) }); h.render(); h.render();
  assert.equal(stepCard(h, 'waitlist').props.status, 'idle');
  assert.equal(stepCard(h, 'outreach').props.status, 'locked');
  assert.equal(stepCard(h, 'research').props.status, 'idle');
});
test('fresh stage and next-step receipts replace stale preview positioning together', () => {
  const stale = overview({ key: 'site.ready', published: false }), fresh = overview().serverNextStep;
  const h = harness({ overviewSnapshot: stale, nextStepReceipt: fresh }); h.render(); h.render();
  assert.equal(stepCard(h, 'outreach').props.status, 'idle');
  assert.equal(stepCard(h, 'waitlist').props.title, 'Your landing page is live');
  assert.equal(nodes(h.tree).find(node => node.type === 'stage-card').props.stageRecord, fresh.founder_stage);
});
for (const copyFails of [false, true]) {
  test(`copy link reports ${copyFails ? 'failure without claiming success' : 'success'}`, async () => {
    const h = harness({ overviewSnapshot: overview(), copyFails }); h.render(); h.render();
    await nodes(stepCard(h, 'waitlist').props.actions).find(node => node.type === 'button').props.onClick(); h.render();
    assert.match(text(stepCard(h, 'waitlist').props.actions), copyFails ? /Could not copy the link/ : /Link copied/);
  });
}
