const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

function compile(path, require, context = {}) {
  const output = ts.transpileModule(readFileSync(path, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const exports = {};
  vm.runInNewContext(output, { exports, require, ...context }, { filename: path });
  return exports;
}
function harness(name = 'GodModePanel', options = {}) {
  let owner = 'account-a', epoch = 1, renderedVenture = 'venture-a', cursor = 0, effects = [], tree;
  const hooks = [], requests = [], events = [], downloads = [], navigations = [], timers = new Map(), listeners = new Map();
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const react = {
    createContext: value => ({ value }),
    useContext: context => context.value,
    useState(initial) {
      const i = cursor++;
      if (!hooks[i]) hooks[i] = { value: typeof initial === 'function' ? initial() : initial };
      return [hooks[i].value, next => { hooks[i].value = typeof next === 'function' ? next(hooks[i].value) : next; }];
    },
    useRef(initial) { const i = cursor++; return (hooks[i] ||= { current: initial }); },
    useCallback(fn, deps) {
      const i = cursor++;
      if (!hooks[i] || !same(hooks[i].deps, deps)) hooks[i] = { fn, deps };
      return hooks[i].fn;
    },
    useEffect(fn, deps) {
      const i = cursor++, before = hooks[i];
      if (!before || !same(before.deps, deps)) effects.push(() => {
        before?.cleanup?.(); hooks[i] = { deps, cleanup: fn() };
      });
    },
  };
  const jsx = (type, props, key) => typeof type === 'function' ? type(props) : { type, props: props || {}, key };
  const runtime = { jsx, jsxs: jsx, Fragment: 'fragment' };
  const window = {
    location: { hash: '' },
    setInterval(fn) { const key = timers.size + 1; timers.set(key, fn); return key; },
    clearInterval(key) { timers.delete(key); },
    setTimeout(fn) { const key = timers.size + 1; timers.set(key, fn); return key; },
    addEventListener(type, fn) { (listeners.get(type) || listeners.set(type, new Set()).get(type)).add(fn); },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    dispatchEvent(event) { events.push(event); for (const fn of [...(listeners.get(event.type) || [])]) fn(event); },
  };
  const scope = compile(join(__dirname, '../../lib/godModeRequestScope.ts'), () => {});
  const status = compile(join(__dirname, '../../lib/godModeStatus.ts'), () => {});
  const api = (path, init) => new Promise((resolve, reject) => requests.push({ path, init, resolve, reject }));
  function requireModule(id) {
    if (id === '@/components/chat/ChatLink') return { default: 'a', ChatAnchor: 'a', captureChatLink() {} };
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return runtime;
    if (id === '@/lib/client') return { api, apiSwr: (path, onData) => api(path).then(r => { onData(r); return r; }), verifiedSessionSubject: () => owner, verifiedSessionEpoch: () => epoch, ensureSession: async () => ({ ok: true }), prefetchApi: () => {} };
    if (id === '@/app/setup/localProviders') return compile(join(__dirname, '../../app/setup/localProviders.ts'), requireModule);
    if (id === '@/lib/chatStop') return require('../../lib/chatStop.ts');
    if (id === '@/lib/founderUI') return { FounderUIContext: { value: options.layout || 'classic' } };
    if (id === '@/lib/chatRouteAction') return compile(join(__dirname, '../../lib/chatRouteAction.ts'), name => require(join(__dirname, '../../lib', name)));
    if (id === '@/lib/apiCache') return { cacheGet: () => null, cachePut: () => {} };
    if (id === '@/lib/nextStep') return compile(join(__dirname, '../../lib/nextStep.ts'), () => {});
    if (id === 'next/navigation') return { usePathname: () => '/v/venture-a/plan', useParams: () => ({ ventureId: renderedVenture }), useRouter: () => ({ push(path) { navigations.push(path); } }) };
    if (id === '@/lib/companyJourney') return compile(join(__dirname, '../../lib/companyJourney.ts'), () => {});
    if (id === '@/lib/apiFailure') return { apiFailure: () => 'Synthetic unavailable state' };
    if (id === '@/lib/gtmDiagnosisApi') return { founderProductContext: () => 'synthetic context' };
    if (id === '@/lib/founderAssetsApi') return { safeSiteUrl: () => undefined };
    if (id === '@/components/layout/AppShell') return { AppShell: props => jsx('shell', { children: [props.title, props.children] }) };
    if (id === '@/components/layout/LocalModeBoundary') return { useLocalMode: () => false, useFeatureVisibility: () => () => true };
    if (id === '@/components/settings/GodModeRequestCard') return { GodModeRequestCard: () => ({ type: 'god-mode-request-card', props: {} }) };
    if (id === '@/components/ventures/BetaPathStrip') return { BetaPathStrip: () => ({ type: 'beta-path', props: {} }) };
    if (id === '@/components/ventures/FounderStageCard') return { FounderStageCard: () => null };
    if (id === '@/components/credits/CompanyCreditSummary') return { CompanyCreditSummary: () => ({ type: 'credit-summary', props: {} }) };
    if (id === '@/components/connections/GmailReconnectBanner') return { GmailReconnectBanner: () => null };
    if (id === '@/lib/betaPath') return { betaStageIndex: () => null, BETA_PATH: [] };
    if (id === '@/components/layout/NextStepStrip') return { ASK_EVENT: 'supymem:ask', useNextStep: () => null, NextStepButton: () => ({ type: 'next-step-button', props: {} }) };
    if (id === '@/components/primitives/OneCard') return { OneCard: props => ({ type: 'one-card', props }), OneCardStack: props => ({ type: 'one-card-stack', props }) };
    if (id === '@/components/ui/InlineSpinner') return { InlineSpinner: () => jsx('spinner', {}) };
    if (id === 'next/link') return { default: props => ({ type: 'a', props }) };
    if (id.endsWith('.module.css')) return { default: {} };
    if (id === '@/lib/godModeRequestScope') return scope;
    if (id === '@/lib/godModeStatus') return status;
    if (id === '@/lib/godModeOutcome') return compile(join(__dirname, '../../lib/godModeOutcome.ts'), () => {});
    if (id === '@/lib/godModeRequest') return compile(join(__dirname, '../../lib/godModeRequest.ts'), () => {});
    if (id === '@/lib/gtmJourneyApi') return { journeyClient: {
      get: owner => api(`/api/ventures/${owner.ventureId}/gtm/journey`).then(result => {
        if (!result.ok) throw new Error('Booking read failed'); return result.data;
      }),
      bookingLink: (owner, booking_link) => api(`/api/ventures/${owner.ventureId}/gtm/journey/booking-link`, { method: 'PUT', body: JSON.stringify({ booking_link }) }).then(result => result.data),
    } };
    if (id === '@/lib/serverReason') return { serverReason: (_, fallback) => fallback };
    if (id === '@/lib/format') return { shortTitle: name => name || 'Owned venture' };
    if (id === '@/lib/runStatusPresentation') return { RUN_STATUS_REFRESH: 'run-status-refresh' };
    if (id === '@/lib/ventures') return { renameVenture: (id, name) => api(`/api/ventures/${id}`, { method: 'PATCH', body: JSON.stringify({ name }) }) };
    if (name === 'SettingsPage' && id.startsWith('@/components/')) {
      const key = id.split('/').at(-1);
      return { [key]: props => ({ type: key, props }) };
    }
    if (id === '@/components/primitives/ErrorNotice') return { ErrorNotice: props => ({ type: 'notice', props }) };
    if (id === './GodModePanel') return { GOD_MODE_EVENT: 'supymem-god-mode' };
      if (id === '@/components/site/PageChoices') return {PageChoices: 'page-choices'};
      throw new Error(`unexpected import ${id}`);
  }
  const file = name === 'SettingsPage' ? join(__dirname, 'VentureSettings.tsx') : name === 'VenturePage' ? join(__dirname, '../../app/v/[ventureId]/page.tsx') : name === 'NextStepStrip' ? join(__dirname, '../layout/NextStepStrip.tsx') : join(__dirname, name + '.tsx');
  const Component = compile(file, requireModule, {
    window, URLSearchParams, setInterval: fn => window.setInterval(fn), clearInterval: key => window.clearInterval(key),
    document: { hidden: false, createElement: () => ({ click() { downloads.push({ clicked: true }); } }) },
    URL: { createObjectURL: () => 'blob:owned-fixture', revokeObjectURL() {} }, Blob: class { constructor(parts) { this.parts = parts; } },
    localStorage: { getItem: () => null },
    Event: class { constructor(type) { this.type = type; } },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } },
  })[name === "SettingsPage" ? "VentureSettings" : name === "VenturePage" ? "default" : name];
  return {
    requests, events, downloads, navigations,
    render(venture = 'venture-a') {
      cursor = 0; renderedVenture = venture; tree = Component({ ventureId: venture, ...options.props });
      const scheduled = effects; effects = []; scheduled.forEach(fn => fn());
      return tree;
    },
    get tree() { return tree; },
    listen(type, fn) { window.addEventListener(type, fn); },
    changeOwner(next) { owner = next; epoch++; window.dispatchEvent({ type: 'supymem-session-changed' }); },
    poll() { [...timers.values()].forEach(fn => fn()); },
    focus() { window.dispatchEvent({ type: 'focus' }); },
    select(section) { options.props = { embedded: true, section }; return this.render(renderedVenture); },
  };
}
function nodes(tree) {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return [tree, ...nodes(tree.props?.children)];
}
function text(tree) {
  if (typeof tree === 'string' || typeof tree === 'number') return String(tree);
  if (Array.isArray(tree)) return tree.map(text).join(' ');
  return text(tree?.props?.children ?? '');
}
function button(h, label) { return nodes(h.tree).find(n => n.type === 'button' && text(n) === label); }
function input(h, id) { return nodes(h.tree).find(n => ['input', 'select'].includes(n.type) && n.props.id === id); }
const reply = (on = true, sendMax = 0) => ({ ok: true, status: 200, data: {
  god_mode: on, daily_cap_credits: null, lifetime_cap_credits: null, spent_today_credits: 0, allow_send: sendMax > 0,
  allow_mac: false, send_max_per_day: sendMax, floors: [], warning: 'beta', timeline: [], pmf_journal: [],
  status: { state: 'idle', working_on: '', waiting_on_you: [] },
} });
const settle = () => new Promise(resolve => setImmediate(resolve));

