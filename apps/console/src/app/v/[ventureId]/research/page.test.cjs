const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

async function renderReport(report, options = {}) {
  const root = join(__dirname, '../../../..');
  const hooks = [], effects = [];
  let cursor = 0;
  const jsx = (type, props) => ({ type, props });
  const primitive = names => Object.fromEntries(names.map(name => [name, name]));
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in hooks)) hooks[index] = typeof initial === 'function' ? initial() : initial;
      return [hooks[index], value => { hooks[index] = typeof value === 'function' ? value(hooks[index]) : value; }];
    },
    useRef(initial) { const index = cursor++; return hooks[index] ||= { current: initial }; },
    useEffect(effect) { const index = cursor++; if (!(index in hooks)) { hooks[index] = true; effects.push(effect); } },
  };
  const browser = { location: { pathname: '/v/venture/research' } };
  function compile(relative) {
    const exports = {};
    const code = ts.transpileModule(readFileSync(join(root, relative), 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
    } }).outputText;
    vm.runInNewContext(code, { exports, require: resolve, URL, URLSearchParams, window: browser, crypto: {randomUUID: () => 'request-key'}, requestAnimationFrame() {} }, { filename: relative });
    return exports;
  }
  function resolve(id) {
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'Fragment' };
    if (id === 'next/link') return { default: 'a' };
    if (id === 'next/navigation') return { useParams: () => ({ ventureId: options.ventureId || 'venture' }), useSearchParams: () => new URLSearchParams(options.search), usePathname: () => browser.location.pathname, useRouter: () => ({ push() {}, replace: options.replace }) };
    if (id === '@/lib/client') return { ensureSession: async () => {}, verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => 1, downloadFile: options.downloadFile,
      api: async (path, init) => options.api?.(path, init) ?? ({ ok: true, data: path.endsWith('/research/dossiers') ? { items: [report] }
        : path.includes('/research/dossiers/') ? report : path.endsWith('/plans') ? { items: [] } : { workspace_id: 'workspace' } }) };
    if (['@/lib/researchPresentation', '@/lib/researchBrief', '@/lib/researchNextStep', '@/lib/nextStep'].includes(id))
      return compile(`lib/${id.split('/').at(-1)}.ts`);
    if (id === '@/lib/researchRecovery') return { unknownResearchRecovery: () => ({ state: 'unknown', error: '' }),
      loadResearchRecovery: async () => ({ state: 'confirmed', revisionId: 'current', personal: false, jobId: null, error: '' }), researchReceiptMatches: () => true };
    if (id === '@/components/layout/NextStepStrip') return { NextStepButton: 'NextStepButton', useNextStep: () => null };
    if (id === '@/hooks/useDurableJob') return { useDurableJob: (id, finish) => {
      options.observeJob?.(id, finish);
      return { job: null, label: '', error: '', retry() {} };
    }, jobSucceeded: job => job.status === 'succeeded' };
    if (id === '@/lib/localMode') return { isLocalConsole: () => true };
    if (id === '@/lib/detailLevel') return { useDetailLevel: () => ['brief'] };
    if (id === '@/lib/apiFailure') return {apiFailure: () => 'Open Settings to check your model provider.'};
    if (id === '@/lib/credits') return {usageLimitMessage: () => 'Open Settings to check your model provider.'};
    if (id.endsWith('.module.css')) return { default: new Proxy({}, { get: (_, key) => key }) };
    if (id.endsWith('/ResearchBrief')) return { ...primitive(['Reveal', 'RunningShimmer', 'SourceChip', 'Stat']),
      displayResearchText: compile('components/research/ResearchBrief.tsx').displayResearchText };
    if (id.endsWith('/OneCard')) return primitive(['OneCard', 'OneCardStack']);
    return primitive([id.split('/').at(-1)]);
  }
  const Page = compile('app/v/[ventureId]/research/page.tsx').default;
  Page();
  effects.splice(0).forEach(effect => effect());
  await new Promise(setImmediate);
  const render = () => { cursor = 0; return nodes(Page()); };
  return options.api ? render : render();
}

function nodes(value) {
  if (!value || typeof value !== 'object') return [];
  if (Array.isArray(value)) return value.flatMap(nodes);
  return [value, ...nodes(value.props?.children), ...nodes(value.props?.details), ...nodes(value.props?.actions)];
}

