const chatRoutes = require("../../lib/chatRouteAction.ts");
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

// Execute the shipping shell, page, strip and choices together. Only unrelated
// shell children, native availability, browser primitives and transport receipts
// are synthetic. No native identity or global is injected into a real browser.
// This modeled lifecycle check is not a browser, backend or founder acceptance.
function composition({ shellOnly = false, founderUI = 'classic', path = '/v/venture/research', unknown = false,
  noReport = false, recovery: recoveryOverride = {}, step: stepOverride, runState = 'idle', readFailure = false,
  reportState = 'partial', stoppedEarly = false, nextQuestions = [], competitorChecks = [], progress,
  findings: savedFindings, summary: savedSummary = {} } = {}) {
  const scope = { owner: 'usr_fixture', epoch: 1, path };
  const url = () => new URL('http://127.0.0.1:57399' + scope.path);
  const listeners = new Map(), timers = new Map(), instances = new Map(), modules = new Map();
  const requests = [], navigations = [], events = [], executed = new Set();
  let active, cursor = 0, dirty = true, sequence = 0, effects = [], tree, finishRerun, holdRead = false, finishRead;
  const location = { get pathname() { return url().pathname; }, get href() { return url().href; },
    get hostname() { return url().hostname; }, get search() { return url().search; }, assign(href) { navigations.push(href); } };
  const window = { location, addEventListener(name, callback) { const list = listeners.get(name) || new Set(); list.add(callback); listeners.set(name, list); },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
    dispatchEvent(event) { events.push(event); listeners.get(event.type)?.forEach(callback => callback(event)); },
    setTimeout: () => 1, clearTimeout() {} };
  const report = { id: 'report', kind: 'deep', revision_id: 'revision', state: reportState, findings: unknown
    ? { source: [{ id: 'lead', title: 'Synthetic related page', category: 'source' }] } : {},
    summary: { coverage_gaps: ['Customer evidence is unverified.', 'YC directory matching is unavailable while data-use rights are reviewed.'],
      stopped_early: stoppedEarly, next_questions: nextQuestions, review: { competitor_checks: competitorChecks },
      verdict: { title: 'Review the evidence', verdict: 'test_first',
      source_count: unknown ? 1 : 0, why: ['Synthetic saved research.'], why_not: ['Demand is unverified.'] },
      ...(unknown ? { evidence_dimensions: { pricing: { status: 'unknown' } } } : {}), ...savedSummary } };
  if (savedFindings) report.findings = savedFindings;
  const recovery = { state: 'confirmed', revisionId: 'revision', personal: false, jobId: null, error: '', ...recoveryOverride };
  const dashboard = { venture: { id: 'venture' }, run_status: { state: runState }, next_step: stepOverride === undefined ? { key: 'site.build', stage: 'Page',
    title: 'Choose your next step', detail: 'Choose a private landing page preview or customers first.',
    cta_label: 'Build the landing page and explore customers', href: '/v/venture/site?build=1', chat_action: null, payload: null,
    alternatives: [{ choice: 'build', label: 'Build the landing page', href: '/v/venture/site?build=1' },
      { choice: 'skip', label: 'Find customers first', href: '/gtm?venture=venture' }] } : stepOverride };
  const jsx = (type, props) => ({ type, props });
  const react = {
    Suspense: 'Suspense', createContext: value => ({ value }), useContext: context => context.value,
    useState(initial) {
      const i = cursor++, owner = active;
      if (!owner.hooks[i]) owner.hooks[i] = { value: typeof initial === 'function' ? initial() : initial };
      const state = owner.hooks[i];
      state.set ||= value => { const next = typeof value === 'function' ? value(state.value) : value;
        if (!Object.is(next, state.value)) { state.value = next; dirty = true; } };
      return [state.value, state.set];
    },
    useRef(initial) { return active.hooks[cursor++] ||= { current: initial }; },
    useEffect(callback, deps) {
      const i = cursor++, owner = active, before = owner.hooks[i];
      if (!before || !deps || deps.some((value, index) => value !== before.deps?.[index])) {
        owner.hooks[i] = { deps, setup: callback, cleanup: before?.cleanup };
        effects.push(() => { before?.cleanup?.(); owner.hooks[i].cleanup = callback(); });
      }
    },
  };
  react.useLayoutEffect = react.useEffect;
  const router = { push: href => navigations.push(href), replace: href => navigations.push(href) };
  const client = {
    verifiedSessionSubject: () => scope.owner, verifiedSessionEpoch: () => scope.epoch,
    ensureSession: async () => ({ ok: true, status: 200, data: { authenticated: true, subject: scope.owner } }), resolveGateway: () => 'http://127.0.0.1:57399',
    apiSwr: async (_path, callback) => { callback({ ok: true, data: dashboard }); return { ok: true, data: dashboard }; },
    api: async (requestPath, options = {}) => {
      requests.push({ path: requestPath, ...options });
      if (requestPath.endsWith('/dashboard')) return { ok: true, data: dashboard };
      if (requestPath.endsWith('/rerun')) return new Promise(resolve => finishRerun = resolve);
      if (holdRead && requestPath.endsWith('/research/dossiers')) return new Promise(resolve => finishRead = () => {
        holdRead = false; resolve({ ok: true, data: { items: noReport ? [] : [report] } });
      });
      if (readFailure && requestPath.endsWith('/research/dossiers')) return { ok: false, status: 503, data: {} };
      return { ok: true, data: requestPath.endsWith('/research/dossiers') ? { items: noReport ? [] : [report] }
        : requestPath.includes('/research/dossiers/') ? report
        : requestPath === '/api/ventures' ? { items: [] }
        : requestPath.endsWith('/plans') ? { items: [] }
        : { id: 'venture', workspace_id: 'workspace', manager_name: 'Charlie' } };
    },
  };
  const root = join(__dirname, '../..');
  const actual = {
    '@/components/layout/AppShell': 'components/layout/AppShell.tsx',
    '@/components/layout/NextStepStrip': 'components/layout/NextStepStrip.tsx',
    './NextStepStrip': 'components/layout/NextStepStrip.tsx',
    '@/components/site/PageChoices': 'components/site/PageChoices.tsx',
    '@/components/primitives/ResearchProgress': 'components/primitives/ResearchProgress.tsx',
    '@/components/research/ResearchBrief': 'components/research/ResearchBrief.tsx',
    './ResearchBrief': 'components/research/ResearchBrief.tsx',
    '@/components/research/PriorArt': 'components/research/PriorArt.tsx',
    '@/lib/nextStep': 'lib/nextStep.ts', '@/lib/researchNextStep': 'lib/researchNextStep.ts',
    '@/lib/researchPresentation': 'lib/researchPresentation.ts', '@/lib/researchBrief': 'lib/researchBrief.ts',
  };
  const primitive = names => Object.fromEntries(names.map(name => [name, name]));
  function resolve(id) {
    if (id === '@/components/chat/ChatLink') return { default: 'a', ChatAnchor: 'a', captureChatLink() {} };
    if (id === '@/lib/chatRouteAction') return chatRoutes;
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'Fragment' };
    if (id === 'next/link') return { default: 'a' };
    if (id === 'next/navigation') return { useParams: () => ({ ventureId: 'venture' }), usePathname: () => location.pathname,
      useSearchParams: () => new URLSearchParams(), useRouter: () => router };
    if (id === '@/lib/client') return client;
    if (id === '@/lib/founderUI') return { useFounderUI: () => founderUI, useFounderUIState: () => ({ layout: founderUI, loaded: true }), FounderUIContext: react.createContext(founderUI) };
    if (id === '@/components/office/CompanyOffice') return { CompanyOffice: ({ children, nextStep, controls }) => jsx('div', {
      children: [jsx('header', { children: [nextStep, controls] }), jsx('main', { children })],
    }) };
    if (id === '@/lib/taskApi') return { captureFounderTimezone: async () => ({ timezone: 'UTC', confirmed: true }) };
    if (id === '@/lib/apiCache') return { cacheGet: owner => owner === 'usr_fixture' ? { data: dashboard } : null };
    if (actual[id]) return compile(actual[id]);
    if (id === '@/lib/researchRecovery') return { ...compile('lib/researchRecovery.ts'),
      loadResearchRecovery: async () => recovery, researchReceiptMatches: () => true };
    if (id === '@/hooks/useDurableJob') return { jobSucceeded: job => job.status === 'succeeded',
      useDurableJob: id => ({ job: id ? { id, status: 'scheduled', progress } : null, label: 'Queued', error: '' }) };
    if (id === '@/lib/jobCopy') return { plainJob: () => ({ title: 'Research stopped', body: 'Review saved status.' }) };
    if (id === '@/lib/apiFailure') return { apiFailure: () => 'Research could not be confirmed. Refresh saved status.' };
    if (id === '@/lib/durableJobs') return { reportJobFailure() {}, jobFailure: () => '' };
    if (id === '@/lib/detailLevel') return { useDetailLevel: () => ['brief'] };
    if (id === '@/components/office/OfficeProvider') return { OfficeProvider: 'OfficeProvider', useOfficeOptional: () => null };
    if (id === '@/components/chat/ChatDock') return { usePersistedDockSize: () => [{ width: 360 }, () => {}] };
    if (id === '@/lib/desktop') return { isDesktopShell: () => true, desktopInvoke: async () => null, desktopListen: async () => () => {} };
    if (id === '@/lib/operationsScope') return { isCompanyOperationsPath: () => false, ventureQueryId: () => undefined,
      shellVentureId: value => value.pathVentureId };
    if (id === '@/lib/chatVisibility') return { isIdeaJourneyPath: () => false, showsDetailToggle: () => false, chatFirstHidden: require(join(__dirname, '../../lib/chatVisibility.ts')).chatFirstHidden };
    if (id === '@/lib/usageCollectorState') return { listenForUsageSubject: () => () => {} };
    if (id === '@/lib/deviceIntent') return { readDeviceIntent: () => null, continueDeviceApproval() {} };
    if (id === '@/lib/inviteCode') return { normalizeInviteCode: value => value, recallInvite: () => '', rememberInvite() {} };
    if (id === '@/lib/cx') return { cx: (...values) => values.filter(Boolean).join(' ') };
    if (id.endsWith('.module.css')) return { default: new Proxy({}, { get: (_, key) => String(key) }) };
    if (id.endsWith('/ResearchBrief')) return primitive(['CoverageMeter', 'KeyLines', 'Reveal', 'RiskLine', 'RunningShimmer', 'SourceChip', 'Stat', 'StrengthBar', 'VerdictBadge']);
    if (id.endsWith('/OneCard')) return primitive(['OneCard', 'OneCardStack']);
    return new Proxy(primitive([id.split('/').at(-1)]), { get: (target, key) => key in target ? target[key] : () => false });
  }
  function compile(relative) {
    if (modules.has(relative)) return modules.get(relative);
    const exports = {}; modules.set(relative, exports);
    const code = ts.transpileModule(readFileSync(join(root, relative), 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
    } }).outputText;
    vm.runInNewContext(code, { exports, require: resolve, URL, URLSearchParams, window, document: { hidden: false,
      getElementById: () => ({ scrollIntoView() {} }) }, localStorage: { getItem: () => null, setItem() {} },
      setInterval: callback => { timers.set(++sequence, callback); return sequence; }, clearInterval: id => timers.delete(id),
      crypto: { randomUUID: () => 'synthetic-rerun-key' }, CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
      requestAnimationFrame() {}, console }, { filename: relative });
    return exports;
  }
  const Page = compile('app/v/[ventureId]/research/page.tsx').default;
  const Shell = compile('components/layout/AppShell.tsx').AppShell;
  function expand(value, key, live) {
    if (!value || typeof value !== 'object') return value;
    if (Array.isArray(value)) return value.map((child, i) => expand(child, key + '/' + i, live));
    if (typeof value.type === 'function') {
      const name = value.type.name, identity = key + '/' + name;
      live.add(identity); active = instances.get(identity) || { hooks: [] }; instances.set(identity, active); cursor = 0;
      executed.add(name);
      return expand(value.type(value.props), identity, live);
    }
    return { ...value, props: { ...value.props, children: expand(value.props.children, key + '/children', live),
      actions: expand(value.props.actions, key + '/actions', live), details: expand(value.props.details, key + '/details', live) } };
  }
  function render() {
    dirty = false; effects = []; const live = new Set();
    tree = expand(shellOnly ? jsx(Shell, { ventureId: 'venture', children: jsx('p', { children: 'Other venture page' }) }) : jsx(Page, {}), 'root', live);
    for (const [identity, instance] of instances) if (!live.has(identity)) { instance.hooks.forEach(hook => hook.cleanup?.()); instances.delete(identity); }
    effects.splice(0).forEach(effect => effect());
    return tree;
  }
  function nodes(value) {
    if (!value || typeof value !== 'object') return [];
    if (Array.isArray(value)) return value.flatMap(nodes);
    return [value, ...nodes(value.props?.children), ...nodes(value.props?.actions), ...nodes(value.props?.details)];
  }
  async function flush() { for (let i = 0; i < 8; i++) { if (dirty) render(); await new Promise(setImmediate); } return nodes(tree); }
  return { flush, nodes: () => nodes(tree), requests, navigations, events, executed, scope, render,
    finishRerun: (result = { ok: true, data: { job_id: 'job_new_research' } }) => finishRerun(result),
    holdRefresh() {
      holdRead = true;
      nodes(tree).find(node => node.type === 'button' && node.props.children === 'Refresh research').props.onClick();
    },
    finishRefresh: () => finishRead(),
    replayButtonEffects() {
      for (const [name, instance] of instances) if (name.endsWith('/NextStepButton'))
        for (const hook of instance.hooks) if (hook.setup) { hook.cleanup?.(); hook.cleanup = hook.setup(); }
    },
    prepareRerun() {
      return nodes(tree).find(node => node.props?.['data-usage-control'] === 'research_rerun').props.onClick;
    },
    startRerun() {
      return this.prepareRerun()();
    } };
}