test('settings failed initial GET exposes recovery and confirmed state enables controls', async () => {
  const h = harness(); h.render(); h.requests[0].resolve({ ok: false, status: 503 }); await settle(); h.render();
  assert.ok(button(h, 'Refresh saved status'));
  assert.equal(button(h, 'Turn on God mode'), undefined);
  button(h, 'Refresh saved status').props.onClick(); h.requests[1].resolve(reply(false, 123)); await settle(); h.render();
  assert.equal(input(h, 'god-cap'), undefined);
  assert.equal(button(h, 'Save limit'), undefined);
  assert.equal(input(h, 'god-send').props.value, '123');
  assert.equal(button(h, 'Turn on God mode').props.disabled, false);
});

test('a late prior venture response cannot populate next venture settings', async () => {
  const h = harness(); h.render('venture-a'); h.render('venture-b');
  h.requests[0].resolve(reply(true, 901)); await settle(); h.render('venture-b');
  assert.equal(input(h, 'god-cap'), undefined);
  h.requests[1].resolve(reply(false, 202)); await settle(); h.render('venture-b');
  assert.equal(input(h, 'god-send').props.value, '202');
  assert.equal(button(h, 'Turn off God mode'), undefined);
});

test('same-route account change retires cached settings and old-account response', async () => {
  const h = harness(); h.render(); h.changeOwner('account-b'); h.render();
  h.requests[0].resolve(reply(true, 901)); await settle(); h.render();
  assert.equal(input(h, 'god-cap'), undefined);
  h.requests[1].resolve(reply(false, 202)); await settle(); h.render();
  assert.equal(input(h, 'god-send').props.value, '202');
});

test('polling does not overwrite unsaved founder sending limits', async () => {
  const h = harness(); h.render(); h.requests[0].resolve(reply()); await settle(); h.render();
  input(h, 'god-send').props.onChange({ target: { value: '999' } }); h.render();
  h.poll(); h.requests[1].resolve(reply(true, 33)); await settle(); h.render();
  assert.equal(input(h, 'god-send').props.value, '999');
});