test('an explicit report link opens that dossier and exports under the venture name and date', async () => {
  for (const [ventureId, name, filename] of [['ven_freight', 'Depot Forecasts', 'Depot-Forecasts'], ['ven_hiking', 'Trail Notes', 'Trail-Notes']]) {
    const report = {id: 'older', kind: 'deep', state: 'complete', revision_id: 'current', created_at: '2026-08-14T12:00:00Z', summary: {}};
    const downloaded = [];
    const render = await renderReport(report, {ventureId, search: 'dossier=older',
      downloadFile: async (...args) => { downloaded.push(args); return true; },
      api: path => {
        if (path.endsWith('/research/dossiers')) return {ok: true, data: {items: [{...report, id: 'newer'}]}};
        if (path === `/api/ventures/${ventureId}`) return {ok: true, data: {name, workspace_id: 'workspace'}};
        if (path.endsWith('/newer')) assert.fail('The saved report selection was discarded');
      }});
    render().find(node => node.props?.children === 'Export report').props.onClick();
    await new Promise(setImmediate);
    assert.deepEqual(downloaded, [[`/api/ventures/${ventureId}/research/dossiers/older/export`, `${filename}-research-2026-08-14.md`]]);
  }
});

test('a rerun opens its own new report rather than the linked history or another run', async () => {
  for (const ventureId of ['stock.example', 'trails.test']) {
    const older = {id: 'older', kind: 'deep', state: 'complete', revision_id: 'current', summary: {}};
    const rerun = {...older, id: 'rerun-report', job_id: 'rerun-job'};
    let finish;
    const details = [], selections = [];
    const render = await renderReport(older, {ventureId, search: 'dossier=older&view=brief',
      replace: href => selections.push(href), observeJob: (id, done) => { if (id === rerun.job_id) finish = done; },
      api: path => {
        if (path.endsWith('/rerun')) return {ok: true, data: {job_id: rerun.job_id}};
        if (path.endsWith('/research/dossiers')) return {ok: true, data: {items: [{...rerun, id: 'unrelated', job_id: 'other-job'}, rerun, older]}};
        if (path.includes('/research/dossiers/')) {
          details.push(path.split('/').at(-1));
          return {ok: true, data: details.at(-1) === rerun.id ? rerun : older};
        }
      }});
    render().find(node => node.props?.children === 'Run research again').props.onClick();
    await new Promise(setImmediate);
    render();
    finish({id: rerun.job_id, kind: 'research.deep', venture_id: ventureId, revision_id: 'current', status: 'succeeded'});
    await new Promise(setImmediate);
    assert.deepEqual(details, ['older', 'rerun-report']);
    assert.deepEqual(selections.map(href => new URL(href, 'http://app.test').search), ['?dossier=rerun-report&view=brief']);
    assert.equal(render().find(node => node.type === 'select').props.value, rerun.id);
  }
});

test('queued research keeps running off-screen, but quitting requires reopening Supymem', async () => {
  for (const ventureId of ['stock.example', 'trails.test']) {
    const requests = [];
    const render = await renderReport({id: 'report', kind: 'deep', state: 'queued', job_id: 'queued-job', revision_id: 'current', summary: {}},
      {ventureId, api: (path, init) => { if (init?.method === 'POST') { requests.push(path); return {ok: true, data: {}}; } }});
    const progress = render().find(node => node.type === 'OneCard' && node.props.status === 'working');
    const copy = JSON.stringify(progress.props.children);
    assert.match(copy, /leave Research/i);
    assert.match(copy, /while Supymem is open/i);
    assert.match(copy, /quit.*reopen it.*saved progress/i);
    nodes(progress).find(node => node.props?.children === 'Stop research').props.onClick();
    await new Promise(setImmediate);
    assert.deepEqual(requests, [`/api/ventures/${ventureId}/research/jobs/queued-job/stop`]);
    assert.ok(!render().some(node => node.props?.children === 'Stop research'));
  }
});

test('rerun shows the exact search setup message and link without blocking the report', async () => {
  for (const ventureId of ['ven_stock', 'ven_walk']) {
    const body = 'Save your Tavily search API key on the Keys and connections screen, then retry.';
    const render = await renderReport({ id: 'report', kind: 'deep', state: 'complete', revision_id: 'current', summary: {} }, { ventureId,
      api: path => path.endsWith('/rerun') ? {ok: true, data: {body_md: body, actions: [{label: 'Keys and connections', href: '/settings/local'}]}} : undefined });
    render().find(node => node.props?.children === 'Run research again').props.onClick();
    await new Promise(setImmediate);
    const view = render();
    assert.ok(view.some(node => node.props?.role === 'status' && JSON.stringify(node.props.children).includes(body)));
    assert.ok(view.some(node => node.props?.href === '/settings/local'));
    assert.equal(view.find(node => node.props?.children === 'Run research again').props.disabled, false);
  }
});