const pairs = nodes => nodes.filter(node => node.props?.['aria-label'] === 'Landing page or customers');
const skipButtons = nodes => nodes.filter(node => node.props?.['data-usage-control'] === 'page_choice_skip');

const textContent = value => Array.isArray(value) ? value.map(textContent).join('')
  : value && typeof value === 'object' ? textContent(value.props?.children) : typeof value === 'string' ? value : '';

test('completed research never shows partial, stopped research does', async () => {
  for (const reportState of ['complete', 'partial']) {
    const nodes = await composition({ reportState }).flush();
    assert.ok(!nodes.some(node => /Deep research.*partial/.test(textContent(node))));
  }
  const stopped = await composition({ stoppedEarly: true }).flush();
  assert.ok(stopped.some(node => node.type === 'h2' && textContent(node) === 'Deep research · partial'));
});

test('research presents your opening instead of missing-evidence questions', async () => {
  const nextQuestions = Array.from({ length: 7 }, (_, index) => ({ kind: 'demand', text: `Talk to ${index + 5} robotics teams about field failures.`, action: 'customers' }));
  const nodes = await composition({ nextQuestions }).flush();
  assert.ok(!nodes.some(node => node.props?.id === 'research-gaps'));
  assert.ok(nodes.some(node => node.type === 'h3' && textContent(node) === 'Your opening'));
  assert.ok(!nodes.some(node => textContent(node).includes('Talk to 9')));
  assert.ok(!nodes.some(node => /YC directory|Customer evidence is unverified/.test(textContent(node))));
});