test('late poll cannot undo a confirmed newer off setting or claim in-flight work was stopped', async () => {
  const h = harness(); h.render(); h.requests[0].resolve(reply()); await settle(); h.render();
  h.poll(); button(h, 'Turn off God mode').props.onClick();
  h.requests[2].resolve(reply(false)); await settle();
  h.requests[1].resolve(reply(true)); await settle(); h.render();
  assert.ok(button(h, 'Turn on God mode'));
  assert.doesNotMatch(text(h.tree), /stopped working on its own/);
  assert.match(text(h.tree), /existing work may still finish/);
});

test('lost mutation acknowledgement requires read recovery and never auto-replays PUT', async () => {
  const h = harness(); h.render(); h.requests[0].resolve(reply()); await settle(); h.render();
  button(h, 'Turn off God mode').props.onClick(); h.requests[1].reject(new Error('synthetic transport failure'));
  await settle(); h.render();
  assert.ok(button(h, 'Refresh saved status'));
  assert.equal(button(h, 'Turn off God mode').props.disabled, true);
  assert.equal(h.requests.filter(r => r.init?.method === 'PUT').length, 1);
  button(h, 'Refresh saved status').props.onClick(); h.requests[2].resolve(reply(false)); await settle(); h.render();
  assert.ok(button(h, 'Turn on God mode'));
});

test('live offer suppresses duplicate mutations and polling while its PUT is pending', async () => {
  const h = harness('GodModeLive'); h.render(); h.requests[0].resolve(reply(false)); await settle(); h.render();
  button(h, 'Turn on God mode').props.onClick(); h.render();
  const confirm = button(h, 'I understand, turn it on'); confirm.props.onClick(); confirm.props.onClick(); h.poll();
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve(reply(true)); await settle(); h.render();
  assert.match(text(h.tree), /No work is in progress/);
});

test('live old-venture mutation cannot repopulate the new venture offer', async () => {
  const h = harness('GodModeLive'); h.render(); h.requests[0].resolve(reply(false)); await settle(); h.render();
  button(h, 'Turn on God mode').props.onClick(); h.render(); button(h, 'I understand, turn it on').props.onClick();
  h.render('venture-b'); h.requests[1].resolve(reply(true)); await settle(); h.render('venture-b');
  assert.doesNotMatch(text(h.tree), /No work is in progress/);
  h.requests[2].resolve(reply(false)); await settle(); h.render('venture-b');
  assert.ok(button(h, 'Turn on God mode'));
});


test('an old rendered settings handler cannot write after account changes before rerender', async () => {
  const h = harness(); h.render(); h.requests[0].resolve(reply()); await settle(); h.render();
  const oldHandler = button(h, 'Turn off God mode').props.onClick;
  h.changeOwner('account-b');
  oldHandler();
  assert.equal(h.requests.filter(r => r.init?.method === 'PUT').length, 0);
});

test('an old rendered live confirmation cannot write after venture change', async () => {
  const h = harness('GodModeLive'); h.render(); h.requests[0].resolve(reply(false)); await settle(); h.render();
  button(h, 'Turn on God mode').props.onClick(); h.render();
  const oldHandler = button(h, 'I understand, turn it on').props.onClick;
  h.render('venture-b'); oldHandler();
  assert.equal(h.requests.filter(r => r.init?.method === 'PUT').length, 0);
});


test('God mode capability copy describes requested eligible work without billing details', async () => {
  for (const name of ['GodModePanel', 'GodModeLive']) {
    const h = harness(name); h.render(); h.requests[0].resolve(reply(false)); await settle(); h.render();
    const rendered = text(h.tree);
    assert.match(rendered, /eligible/);
    assert.match(rendered, /Your requested ongoing work/);
    assert.doesNotMatch(rendered, /credits|spending cap/);
    assert.doesNotMatch(rendered, /Daily spending limit|Save limit/);
    assert.doesNotMatch(rendered, /around the clock|without asking you first|from your idea to your first users/);
  }
});


const nextReply = title => ({ ok: true, status: 200, data: { venture: { id: 'venture-a' }, next_step: {
  key: 'plan', title, stage: 'Plan', detail: '', cta_label: 'Approve saved plan', href: '/v/venture-a/plan',
  chat_action: 'plan.approve', payload: { plan_id: 'plan_owned' },
} } });

test('next-step callbacks cannot restore an old account step and cached step retires immediately', async () => {
  const h = harness('NextStepStrip'); h.render(); h.changeOwner('account-b');
  h.requests[0].resolve(nextReply('Old account private plan')); await settle(); h.render();
  assert.doesNotMatch(text(h.tree), /Old account/);
  h.requests[1].resolve(nextReply('Current actor plan')); await settle(); h.render();
  assert.match(text(h.tree), /Current actor/);
  h.changeOwner('account-c'); h.render();
  assert.doesNotMatch(text(h.tree), /Current actor/);
});

test('an old next-step handler cannot dispatch after account transition before rerender', async () => {
  const h = harness('NextStepStrip'); h.render(); h.requests[0].resolve(nextReply('Owned actor plan')); await settle(); h.render();
  const old = button(h, 'Approve saved plan').props.onClick;
  h.changeOwner('account-b'); old();
  assert.equal(h.events.filter(event => event.type === 'supymem:ask').length, 0);
});


test('cockpit cannot display or cache old-account company after same-route account change', async () => {
  const h = harness('VenturePage'); h.render(); await settle();
  const previous = [...h.requests]; assert.equal(previous.length, 5);
  h.changeOwner('account-b'); await settle(); h.render();
  function resolveRead(request, name) {
    request.resolve({ ok: true, status: 200, data: request.path === '/api/ventures/venture-a'
      ? { workspace_id: 'wsp_owned', name, raw_idea: 'synthetic', stage: 'idea' }
      : request.path.endsWith('/dashboard') ? { tiles: {} } : { items: [] } });
  }
  previous.forEach(request => resolveRead(request, 'Old account private company'));
  await settle(); h.render();
  assert.doesNotMatch(text(h.tree), /Old account/);
  h.requests.slice(5).forEach(request => resolveRead(request, 'Current actor company'));
  await settle();
  h.requests.filter(request => request.path.includes('/jobs?')).forEach(request => resolveRead(request, 'Current actor company'));
  await settle(); h.render();
  assert.match(text(h.tree), /Current actor company/);
});