test('research card keeps sourced findings and a recommended opening, not missing-evidence lists', async () => {
  for (const customer of ['commercial electricians', 'home gardeners']) {
    const url = 'https://alternative.example/product';
    const entry = { finding: `Offers a focused workflow for ${customer}.`, url, source_id: 'source' };
    const opening = `Your best opening: start with ${customer}, positioned around one useful result. Use a focused page to reach them.`;
    const rendered = await renderReport({ id: 'report', kind: 'deep', state: 'complete', revision_id: 'current',
      findings: { source: [{ id: 'source', title: 'An alternative', summary: entry.finding, url, category: 'services' }] },
      summary: { next_questions: [{ text: 'No direct conversation recorded', kind: 'demand' }],
        founder_report: { findings: [entry], contradictions: [], assumptions: ['No identified customer'], opening,
          dimensions: { competition: { support: [entry], challenge: [] } } } } });
    assert.ok(rendered.some(node => node.type === 'h3' && node.props.children === 'Your opening'));
    assert.ok(rendered.some(node => node.type === 'p' && node.props.children === opening));
    assert.ok(rendered.some(node => node.type === 'a' && node.props.href === url && node.props.children === 'alternative.example'));
    assert.ok(!/What to find out next|What challenges the idea|Risks and alternatives|No direct conversation|No identified/.test(JSON.stringify(rendered)));
  }
});

test('saved search outcomes offer a next step instead of an empty findings card', async () => {
  for (const ventureId of ['inventory', 'cycling']) {
    for (const outcome of ['limited', 'unavailable', 'no_matches', 'key_rejected']) {
      const next_step = `A next step for ${ventureId}`;
      const settings_href = outcome === 'no_matches' ? undefined : '/settings/local';
      const report = {id: 'saved', kind: 'deep', state: outcome === 'key_rejected' ? 'failed' : 'partial', revision_id: 'current',
        summary: {search_notice: {state: outcome, next_step, settings_href}}};
      const rendered = await renderReport(report, {ventureId});
      assert.ok(rendered.some(node => node.type === 'OneCard' && node.props.body === next_step));
      assert.ok(!rendered.some(node => node.props?.title === 'What we found' || node.props?.children === 'Your opening'));
      assert.equal(rendered.some(node => node.type === 'a' && node.props.href === '/settings/local'), !!settings_href);
    }
  }
});

test('research renders only sourced topics and one constructive empty-report action across domains', async () => {
  for (const [title, finding, url] of [
    ['Warehouse inventory', 'Tracks warehouse inventory and replenishment.', 'https://stock.example/product'],
    ['Piano lessons', 'Guides piano practice with interactive lessons.', 'https://practice.example/lessons'],
  ]) {
    const source = { id: 'source', title, summary: finding, url, category: 'source' };
    const entry = { finding, url, source_id: source.id };
    const message = "Your next move: share a relevant link or ask potential customers about their current workaround.";
    for (const found of [true, false]) {
      const rendered = await renderReport({ id: 'report', kind: 'deep', state: 'complete', revision_id: 'current',
        findings: { source: found ? [source] : [] }, summary: {
          evidence_dimensions: { competition: { support: [entry] }, pricing: { status: 'unknown', unknown: 'Still no evidence found for pricing.' } },
          founder_report: { findings: found ? [entry] : [], contradictions: [], assumptions: [],
            dimensions: found ? { competition: { support: [entry], challenge: [] } } : {}, empty_message: found ? null : message },
        } });
      const headings = rendered.filter(node => node.type === 'h3').map(node => node.props.children);
      assert.ok(!rendered.some(node => node.type === 'h2' && ['Service costs and requirements', 'Competitor details from saved excerpts'].includes(node.props.children)));
      assert.ok(!headings.includes('Pricing'));
      assert.equal(rendered.filter(node => node.props?.['aria-labelledby'] === 'evidence-dimensions-title').length, found ? 1 : 0);
      assert.equal(rendered.filter(node => node.type === 'OneCard' && node.props.body === message).length, found ? 0 : 1);
      if (found) {
        assert.ok(headings.includes('Competition'));
        assert.ok(rendered.some(node => node.type === 'a' && node.props.href === url && node.props.children === new URL(url).hostname));
      }
      assert.ok(!JSON.stringify(rendered).includes('Still no evidence found'));
    }
  }
});