test('empty competitor fields are hidden rather than rendered as unknown', async () => {
  const nodes = await composition({ findings: { source: [{ id: 'lead', title: 'Simulation tool', url: 'https://simulation.example', summary: 'Robotics simulation.', category: 'source' }] },
    competitorChecks: [{ source_id: 'lead', product_quote: 'Robotics simulation.', customer_quote: null, positioning_quote: '', pricing_quote: '  ' }] }).flush();
  assert.ok(nodes.some(node => node.type === 'strong' && textContent(node) === 'Product'));
  for (const label of ['Target customer', 'Positioning', 'Public price'])
    assert.ok(!nodes.some(node => node.type === 'strong' && textContent(node) === label));
  assert.ok(!nodes.some(node => /Status unknown|Unknown from the saved sources|Unknown from this excerpt/.test(textContent(node))));
});

test('founder summary leads with sourced facts and your opening, hiding diagnostics', async () => {
  const found = { finding: 'PortfolioPro: Automates lending portfolio reporting.', url: 'https://portfolio.example', source_id: 'vendor' };
  const demand = { finding: 'I need a way to replace our manual portfolio reports.', url: 'https://reddit.com/r/lending/1', source_id: 'customer' };
  const nodes = await composition({ findings: { source: [
    { id: 'vendor', title: 'PortfolioPro', url: found.url, summary: 'Automates lending portfolio reporting.', category: 'source' },
    { id: 'customer', title: 'Portfolio manager', url: demand.url, summary: demand.finding, category: 'source' },
  ] }, summary: {
    evidence_dimensions: { competition: { status: 'supporting_leads', unknown: 'Source excerpt and relevance need verification; this is not proof of market demand.' },
      pricing: { status: 'unknown', unknown: 'Insufficient public pricing evidence; this does not imply customers will not pay.' } },
    founder_report: { findings: [found, demand], contradictions: [], assumptions: ['The buyer is VP Portfolio Mgmt.'],
      dimensions: { competition: { support: [found], challenge: [], empty_action: 'Compare alternatives.' },
        pricing: { support: [], challenge: [], empty_action: 'Ask customers what they pay today.' } } },
    verdict: { title: 'Not yet', verdict: 'not_yet', source_count: 2, why: ['Your idea review found: The buyer is VP Portfolio Mgmt.'],
      why_not: ['No strong public evidence yet on demand.'] },
  } }).flush();
  const text = nodes.map(textContent).join(' ');
  assert.match(text, /PortfolioPro: Automates lending portfolio reporting/);
  assert.match(text, /Your opening.*Your best opening:/);
  assert.ok(!text.includes('Your assumption:'));
  assert.ok(!/Nothing public found yet|Ask customers what they pay today|Insufficient public/.test(text));
  for (const diagnostic of ['Supporting leads', 'Still unknown', 'Insufficient public', 'not verified customer', 'Top risk', 'open question', 'areas have evidence', 'Your idea review found', 'No strong public evidence'])
    assert.ok(!text.includes(diagnostic), diagnostic);
  const list = nodes.find(node => node.props?.['aria-label'] === 'Findings');
  assert.equal(list.props.children.length, 1);
  assert.equal(nodes.filter(node => node.type === 'a' && !node.props.className && new URL(node.props.href, 'https://test.example').href === new URL(found.url).href && textContent(node) === new URL(found.url).hostname.replace(/^www\./, '')).length, 2);
});