test('activity labels distinguish queued, prepared, verified and historical unverified effects', async () => {
  for (const name of ['GodModePanel', 'GodModeLive']) {
    const h = harness(name); h.render();
    const r = reply(true); r.data.timeline = ['attempt', 'prepared', 'queued', 'completed', 'held', 'done'].map((outcome, i) => ({
      id: 'entry_' + i, what: 'Synthetic saved activity', why: '', credits: 0, outcome,
      completion_verified: outcome === 'completed', display_outcome: outcome === 'done' ? 'recorded' : outcome, at: '2026-10-03T00:00:00Z',
    }));
    h.requests[0].resolve(r); await settle(); h.render();
    if (name === 'GodModeLive') { button(h, 'What it did').props.onClick(); h.render(); }
    const rendered = text(h.tree);
    for (const label of ['budget reservation, not provider charge', 'Prepared for review', 'Work queued', 'Verified effect saved', 'Review needed', 'Recorded activity (completion unverified)']) assert.ok(rendered.includes(label), label);
    assert.doesNotMatch(rendered, /· Done/);
  }
});

test('God mode settings old handler cannot write across account A to B to A before rerender', async () => {
  const h = harness('GodModePanel'); h.render(); h.requests[0].resolve(reply(true)); await settle(); h.render();
  const handler = button(h, 'Turn off God mode').props.onClick;
  h.changeOwner('account-b'); h.changeOwner('account-a'); handler();
  assert.equal(h.requests.filter(r => r.init?.method === 'PUT').length, 0);
});
test('God mode live old confirmation cannot write across account A to B to A before rerender', async () => {
  const h = harness('GodModeLive'); h.render(); h.requests[0].resolve(reply(false)); await settle(); h.render();
  button(h, 'Turn on God mode').props.onClick(); h.render();
  const handler = button(h, 'I understand, turn it on').props.onClick;
  h.changeOwner('account-b'); h.changeOwner('account-a'); handler();
  assert.equal(h.requests.filter(r => r.init?.method === 'PUT').length, 0);
});

test('legacy unqualified PMF measurements are not displayed as venture progress', async () => {
  const h = harness('GodModeLive'); h.render();
  const r = reply(true);
  r.data.pmf_journal = [{n: 1, variable: 'icp', change: 'Historical fixture', metric: 'outbound_replies', baseline: 99, measured: 100, target: 101, state: 'needs_review', decision: null, metric_qualified: false}];
  h.requests[0].resolve(r); await settle(); h.render(); button(h, 'What it did').props.onClick(); h.render();
  const rendered = text(h.tree);
  assert.ok(rendered.includes('Experiment journal'));
  assert.ok(rendered.includes('Historical measurement unqualified'));
  assert.doesNotMatch(rendered, /99 to 100/);
});


test('God mode strip reads as a cofounder: now, next, one thing waiting on you, what it learned', async () => {
  const h = harness('GodModeLive'); h.render();
  const r = reply(true);
  r.data.brief = {
    now: 'Working on the parts that do not need you.', now_detail: 'Blocked on: Review your first build.',
    next: ['Draft a batch of outreach'], more_waiting: 1, learned: ['Test #1 (positioning): worked, keeping it. 0 to 5 waitlist signups (needed 5).'],
    waiting_on_you: [{ title: 'Review your first build', why: 'Only you can accept a build.', how: 'Open it' }],
    recent: [{ at: '2026-10-03T00:00:00Z', kind: 'did', text: 'Published your landing page' }],
  };
  h.requests[0].resolve(r); await settle(); h.render();
  let rendered = text(h.tree);
  assert.ok(rendered.includes('Working on the parts that do not need you.'));
  assert.ok(rendered.includes('Next:') && rendered.includes('Draft a batch of outreach'));
  assert.ok(rendered.includes('Waiting on you (1 thing):') && rendered.includes('Review your first build') && /1\s+more after this/.test(rendered));
  assert.ok(rendered.includes('What I learned') && rendered.includes('worked, keeping it'));
  assert.doesNotMatch(rendered, /Attempting|Guarded attempt|budget reservation/);
  button(h, 'What it did').props.onClick(); h.render();
  rendered = text(h.tree);
  assert.ok(rendered.includes('What I did lately') && rendered.includes('Published your landing page'));
});

test('God mode strip says plainly when nothing needs the founder', async () => {
  const h = harness('GodModeLive'); h.render();
  const r = reply(true);
  r.data.brief = { now: 'Research is running', now_detail: '', next: [], more_waiting: 0, learned: [], waiting_on_you: [], recent: [] };
  h.requests[0].resolve(r); await settle(); h.render();
  assert.ok(text(h.tree).includes('Nothing needs you right now.'));
});

const llmFixture = (model = 'owned-model') => ({ provider: 'openai', model, models: [model], source: 'founder_override', base_url: null, api_key_set: false, usage: { total_tokens: 7 } });
async function completeSettings(h, start, { venture = 'venture-a', paused = false, workspace = '', name = 'Owned venture' } = {}) {
  assert.equal(h.requests[start].path, '/api/ventures/delete-phrase');
  h.requests[start].resolve({ ok: true, status: 200, data: { phrase: 'DELETE' } }); await settle();
  assert.equal(h.requests[start + 1].path, '/api/ventures/' + venture);
  h.requests[start + 1].resolve({ ok: true, status: 200, data: { name, paused, autonomy_level: 'L1', workspace_id: workspace } }); await settle();
  if (workspace) {
    assert.equal(h.requests[start + 2].path, `/api/workspaces/${workspace}/llm-settings`);
    h.requests[start + 2].resolve({ ok: true, status: 200, data: llmFixture() }); await settle();
    h.requests[start + 3].resolve({ ok: true, status: 200, data: { providers: { openai: ['owned-model'] } } }); await settle();
  }
  h.render(venture); return h;
}
async function loadedSettings(paused = false, workspace = '') {
  const h = harness('SettingsPage'); h.render(); await settle();
  return completeSettings(h, 0, { paused, workspace });
}

