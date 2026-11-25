const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

function harness({ dashboardFails = false, previewFails = true, live = true, query = '', holdDeployment = false, local = false, ventureId = 'venture' } = {}) {
  const hooks = [], effects = [], requests = [], listeners = new Map(), copied = [];
  let cursor = 0, actor = 'owner', epoch = 1, releaseDeployment;
  const deployment = { id: 'dep-live', app_id: 'app-live', target: 'supymem_static', state: 'live', version: 2,
    build_artifact_id: 'artifact-live', url: 'https://recalldesk.supymem.com', live_at: '2026-10-11T00:00:00Z' };
  const oldBuild = { id: 'build-old', app_id: 'app-live', runtime_version: 1, artifact_profile: 'static_landing_v1',
    state: 'failed', artifact_id: 'artifact-old', landing_page: { qa_report_available: true, available: false, qa_note: '53 passed · 8 failed' } };
  const liveBuild = { id: 'build-live', app_id: 'app-live', runtime_version: 1, artifact_profile: 'static_landing_v1',
    state: 'succeeded', completion_status: 'closed', qa_state: 'passed', qa_run_id: 'qa-live', artifact_id: 'artifact-live',
    review: { status: 'accepted' }, landing_page: { available: true, live_url: deployment.url, qa_note: null } };
  const respond = data => ({ ok: true, status: 200, data });
  const api = async path => {
    requests.push(path);
    if (path.endsWith('/apps')) return respond({ items: [{ id: 'app-live', kind: 'static' }] });
    if (path.endsWith('/deployments')) {
      if (holdDeployment) await new Promise(resolve => releaseDeployment = resolve);
      return respond({ items: live ? [deployment] : [] });
    }
    if (path.endsWith('/dashboard')) return dashboardFails ? { ok: false, status: 503 } : respond({ tiles: { website: { url: 'https://old.example.test', published: false } } });
    if (path.endsWith('/site/publication')) return respond({ state: 'blocked', live_url: null });
    if (path.endsWith('/assets/site')) return respond({ site: null });
    if (path.endsWith('/builds')) return respond({ items: [oldBuild, liveBuild] });
    if (path === '/api/builds/build-live') return respond(liveBuild);
    if (path === '/api/builds/build-old') return respond(oldBuild);
    if (path.endsWith('/qa/qa-live')) return respond({ id: 'qa-live', source_artifact_id: 'artifact-live', state: 'passed', verified_report: true, passed: 61 });
    if (path.includes('/public-copy')) return respond({ deployment_id: 'dep-live', state: 'clear', findings: [] });
    if (path.endsWith('/site/revisions')) return respond({ baseline: { deployment_id: 'dep-live', source_sha256: 'source-live' }, requests: [] });
    if (path.includes('/sites')) return { ok: false, status: 404 };
    return respond({ workspace_id: 'workspace', name: 'RecallDesk', raw_idea: 'Rebook patients', id: 'venture' });
  };
  const jsx = (type, props, key) => ({ type, props, key });
  const react = {
    useState(initial) { const index = cursor++; hooks[index] ||= { value: typeof initial === 'function' ? initial() : initial }; return [hooks[index].value, value => { hooks[index].value = typeof value === 'function' ? value(hooks[index].value) : value; }]; },
    useRef(initial) { return hooks[cursor++] ||= { current: initial }; },
    useEffect(callback, deps) { const index = cursor++, previous = hooks[index]; if (!previous || deps.some((value, offset) => value !== previous.deps[offset])) {
      hooks[index] = { deps }; effects.push(() => { previous?.cleanup?.(); hooks[index].cleanup = callback(); });
    } },
    useCallback(callback, deps) { const index = cursor++, previous = hooks[index]; if (!previous || deps.some((value, offset) => value !== previous.deps[offset])) hooks[index] = { deps, callback }; return hooks[index].callback; },
  };
  const root = join(__dirname, '../..'), modules = new Map();
  const actual = new Set(['godModeRequestScope', 'sitePresentation', 'companyJourney']);
  function compile(file) {
    if (modules.has(file)) return modules.get(file);
    const exports = {}; modules.set(file, exports);
    const output = ts.transpileModule(readFileSync(join(root, file), 'utf8'), { compilerOptions: {
      target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText;
    vm.runInNewContext(output, { exports, require: resolve, URL, URLSearchParams, AbortController, Blob,
      crypto: require('node:crypto').webcrypto, navigator: { clipboard: { writeText: async value => copied.push(value) } },
      window: { location: { search: query, hash: '' }, addEventListener: (name, callback) => listeners.set(name, callback),
        removeEventListener: name => listeners.delete(name), setTimeout: () => 1, clearTimeout() {} },
      document: { getElementById: () => null } });
    return exports;
  }
  function resolve(id) {
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
    if (id === 'next/navigation') return { useParams: () => ({ ventureId }), useSearchParams: () => new URLSearchParams(query) };
    if (id === '@/components/layout/LocalModeBoundary') return { useLocalMode: () => local };
    if (id === '@/lib/client') return { api, ensureSession: async () => {}, verifiedSessionSubject: () => actor, verifiedSessionEpoch: () => epoch,
      authenticatedFetch: async path => { requests.push(path); if (path.includes('/evidence/') && previewFails) return { ok: false, status: 503 };
        return { ok: true, arrayBuffer: async () => new ArrayBuffer(0) }; } };
    const name = id.split('/').at(-1);
    if (actual.has(name)) return compile(`lib/${name}.ts`);
    if (name === 'firstlookPreview') return { ventureSitePath: () => '/api/sites', scopedSiteMatches: () => false, firstlookPreviewPath: () => null };
    if (name === 'format') return { displayName: value => value, isLandingBuildPause: () => false, isReachableLandingUrl: value => /^https:\/\//.test(value || '') };
    if (name === 'founderAssetsApi') return { checkedSiteImport: value => value, siteImportFromSearch: () => undefined };
    if (name === 'buildReview') return { completedProofMatches: () => true, checkedReport: async () => ({ evidence: ['desktop', 'mobile'].map(size => ({ id: `${size}-1.png`, media_type: 'image/png',
      sha256: require('node:crypto').createHash('sha256').update('').digest('hex'), size_bytes: 0 })) }) };
    if (name === 'siteRevision') return { revisionLoadFailure: () => '', revisionSaveFailure: () => '' };
    if (id.endsWith('.module.css')) return { default: {} };
    return { [name]: name };
  }
  const Page = compile('app/v/[ventureId]/site/page.tsx').default;
  function render() { cursor = 0; const tree = Page(); effects.splice(0).forEach(effect => effect()); return tree; }
  const flush = async () => { for (let turn = 0; turn < 15; turn++) { await new Promise(setImmediate); render(); } return render(); };
  return { render, flush, requests, copied, deployment, liveBuild, release: () => releaseDeployment?.(),
    retire() { actor = 'other-owner'; epoch++; render(); listeners.get('supymem-session-changed')?.(); } };
}
function nodes(tree) {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return [tree, ...nodes(tree.props?.children), ...nodes(tree.props?.actions)];
}
function text(tree) {
  if (tree == null) return '';
  if (typeof tree !== 'object') return String(tree);
  return Array.isArray(tree) ? tree.map(text).join(' ') : text(tree.props?.children);
}

for (const dashboardFails of [false, true]) {
  test(`live deployment is shown first despite preview 503${dashboardFails ? ' and dashboard 503' : ''}`, async () => {
    const h = harness({ dashboardFails, query: '?run=build-old#build-review' }); h.render();
    const tree = await h.flush(), cards = nodes(tree).filter(node => node.type === 'OneCard');
    assert.equal(cards[0].props.id, 'site-live');
    assert.equal(cards[0].props.title, 'Your landing page is live');
    assert.equal(cards[0].props.status, 'done');
    for (const label of ['Visit landing page', 'Copy link', 'Build a new version', 'Find customers']) assert.ok(text(cards[0].props.actions).includes(label), label);
    assert.ok(nodes(cards[0].props.actions).some(node => node.props?.href === h.deployment.url));
    await nodes(cards[0].props.actions).find(node => node.type === 'button').props.onClick();
    assert.deepEqual(h.copied, [h.deployment.url]);
    assert.equal(cards.find(node => node.props.id === 'site-more').props.body, 'Review hosting status or take your landing page offline.');
    assert.ok(h.requests.some(path => path.includes('/evidence/desktop-1.png')), 'preview failed independently');
    assert.ok(!h.requests.includes('/api/builds/build-old'));
    assert.equal(nodes(tree).find(node => node.type === 'BuildEvidence').props.build.id, 'build-live');
    assert.ok(!nodes(tree).some(node => node.type === 'BuildPageCard' || node.type === 'PublishConceptPage'));
    assert.doesNotMatch(text(tree), /preview needs attention|53 passed|8 failed/);
  });
}
test('private builds retain failed checks and do not claim a live page', async () => {
  const h = harness({ live: false, query: '?run=build-old' }); h.render(); const tree = await h.flush();
  assert.ok(!nodes(tree).some(node => node.props?.id === 'site-live'));
  assert.equal(nodes(tree).find(node => node.type === 'BuildEvidence').props.build.id, 'build-old');
  assert.match(text(tree), /preview needs attention/);
});
test('Build a new version remains usable when the live preview cannot be read', async () => {
  const h = harness({ query: '?build=1' }); h.render(); const tree = await h.flush();
  assert.ok(nodes(tree).some(node => node.props?.id === 'site-live'));
  assert.equal(nodes(tree).filter(node => node.type === 'BuildPageCard').length, 1);
});
test('reading an accepted preview does not replace the live deployment or its management state', async () => {
  const h = harness({ previewFails: false }); h.render(); const tree = await h.flush();
  assert.equal(nodes(tree).filter(node => node.props?.id === 'site-live').length, 1);
  assert.equal(nodes(tree).find(node => node.props?.id === 'site-more').props.body, 'Review hosting status or take your landing page offline.');
  assert.ok(!nodes(tree).some(node => text(node) === 'Ready to publish'));
  assert.equal(nodes(tree).find(node => node.type === 'BuildEvidence').props.build.artifact_id, 'artifact-live');
});
test('delayed deployment reads cannot reveal the previous account’s live page', async () => {
  const h = harness({ holdDeployment: true }); h.render(); await new Promise(setImmediate);
  h.retire(); h.release(); const tree = await h.flush();
  assert.ok(!nodes(tree).some(node => node.props?.id === 'site-live'));
});

test('local pages use your publishing controls rather than hosted publication across domains', async () => {
  for (const ventureId of ['ven_inventory', 'ven_gardening']) {
    const app = harness({ local: true, ventureId }); app.render();
    const tree = await app.flush();
    const controls = nodes(tree).filter(node => node.type === 'LocalSiteControls');
    assert.equal(controls.length, 1);
    assert.equal(controls[0].props.ventureId, ventureId);
    assert.ok(!nodes(tree).some(node => node.type === 'PublishConceptPage'));
    assert.ok(!nodes(tree).some(node => node.props?.['data-usage-control'] === 'website_publish_review'));
    const url = `https://${ventureId.replace('_', '-')}.example.test`;
    controls[0].props.onPublication({ state: 'live', url });
    assert.ok(nodes(app.render()).some(node => node.props?.href === url));
    app.retire();
    controls[0].props.onPublication({ state: 'live', url });
    assert.ok(!nodes(app.render()).some(node => node.props?.href === url));
  }
});