test('competition fallback deduplicates saved company domains and limits to five', async () => {
  const findings = Array.from({ length: 7 }, (_, index) => ({ id: `vendor${index}`, title: `Vendor ${index}`, category: 'source',
    url: `https://vendor${index}.example/product`, summary: `Vendor ${index} automates portfolio reporting.` }));
  findings.splice(1, 0, { ...findings[0], id: 'duplicate', url: 'https://www.vendor0.example/plans' });
  const nodes = await composition({ findings: { source: findings }, summary: { evidence_dimensions: { competition: {
    status: 'supporting_leads', support: findings.map(finding => ({ url: finding.url, finding: 'Potential existing solution; fit and positioning need verification.' })),
  } } } }).flush();
  const list = nodes.find(node => node.props?.['aria-label'] === 'Findings');
  assert.equal(list.props.children.length, 5);
  assert.ok(!textContent(list).includes('Potential existing solution'));
  assert.equal(list.props.children.filter(item => textContent(item).includes('Vendor 0:')).length, 1);
});

test('prior attempt cards show known facts without unknown fields or empty sections', async () => {
  const quote = 'Automates lending portfolio reporting.';
  const nodes = await composition({ findings: { source: [{ id: 'vendor', title: 'PortfolioPro', url: 'https://portfolio.example', summary: quote, category: 'source' }] },
    summary: { prior_art: { companies: [{ key: 'portfolio.example', name: 'PortfolioPro', kind: 'live', status: 'unknown', finding_ids: ['vendor'],
      first_seen: 'Unknown', last_seen: null, what_they_built: { finding_id: 'vendor', quote },
      pricing_now: [{ finding_id: 'vendor', quote: 'Unknown from the saved sources' }], complaints: [] }], lessons: [], people: [], freshness: {} } } }).flush();
  const card = nodes.find(node => node.props?.['aria-labelledby'] === 'prior-art-title');
  assert.match(textContent(card), /Automates lending portfolio reporting/);
  assert.ok(!/Unknown|Public pricing|User complaint|Lessons to test|People to learn from|Newest source/.test(textContent(card)));
});