test('actual Settings pause callback keeps its scope and reports new-work admission without promising interruption', async () => {
  const h = await loadedSettings();
  assert.match(text(h.tree), /Blocks new work for this venture/);
  assert.match(text(h.tree), /Work already running may finish/);
  assert.match(text(h.tree), /unconfirmed usage remains unresolved/);
  button(h, 'Pause venture').props.onClick(); h.render();
  const write = h.requests[2];
  assert.equal(write.path, '/api/ventures/venture-a/pause');
  assert.equal(write.init.method, 'POST');
  assert.deepEqual(JSON.parse(write.init.body), { reason: 'Stopped from Settings' });
  assert.equal(button(h, 'Pause venture').props.disabled, true);
  write.resolve({ ok: true, status: 200, data: { paused: true } }); await settle(); h.render();
  assert.ok(button(h, 'Resume'));
  assert.match(text(h.tree), /This venture is paused: new work is blocked/);
  assert.match(text(h.tree), /review its results and credit settlement/);
  assert.doesNotMatch(text(h.tree), /stops everything|all stopped|spend right away|stops.*immediately/i);
  assert.equal(h.events.filter(e => e.type === 'run-status-refresh').length, 1);
});

test('a refused Settings pause does not report the venture paused or running work cancelled', async () => {
  const h = await loadedSettings(); button(h, 'Pause venture').props.onClick();
  h.requests[2].resolve({ ok: false, status: 503 }); await settle(); h.render();
  assert.ok(button(h, 'Pause venture'));
  assert.equal(button(h, 'Resume'), undefined);
  assert.match(text(h.tree), /Couldn’t pause this venture/);
  assert.doesNotMatch(text(h.tree), /This venture is paused|running work cancelled/i);
  assert.equal(h.events.filter(e => e.type === 'run-status-refresh').length, 0);
});

test('confirmed God mode off keeps founder limits and renders the qualified running-work floor', async () => {
  const h = harness(); h.render();
  const r = reply(true, 200);
  r.data.floors = ['Pausing a venture blocks new work for that venture. Turning God mode off blocks new autonomous steps. Work already running may finish; review its recorded result and credit settlement. Unconfirmed usage remains unresolved.'];
  h.requests[0].resolve(r); await settle(); h.render();
  assert.match(text(h.tree), /Unconfirmed usage remains unresolved/);
  button(h, 'Turn off God mode').props.onClick();
  assert.deepEqual(JSON.parse(h.requests[1].init.body), { god_mode: false });
  const off = reply(false, 200); off.data.floors = r.data.floors;
  h.requests[1].resolve(off); await settle(); h.render();
  assert.equal(input(h, 'god-cap'), undefined);
  assert.equal(input(h, 'god-send').props.value, '200');
  assert.match(text(h.tree), /existing work may still finish/);
  assert.match(text(h.tree), /Unconfirmed usage remains unresolved/);
  assert.doesNotMatch(text(h.tree), /stops everything immediately|work cancelled|credits refunded/i);
});

function field(h, label) { return nodes(h.tree).find(n => n.type === 'input' && n.props['aria-label'] === label); }
function control(h, id) { return nodes(h.tree).find(n => n.props?.['data-usage-control'] === id); }
function formFor(h, id) { return nodes(h.tree).find(n => n.type === 'form' && nodes(n).some(child => child.props?.['data-usage-control'] === id)); }

test('delayed Settings reads cannot display a prior venture, subject or same-subject ABA generation', async () => {
  for (const transition of ['venture', 'subject', 'aba']) {
    const h = harness('SettingsPage'); h.render(); await settle();
    h.requests[0].resolve({ ok: true, status: 200, data: { phrase: 'OLD PHRASE' } }); await settle();
    const before = h.requests[1];
    if (transition === 'venture') h.render('venture-b');
    else { h.changeOwner('account-b'); if (transition === 'aba') h.changeOwner('account-a'); }
    before.resolve({ ok: true, status: 200, data: { name: 'Prior private fixture', manager_name: 'Old manager', workspace_id: 'workspace-old', paused: true } });
    await settle(); h.render(transition === 'venture' ? 'venture-b' : 'venture-a');
    assert.doesNotMatch(text(h.tree), /Prior private fixture|OLD PHRASE|Old manager/);
    assert.equal(h.requests.some(r => r.path.includes('workspace-old')), false);
    assert.equal(button(h, 'Pause venture'), undefined);
  }
});

test('retained Settings effect callbacks refuse before requests after account, route and ABA retirement', async () => {
  for (const transition of ['subject', 'venture', 'aba']) {
    const h = await loadedSettings(false, 'workspace-a');
    field(h, 'Type DELETE to confirm').props.onChange({ target: { value: 'DELETE' } }); h.render();
    const callbacks = [
      () => button(h, 'Pause venture').props.onClick,
      () => formFor(h, 'venture_rename').props.onSubmit,
      () => formFor(h, 'settings_manager_rename').props.onSubmit,
      () => button(h, 'Download my venture').props.onClick,
      () => button(h, "I'm stuck, download report").props.onClick,
      () => control(h, 'venture_delete').props.onClick,
      () => control(h, 'llm_settings_save').props.onClick,
      () => control(h, 'llm_settings_test').props.onClick,
      () => control(h, 'llm_settings_use_ours').props.onClick,
    ].map(get => get());
    const before = h.requests.length;
    if (transition === 'venture') h.render('venture-b');
    else { h.changeOwner('account-b'); if (transition === 'aba') h.changeOwner('account-a'); }
    for (const callback of callbacks) callback({ target: { value: 'L4' }, preventDefault() {} });
    await settle();
    assert.equal(h.requests.slice(before).some(r => r.init?.method), false, transition);
    assert.equal(h.downloads.length, 0); assert.equal(h.navigations.length, 0);
  }
});

test('old inline Settings editors cannot poison the newly loaded owner fields or typed deletion confirmation', async () => {
  const h = await loadedSettings(false, 'workspace-a');
  const previousKey = nodes(h.tree).find(n => n.props?.className === 'chat-inner').key;
  const editors = nodes(h.tree).filter(n => ['input', 'select'].includes(n.type) && n.props.onChange).map(n => n.props.onChange);
  const before = h.requests.length; h.changeOwner('account-b'); await settle();
  h.render(); assert.equal(input(h, 'llm-api-key'), undefined);
  await completeSettings(h, before, { workspace: 'workspace-b', name: 'Current owner fixture' });
  for (const edit of editors) edit({ target: { value: 'OLD PRIVATE FIXTURE' } }); h.render();
  assert.equal(field(h, 'Venture name').props.value, 'Current owner fixture');
  assert.equal(field(h, 'Manager name').props.value, '');
  assert.equal(input(h, 'llm-api-key').props.value, '');
  assert.equal(field(h, 'Type DELETE to confirm').props.value, '');
  assert.equal(input(h, 'llm-model').props.value, 'owned-model');
  assert.notEqual(nodes(h.tree).find(n => n.props?.className === 'chat-inner').key, previousKey);
});