test('research citations show distinct source domains without changing saved findings across audiences', async () => {
  for (const audience of ['warehouse managers', 'home gardeners']) {
    const entries = [
      { source_id: 'first', finding: `A useful workflow for ${audience}.`, url: 'https://www.workflow.example/product' },
      { source_id: 'second', finding: `A discussion by ${audience}.`, url: 'https://community.example/thread' },
    ];
    const original = JSON.stringify(entries);
    const rendered = await renderReport({ id: 'report', kind: 'deep', state: 'complete', revision_id: 'current',
      findings: { source: entries.map(entry => ({ id: entry.source_id, title: audience, summary: entry.finding,
        url: entry.url, category: 'source' })) },
      summary: { founder_report: { findings: entries, contradictions: [], dimensions: {} } } });
    for (const [index, domain] of ['workflow.example', 'community.example'].entries()) {
      const citation = rendered.find(node => node.type === 'a' && node.props.href === entries[index].url
        && node.props.target === '_blank');
      assert.equal(citation?.props.children, domain);
      assert.equal(citation.props.rel, 'noopener noreferrer');
    }
    assert.equal(JSON.stringify(entries), original);
  }
});

test('your requested research starts directly in unrelated domains', async () => {
  const source = ts.createSourceFile('page.tsx', readFileSync(join(__dirname, 'page.tsx'), 'utf8'), ts.ScriptTarget.Latest, true);
  const methods = {};
  function visit(node) { if (ts.isFunctionDeclaration(node) && node.name?.text === 'startResearch') methods[node.name.text] = node.getText(source); ts.forEachChild(node, visit); }
  visit(source);
  for (const domain of ['Factory inventories', 'Family gardens']) {
    const calls = [], recovery = {state: 'confirmed', personal: false, revisionId: 'saved-revision'};
    const context = {startConfirmed: false, pending: {current: false}, busy: false, refreshing: false, jobId: null, syncError: '', recovery,
      generation: {current: 1}, active: {current: domain}, ventureId: domain, verifiedSessionSubject: () => 'owner',
      setBusy() {}, setNotice() {}, setRecovery() {}, observeJob() {}, setSyncError() {}, setStartConfirmed() {}, setReviewQuote() {},
      loadResearchRecovery: async () => recovery, researchStartDecision: () => 'start', report: {id: 'saved-report'},
      crypto: {randomUUID: () => 'requested-run'}, apiFailure: String,
      api: async (path, init) => { calls.push({path, init}); return {ok: true, data: init?.method === 'POST' ? {job_id: 'requested'} : {workspace_id: 'workspace'}}; }};
    vm.runInNewContext(ts.transpileModule(Object.values(methods).join('\n') + ';globalThis.start = startResearch;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    await context.start();
    assert.equal(calls.filter(call => call.init?.method === 'POST').length, 1);
  }
});

test('saved-source review starts once from the first click only when the quote is sufficient', async () => {
  for (const ventureId of ['ven_freight', 'ven_hiking']) for (const sufficient_now of [true, false]) {
    const calls = [];
    const render = await renderReport({id: 'report', kind: 'deep', state: 'complete', revision_id: 'current', summary: {},
      findings: {services: [{id: 'source', title: 'One source', url: 'https://source.example'}]}}, {ventureId,
      api: (path, init) => {
        if (path.endsWith('/review-quote')) {
          calls.push('quote'); return {ok: true, data: {source_digest: 'saved-source', sufficient_now}};
        }
        if (path.endsWith('/rerun')) { calls.push(JSON.parse(init.body)); return {ok: true, data: {job_id: 'job'}}; }
      }});
    const button = render().find(node => node.props?.children === 'Review saved sources');
    button.props.onClick(); button.props.onClick();
    await new Promise(setImmediate);
    assert.deepEqual(calls, sufficient_now ? ['quote', {phase: 'review', source_digest: 'saved-source', request_key: 'request-key'}] : ['quote']);
    if (!sufficient_now) assert.equal(render().filter(node => node.props?.children === 'Review saved sources').length, 1);
  }
});