test('actual AppShell + Research + strips render one primary build and one secondary skip', async () => {
  const view = composition({ unknown: true }), nodes = await view.flush();
  for (const name of ['ResearchPage', 'AppShell', 'AppShellFrame', 'AppShellInner', 'NextStepStrip', 'PageChoices'])
    assert.ok(view.executed.has(name), name + ' must execute');
  assert.equal(pairs(nodes).length, 1);
  const controls = nodes.filter(node => ['page_choice_skip', 'page_choice_build'].includes(node.props?.['data-usage-control']));
  assert.equal(controls.length, 2);
  assert.equal(controls[0].props.className, 'primary');
  assert.equal(controls[1].props.className, 'ghost sm');
  assert.equal(nodes.filter(node => node.props?.['data-usage-control'] === 'research_customer_interviews').length, 1);
});

test('chat-first home shell sends the hidden Research and Now pages to Charlie\'s chat (classic keeps them)', async () => {
  for (const path of ['/v/venture/research', '/v/venture/now']) {
    const view = composition({ founderUI: 'home', shellOnly: path !== '/v/venture/research', path });
    const nodes = await view.flush();
    assert.deepEqual(view.navigations, [`/v/venture?chat=1&chat_route=${encodeURIComponent(path)}`], path);
    assert.equal(pairs(nodes).length, 0, path);
  }
});