test('retired Settings export response never creates or clicks a private download', async () => {
  const h = await loadedSettings(); button(h, 'Download my venture').props.onClick();
  const read = h.requests[2]; assert.equal(read.path, '/api/ventures/venture-a/export');
  assert.equal(read.init?.method, 'POST');
  h.changeOwner('account-b');
  read.resolve({ ok: true, status: 200, data: { synthetic_private_fixture: true } }); await settle(); h.render();
  assert.equal(h.downloads.length, 0);
  assert.doesNotMatch(text(h.tree), /Owned venture/);
});

test('retired delete receipt cannot navigate and old request cannot clear a newer pause in-flight state', async () => {
  const del = await loadedSettings(); field(del, 'Type DELETE to confirm').props.onChange({ target: { value: 'DELETE' } }); del.render();
  control(del, 'venture_delete').props.onClick(); const oldDelete = del.requests[2]; del.changeOwner('account-b');
  oldDelete.resolve({ ok: true, status: 200, data: {} }); await settle(); del.render();
  assert.equal(del.navigations.length, 0);
  const h = await loadedSettings(); button(h, 'Pause venture').props.onClick(); const oldPause = h.requests[2];
  const start = h.requests.length; h.changeOwner('account-b'); await settle(); await completeSettings(h, start);
  button(h, 'Pause venture').props.onClick(); h.render(); const nextPause = h.requests.at(-1);
  oldPause.resolve({ ok: true, status: 200, data: { paused: true } }); await settle(); h.render();
  assert.equal(button(h, 'Pause venture').props.disabled, true);
  assert.equal(h.events.filter(e => e.type === 'run-status-refresh').length, 0);
  nextPause.resolve({ ok: true, status: 200, data: { paused: true } }); await settle(); h.render();
  assert.ok(button(h, 'Resume'));
});

test('initial Settings read failure offers recovery and no mutating or export controls', async () => {
  const h = harness('SettingsPage'); h.render(); await settle();
  h.requests[0].resolve({ ok: true, status: 200, data: { phrase: 'DELETE' } }); await settle();
  h.requests[1].resolve({ ok: false, status: 503 }); await settle(); h.render();
  assert.ok(button(h, 'Refresh saved settings'));
  assert.equal(button(h, 'Pause venture'), undefined);
  assert.equal(button(h, 'Download my venture'), undefined);
});

test('current Settings exports and typed deletion retain their exact effects with in-flight dedupe', async () => {
  const h = await loadedSettings(); const exportClick = button(h, 'Download my venture').props.onClick;
  exportClick(); exportClick(); assert.equal(h.requests.filter(r => r.path.endsWith('/export')).length, 1);
  assert.equal(h.requests[2].init?.method, 'POST');
  h.requests[2].resolve({ ok: true, status: 200, data: { owned_fixture: true } }); await settle(); h.render();
  assert.equal(h.downloads.length, 1);
  field(h, 'Type DELETE to confirm').props.onChange({ target: { value: 'DELETE' } }); h.render();
  control(h, 'venture_delete').props.onClick(); const write = h.requests[3];
  assert.equal(write.path, '/api/ventures/venture-a/delete'); assert.deepEqual(JSON.parse(write.init.body), { confirmation: 'DELETE' });
  write.resolve({ ok: true, status: 200, data: {} }); await settle(); h.render();
  assert.deepEqual(h.navigations, ['/']);
});

test('current LLM save invalidates older focus refresh and remains exact to the loaded workspace', async () => {
  const h = await loadedSettings(false, 'workspace-a'); h.focus(); const oldRead = h.requests.at(-1);
  input(h, 'llm-api-key').props.onChange({ target: { value: 'owned-test-placeholder' } }); h.render();
  control(h, 'llm_settings_save').props.onClick(); const write = h.requests.at(-1);
  assert.equal(write.path, '/api/workspaces/workspace-a/llm-settings'); assert.equal(write.init.method, 'PUT');
  assert.equal(JSON.parse(write.init.body).api_key, 'owned-test-placeholder');
  write.resolve({ ok: true, status: 200, data: llmFixture('saved-model') }); await settle();
  oldRead.resolve({ ok: true, status: 200, data: llmFixture('old-model') }); await settle(); h.render();
  assert.equal(input(h, 'llm-model').props.value, 'saved-model'); assert.equal(input(h, 'llm-api-key').props.value, '');
});

test('Settings rename rechecks owner after its awaited import before dispatch', async () => {
  const h = await loadedSettings(); formFor(h, 'venture_rename').props.onSubmit({ preventDefault() {} });
  h.changeOwner('account-b'); await settle(); h.render();
  assert.equal(h.requests.some(r => r.init?.method === 'PATCH'), false);
  assert.doesNotMatch(text(h.tree), /Name saved/);
});

test('Settings focus reads stop on retirement and stale LLM response cannot restore private controls', async () => {
  const h = await loadedSettings(false, 'workspace-a'); h.focus(); const old = h.requests.at(-1);
  const before = h.requests.length; h.changeOwner('account-b'); h.focus(); h.poll(); await settle(); h.render();
  assert.equal(h.requests.slice(before).some(r => r.path.includes('workspace-a')), false);
  old.resolve({ ok: true, status: 200, data: { ...llmFixture('old-private-model'), badge: 'Old private fixture badge' } });
  await settle(); h.render();
  assert.equal(input(h, 'llm-api-key'), undefined); assert.doesNotMatch(text(h.tree), /Old private fixture badge/);
});