test('starting the real Research rerun callback retires every pair before admission replies', async () => {
  const view = composition(); await view.flush(); const old = skipButtons(view.nodes());
  view.startRerun(); const pending = await view.flush();
  assert.equal(view.requests.filter(request => request.path.endsWith('/rerun')).length, 1);
  assert.ok(skipButtons(pending).every(node => node.props.disabled), 'no enabled pair during the request');
  old.forEach(node => node.props.onClick()); await view.flush();
  assert.equal(view.requests.filter(request => request.path.endsWith('/page-choice')).length, 0);
});

test('admitted research hides every pair despite the unchanged cached dashboard', async () => {
  const view = composition(); await view.flush();
  view.startRerun(); await view.flush(); view.finishRerun(); const nodes = await view.flush();
  assert.equal(pairs(nodes).length, 0, 'do not wait for the 15-second dashboard refresh');
  assert.ok(nodes.some(node => node.props?.title === 'Researching…'));
  assert.deepEqual(view.navigations, []);
});

test('the Research page renders durable progress for its active job', async () => {
  const progress = { state: 'leased', percent: 42, step_label: 'Searching for similar products', started_at: null };
  const view = composition({ noReport: true, recovery: { jobId: 'active_job' }, progress });
  const rendered = await view.flush();
  assert.equal(rendered.find(node => node.type === 'progress').props['aria-valuenow'], 42);
  assert.ok(view.executed.has('ResearchProgress'));
});

test('the actual shared strip keeps page choices on other venture routes', async () => {
  const view = composition({ shellOnly: true, path: '/v/venture/site' }), nodes = await view.flush();
  assert.ok(view.executed.has('NextStepButton'));
  assert.equal(pairs(nodes).length, 1);
});

for (const [key, title, href] of [
  ['site.building', 'Building your landing page…', '/v/venture/site'],
  ['research.running', 'Researching your market…', '/v/venture/research'],
]) {
  test(`the shared strip describes ${key} during queued work instead of offering choices`, async () => {
    const view = composition({ shellOnly: true, path: '/v/venture/now', runState: 'queued',
      step: { key, stage: 'Working', title, detail: 'Your saved work is safe.', href, cta_label: 'View progress', chat_action: null, payload: null } });
    const nodes = await view.flush();
    assert.equal(pairs(nodes).length, 0);
    assert.ok(nodes.some(node => node.type === 'strong' && node.props.children.includes(title)));
    assert.ok(nodes.some(node => node.props?.href === href && node.props['data-usage-control'] === 'next_step_action'));
  });
}

test('unconfirmed rerun keeps choices retired until a fresh saved-state read', async () => {
  for (const result of [{ ok: false, status: 0 }, { ok: true, data: {} }, { ok: true, data: { job_id: {} } }]) {
    const view = composition(); await view.flush(); view.startRerun(); await view.flush();
    view.finishRerun(result); const nodes = await view.flush();
    assert.equal(pairs(nodes).length, 0);
    nodes.find(node => node.type === 'button' && node.props.children === 'Refresh research').props.onClick();
    assert.equal(pairs(await view.flush()).length, 1);
  }
});

test('retired Research rerun controls cannot start a request in a new scope', async () => {
  for (const retire of ['owner', 'epoch', 'path']) {
    const view = composition(); await view.flush(); const oldStart = view.prepareRerun();
    if (retire === 'owner') view.scope.owner = 'another_owner';
    if (retire === 'epoch') view.scope.epoch += 2;
    if (retire === 'path') view.scope.path = '/v/another/research';
    oldStart(); await view.flush();
    assert.equal(view.requests.filter(request => request.path.endsWith('/rerun')).length, 0, retire);
  }
});