test('current manager, name and provider reset callbacks preserve their exact source routes', async () => {
  const h = await loadedSettings(false, 'workspace-a');
  field(h, 'Manager name').props.onChange({ target: { value: 'Fixture manager' } }); h.render();
  formFor(h, 'settings_manager_rename').props.onSubmit({ preventDefault() {} }); const manager = h.requests.at(-1);
  assert.equal(manager.path, '/api/ventures/venture-a/settings'); assert.equal(manager.init.method, 'PATCH');
  manager.resolve({ ok: true, status: 200, data: { manager_name: 'Fixture manager' } }); await settle(); h.render();
  assert.equal(field(h, 'Manager name').props.value, 'Fixture manager');
  field(h, 'Venture name').props.onChange({ target: { value: 'Renamed fixture' } }); h.render();
  formFor(h, 'venture_rename').props.onSubmit({ preventDefault() {} }); await settle(); const rename = h.requests.at(-1);
  assert.equal(rename.path, '/api/ventures/venture-a'); assert.deepEqual(JSON.parse(rename.init.body), { name: 'Renamed fixture' });
  rename.resolve({ ok: true, status: 200, data: { name: 'Renamed fixture' } }); await settle(); h.render();
  assert.match(text(h.tree), /Renamed fixture/);
  control(h, 'llm_settings_use_ours').props.onClick(); const reset = h.requests.at(-1);
  assert.equal(reset.path, '/api/workspaces/workspace-a/llm-settings'); assert.equal(reset.init.method, 'DELETE');
  reset.resolve({ ok: true, status: 200, data: { ...llmFixture(), source: 'platform_default' } }); await settle(); h.render();
  assert.equal(button(h, 'Use my own key instead'), undefined); // beta: BYO key is hidden after the reset
});

test('retained Settings recovery handler cannot reset a later owner after initial read failure', async () => {
  const h = harness('SettingsPage'); h.render(); await settle();
  h.requests[0].resolve({ ok: true, status: 200, data: { phrase: 'DELETE' } }); await settle();
  h.requests[1].resolve({ ok: false, status: 503 }); await settle(); h.render();
  const oldRecovery = button(h, 'Refresh saved settings').props.onClick;
  const start = h.requests.length; h.changeOwner('account-b'); await settle(); await completeSettings(h, start, { name: 'Current loaded fixture' });
  const before = h.requests.length; oldRecovery(); await settle(); h.render();
  assert.equal(h.requests.length, before); assert.match(text(h.tree), /Current loaded fixture/);
});

test('in-flight LLM poll cannot pair an unsaved key with another provider and failed save preserves the draft', async () => {
  const h = await loadedSettings(false, 'workspace-a'); h.focus(); const oldRead = h.requests.at(-1);
  input(h, 'llm-api-key').props.onChange({ target: { value: 'owned-openai-placeholder' } }); h.render();
  oldRead.resolve({ ok: true, status: 200, data: { ...llmFixture('polled-model'), provider: 'deepseek_direct' } });
  await settle(); h.render();
  assert.equal(input(h, 'llm-provider').props.value, 'openai');
  assert.equal(input(h, 'llm-api-key').props.value, 'owned-openai-placeholder');
  const before = h.requests.length; h.focus(); h.poll(); assert.equal(h.requests.length, before);
  control(h, 'llm_settings_save').props.onClick(); const save = h.requests.at(-1);
  assert.equal(JSON.parse(save.init.body).provider, 'openai');
  save.resolve({ ok: false, status: 503 }); await settle(); h.render();
  assert.equal(input(h, 'llm-api-key').props.value, 'owned-openai-placeholder');
  h.focus(); assert.equal(h.requests.length, before + 1);
  control(h, 'llm_settings_save').props.onClick(); h.requests.at(-1).resolve({ ok: true, status: 200, data: llmFixture('confirmed-model') });
  await settle(); h.render(); const next = h.requests.length; h.focus(); assert.equal(h.requests.length, next + 1);
});

test('settings offers Keep working while my Mac is off and shows the offline status', async () => {
  const h = harness(); h.render();
  const r = reply(); r.data.status = { state: 'mac_offline', working_on: "Paused — your Mac is offline. Turn on 'Keep working while my Mac is off' to continue." };
  h.requests[0].resolve(r); await settle(); h.render();
  assert.match(text(h.tree), /Paused — your Mac is offline/);
  const box = input(h, 'god-offline');
  box.props.onChange({ target: { checked: true } });
  const put = h.requests.find(q => q.init?.method === 'PUT');
  assert.deepEqual(JSON.parse(put.init.body), { continue_when_offline: true });
});


test('the clicked support bundle remains a read-only GET download', async () => {
  const h = await loadedSettings(); button(h, "I'm stuck, download report").props.onClick();
  const read = h.requests[2]; assert.equal(read.path, '/api/ventures/venture-a/support-bundle');
  assert.equal(read.init?.method, undefined);
  read.resolve({ ok: true, status: 200, data: { owned_support_fixture: true } }); await settle(); h.render();
  assert.equal(h.downloads.length, 1);
});

test('home settings show only the selected PR section with owned data and retained production features', async () => {
  const h = harness('SettingsPage', { props: { embedded: true, section: 'venture' } });
  h.render(); await settle(); await completeSettings(h, 0, { workspace: 'workspace-a', name: 'Real company' });
  const group = () => nodes(h.tree).filter(node => node.type === 'section' && node.props.className === 'settings-group');
  assert.equal(group().length, 1); assert.equal(group()[0].props.id, 'settings-venture');
  assert.equal(field(h, 'Venture name').props.value, 'Real company');
  assert.equal(nodes(h.tree).filter(node => node.type === 'button' && text(node) === 'Save').length, 2);
  const links = nodes(h.tree).filter(node => node.props.className === 'settings-link');
  assert.deepEqual(links.map(node => node.props.href), ['/v/venture-a/site', '/v/venture-a/tasks', '/connections', '/v/venture-a/now']);
  assert.ok(nodes(h.tree).some(node => node.type === 'BookingLinkCard' && node.props.workspaceId === 'workspace-a'));
  assert.equal(control(h, 'settings_emergency_stop'), undefined);
  h.select('manager'); assert.equal(group()[0].props.id, 'settings-manager');
  for (const type of ['god-mode-request-card', 'TaskPreferences', 'SuggestionsSettings']) assert.ok(nodes(h.tree).some(node => node.type === type));
  assert.doesNotMatch(text(h.tree), /Observe only|Autonomy/);
  control(h, 'settings_emergency_stop').props.onClick();
  const pause = h.requests.at(-1); assert.equal(pause.path, '/api/ventures/venture-a/pause');
  pause.resolve({ ok: true, status: 200, data: { paused: true } }); await settle(); h.render();
  assert.ok(control(h, 'venture_resume'));
  assert.ok(!nodes(h.tree).some(node => ['TokenMeter', 'TopUpCard'].includes(node.type)));
  h.select('models'); assert.match(text(h.tree), /owned-model/); assert.ok(input(h, 'llm-api-key'));
  h.select('mac');
  for (const type of ['DesktopSettings', 'BrowserProfilePreparation']) assert.ok(nodes(h.tree).some(node => node.type === type && node.props.ventureId === 'venture-a'));
  h.select('data'); button(h, "I'm stuck, download report").props.onClick();
  const report = h.requests.at(-1); assert.equal(report.path, '/api/ventures/venture-a/support-bundle'); assert.equal(report.init, undefined);
  report.resolve({ ok: true, status: 200, data: { report: true } }); await settle(); h.render(); assert.equal(h.downloads.length, 1);
  h.select('memory'); assert.equal(group().length, 1); assert.equal(group()[0].props.id, 'settings-memory');
  assert.ok(nodes(h.tree).some(node => node.type === 'VentureMemoryPanel' && node.props.compact));
  const danger = nodes(h.tree).find(node => node.props.className === 'settings-danger');
  assert.equal(danger.type, 'details'); assert.equal(danger.props.open, undefined);
  assert.equal(control(h, 'venture_delete').props.disabled, true);
  field(h, 'Type DELETE to confirm').props.onChange({ target: { value: 'delete' } }); h.render();
  control(h, 'venture_delete').props.onClick(); const deletion = h.requests.at(-1);
  assert.equal(deletion.path, '/api/ventures/venture-a/delete'); assert.equal(JSON.parse(deletion.init.body).confirmation, 'delete');
  deletion.resolve({ ok: true, status: 200 }); await settle(); assert.deepEqual(h.navigations, ['/']);
});