const forward = (key, href, action = null) => ({ key, href, chat_action: action, payload: { fixture: true },
  title: key === 'gtm.outreach' ? 'Find your first customers' : 'Review a local build',
  cta_label: key === 'gtm.outreach' ? 'Find customers' : 'Review local build', stage: 'Next step', detail: 'Your saved next step.' });

test('actual no-report body restores one scoped GTM or personal forward route', async () => {
  for (const options of [
    { recovery: { state: 'missing', revisionId: null }, step: forward('gtm.outreach', '/gtm?venture=venture') },
    { recovery: { personal: true }, step: forward('build.first', '/v/venture/idea') },
  ]) {
    const view = composition({ noReport: true, ...options }), nodes = await view.flush();
    const actions = nodes.filter(node => node.props?.['data-usage-control'] === 'next_step_action');
    assert.equal(actions.length, 1);
    assert.equal(actions[0].type, 'a'); assert.equal(actions[0].props.href, options.step.href);
    assert.ok(view.executed.has('NextStepButton'));
    assert.equal(pairs(nodes).length, 0);
  }
});

test('no-report fallback preserves the real server action and payload', async () => {
  const step = forward('gtm.outreach', '/gtm?venture=venture', 'outreach.draft');
  const view = composition({ noReport: true, step }), nodes = await view.flush();
  const action = nodes.find(node => node.props?.['data-usage-control'] === 'next_step_action');
  assert.equal(action?.type, 'button'); action.props.onClick(); await view.flush();
  const event = view.events.find(event => event.type === 'supymem:ask');
  assert.equal(event?.detail.action, 'outreach.draft');
  assert.equal(event?.detail.payload, step.payload);
  assert.deepEqual(view.navigations, []);
});

test('no-report fallback never invents a pair or bypasses unverified or active state', async () => {
  const step = forward('gtm.outreach', '/gtm?venture=venture');
  for (const options of [
    { step: undefined }, { step: null }, { recovery: { state: 'unknown' } }, { recovery: { state: 'clarifying' } },
    { recovery: { revisionId: null } }, { recovery: { error: 'Saved status unavailable.' } },
    { recovery: { jobId: 'active_job' } }, { readFailure: true }, { step: { ...step, key: 'work.running' } },
    { runState: 'working' }, { runState: 'queued' },
  ]) {
    const view = composition({ noReport: true, step, ...options }), nodes = await view.flush();
    assert.equal(pairs(nodes).length, 0);
    assert.equal(nodes.filter(node => node.props?.['data-usage-control'] === 'next_step_action').length, 0);
  }
});

test('no-report link and action callbacks retire on pathname or full-URL change before rerender', async () => {
  for (const action of [null, 'outreach.draft']) for (const path of ['/v/venture/idea', '/v/venture/research?view=older']) {
    const view = composition({ noReport: true, step: forward('gtm.outreach', '/gtm?venture=venture', action) });
    const nodes = await view.flush(), control = nodes.find(node => node.props?.['data-usage-control'] === 'next_step_action');
    view.scope.path = path;
    let prevented = false;
    control.props.onClick({ preventDefault() { prevented = true; } });
    if (action) assert.equal(view.events.filter(event => event.type === 'supymem:ask').length, 0, path);
    else assert.equal(prevented, true, path);
  }
});

test('no-report guidance retires while a fresh body read is in flight', async () => {
  const view = composition({ noReport: true, step: forward('gtm.outreach', '/gtm?venture=venture') });
  assert.equal((await view.flush()).filter(node => node.props?.['data-usage-control'] === 'next_step_action').length, 1);
  view.holdRefresh();
  assert.equal((await view.flush()).filter(node => node.props?.['data-usage-control'] === 'next_step_action').length, 0);
  view.finishRefresh();
  assert.equal((await view.flush()).filter(node => node.props?.['data-usage-control'] === 'next_step_action').length, 1);
});

test('the actual no-report button effect survives Strict Mode replay and still retires on unmount', async () => {
  const view = composition({ noReport: true, step: forward('gtm.outreach', '/gtm?venture=venture', 'outreach.draft') });
  const control = (await view.flush()).find(node => node.props?.['data-usage-control'] === 'next_step_action');
  view.replayButtonEffects();
  control.props.onClick();
  assert.equal(view.events.filter(event => event.type === 'supymem:ask').length, 1);
  view.holdRefresh(); await view.flush();
  control.props.onClick();
  assert.equal(view.events.filter(event => event.type === 'supymem:ask').length, 1);
});