test('chat-first venture shortcuts close Settings before running the mapped action, while classic keeps links', async () => {
  for (const venture of ['venture-a', 'venture-b']) {
    for (const layout of ['home', 'classic']) {
      let closed = false;
      const h = harness('SettingsPage', { layout, props: { embedded: true, section: 'venture', onClose: () => { closed = true; } } });
      h.render(venture); await settle(); await completeSettings(h, 0, { venture });
      const shortcuts = nodes(h.tree).filter(node => node.props.className === 'settings-link');
      assert.deepEqual(shortcuts.map(text), ['Landing page', 'Tasks', 'Connections', 'Next step']);
      if (layout === 'classic') {
        assert.ok(shortcuts.every(node => node.type === 'a'));
        assert.deepEqual(shortcuts.map(node => node.props.href), [`/v/${venture}/site`, `/v/${venture}/tasks`, '/connections', `/v/${venture}/now`]);
        assert.equal(closed, false); assert.equal(h.events.length, 0);
        continue;
      }
      assert.ok(shortcuts.every(node => node.type === 'button' && node.props.href === undefined));
      for (const shortcut of shortcuts) {
        closed = false;
        h.listen('supymem:ask', () => assert.equal(closed, true));
        shortcut.props.onClick();
      }
      assert.deepEqual(h.events.map(event => event.detail.action), ['site.preview', 'status', 'connections.status', 'whats_next']);
      h.changeOwner('account-b');
      const count = h.events.length;
      shortcuts[0].props.onClick();
      assert.equal(h.events.length, count, 'a stale settings shortcut cannot act in another account');
    }
  }
});

test('compact request-based God mode recovers a failed read and never enables autopilot', async () => {
  const h = harness('GodModeRequestCard', { props: { compact: true } }); h.render();
  h.requests[0].resolve({ ok: false, status: 503 }); await settle(); h.render();
  button(h, 'Try again').props.onClick(); h.render();
  const read = h.requests.at(-1); assert.equal(read.path, '/api/ventures/venture-a/god-mode/requests/me?viewed=true');
  read.resolve({ ok: true, status: 200, data: { status: 'none', requested_at: null } }); await settle(); h.render();
  assert.ok(nodes(h.tree).some(node => node.props.className === 'settings-row'));
  button(h, 'Request God mode access').props.onClick(); h.render();
  nodes(h.tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
  const request = h.requests.at(-1); assert.equal(request.path, '/api/ventures/venture-a/god-mode/requests');
  assert.deepEqual(JSON.parse(request.init.body), { note: '' }); assert.equal(request.init.method, 'POST');
  assert.ok(h.requests.every(request => !request.path.includes('/autopilot')));
});

test('booking link read failure recovers and saves only the current founder draft', async () => {
  const h = harness('BookingLinkCard', { props: { workspaceId: 'workspace-a' } }); h.render();
  h.requests[0].resolve({ ok: false, status: 503 }); await settle(); h.render();
  assert.ok(button(h, 'Try again')); assert.equal(field(h, 'Booking link').props.disabled, true);
  button(h, 'Try again').props.onClick(); h.render();
  h.requests.at(-1).resolve({ ok: true, data: { booking_link: 'https://cal.com/real-founder' } }); await settle(); h.render();
  assert.equal(field(h, 'Booking link').props.value, 'https://cal.com/real-founder');
  field(h, 'Booking link').props.onChange({ target: { value: 'https://cal.com/updated-founder' } }); h.render();
  nodes(h.tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
  const write = h.requests.at(-1); assert.equal(write.init.method, 'PUT');
  assert.deepEqual(JSON.parse(write.init.body), { booking_link: 'https://cal.com/updated-founder' });
  write.resolve({ ok: true, data: { booking_link: 'https://cal.com/updated-founder' } }); await settle(); h.render();
  assert.equal(button(h, 'Save link').props.disabled, true);
});

test('compact memory retains provenance, explicit forgetting and read recovery', async () => {
  const h = harness('VentureMemoryPanel', { props: { compact: true } }); h.render();
  h.requests[0].resolve({ ok: false, status: 503 }); await settle(); h.render();
  button(h, 'Try again').props.onClick(); h.render();
  h.requests.at(-1).resolve({ ok: true, status: 200, data: { items: [{ id: 'memory-a', kind: 'decision', statement: 'Real saved decision', source: { type: 'chat' } }] } }); await settle(); h.render();
  assert.match(text(h.tree), /Real saved decision/); assert.match(text(h.tree), /your saved chat statement/);
  button(h, 'Forget').props.onClick(); const write = h.requests.at(-1);
  assert.equal(write.path, '/api/ventures/venture-a/memories/memory-a'); assert.equal(write.init.method, 'DELETE');
  write.resolve({ ok: true, status: 200 }); await settle(); h.render(); assert.doesNotMatch(text(h.tree), /Real saved decision/);
});
