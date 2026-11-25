const assert = require('node:assert/strict'), test = require('node:test');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript');
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : !tree || typeof tree !== 'object'
  ? [] : [tree, ...nodes(tree.props?.children)];

test('failed unstarted builds are not shown as cancellations across audiences', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '../../../../../lib/deviceOrderStatus.ts'), 'utf8');
  const exports = {};
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText,
    {exports, require: name => {assert.equal(name, './macResult.ts'); return {isVerifiedMacTask: () => false};}});
  for (const domain of ['Workshop inventory', 'Family gardening']) {
    const status = exports.deviceOrderStatus('failed', {expired: false, receipt: null});
    assert.match(status, /failed before it started/, domain);
    assert.match(status, /venture chat/, domain);
    assert.doesNotMatch(status, /[Cc]ancelled|[Aa]pproved|Mac/, domain);
  }
});

function harness(title, receipt, {busy = false, desktop = true, reviewed = false, local = false,
  loop, deploy, coreReceipt, nativeReply, apiReply} = {}) {
  const saved = {id: 'order-a', venture_id: 'venture-a', actor_id: 'founder', state: 'claimed',
    claimed_at: '2026-10-26T00:00:00Z', reviewed_at: reviewed ? '2026-10-26T00:01:00Z' : undefined,
    review_digest: 'a'.repeat(64), expires_at: '2026-10-27T00:00:00Z', manifest: {
      title, instruction: `Review ${title}`, applications: ['com.apple.Safari'], origins: ['https://example.test'],
      actions: ['browser.read'], credit_ceiling_micro: 1000, duration_seconds: 60}};
  if (loop || deploy) {
    saved.state = 'stopped'; saved.manifest.coding_loop_verification = loop;
    saved.manifest.deploy_verification = deploy;
  }
  const state = [saved, 'available', 'available', '', busy, false, false, Date.now(), receipt], calls = [];
  if (coreReceipt) state[18] = coreReceipt;
  const refs = [], effects = [], frames = [];
  let cursor = 0, refCursor = 0;
  const jsx = (type, props) => ({type, props}), exports = {};
  const mocks = {
    react: {useState(initial) { const index = cursor++; if (!(index in state)) state[index] = initial;
      return [state[index], value => {state[index] = typeof value === 'function' ? value(state[index]) : value;}]; },
      useRef: current => refs[refCursor++] ||= {current}, useEffect(effect, deps) {effects.push({effect, deps});}},
    'react/jsx-runtime': {jsx, jsxs: jsx}, 'next/navigation': {useRouter: () => ({push() {}, replace: href => calls.push({redirect: href})})},
    '@/lib/client': {ensureSession: async () => {}, verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async (url, init) => {calls.push({url, init});
        if (apiReply) return apiReply(url, init, saved);
        if (url.endsWith('/acknowledge')) saved.reviewed_at = '2026-10-26T00:02:00Z';
        if (url.endsWith('/approve')) saved.state = 'approved';
        return {ok: true, data: saved}; }},
    '@/lib/desktop': {isDesktopShell: () => desktop, invokeErrorMessage: (_error, fallback) => fallback, desktopInvokeOrThrow: async (command, args) => {
      calls.push({command, args}); return command === 'inspect_device_work_order' ? saved
        : nativeReply ? nativeReply(args) : receipt; }},
    '@/components/desktop/ProductBuildReview': {ProductBuildReview: 'ProductBuildReview', productPreviewUrl: receipt => receipt?.url},
    '@/lib/deviceOrderStatus': {deviceOrderStatus: () => 'Review your saved task'},
    '@/lib/macResult': {isVerifiedMacTask: () => false, macResultHeadline: () => 'Unverified result'},
    '@/lib/credits': {formatMicroCreditUsage: value => String(value)},
    '@/lib/desktopExecutionAvailability': {isNativeDesktopManifest: () => false},
    '@/lib/productBuildCaps': {parseProductBuildLoop: () => undefined, productBuildActorMatches: () => true},
    '@/lib/browserExecutionProfile': {parseBrowserExecutionProfile: () => undefined, browserReadOnlyAuthority: () => false},
    '@/lib/browserWatch': {needsYouInstruction: () => 'You need to review this task', nativePollMs: () => 5000},
    '@/lib/localMode': {isLocalConsole: () => local},
    '@/components/layout/LocalModeBoundary': {useFeatureVisibility: () => () => !local},
    '@/lib/loopProgress': require('../../../../../lib/loopProgress.ts'),
    '@/lib/errorCopy': {plainErrorText: (_error, fallback) => fallback},
  };
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, 'page.tsx'), 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText + '\nexports.Review = Review;', {exports, window: {location: {assign: url => calls.push({open: url})},
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    addEventListener() {}, removeEventListener() {},
    requestAnimationFrame: callback => frames.push(callback)}, navigator: {clipboard: {writeText: async value => calls.push({copy: value})}}, require: id =>
    id.endsWith('.css') ? {default: {}} : mocks[id] || {}});
  return {calls, saved, effects, state, frames, render() {cursor = refCursor = 0; effects.length = 0;
    return exports.Review({ventureId: saved.venture_id, orderId: saved.id});}};
}

test('your local task review hides computer controls but keeps connected coding-tool builds across domains', () => {
  for (const domain of ['Factory inventory', 'Family recipes']) {
    const hidden = harness(domain, null, {local: true});
    assert.ok(JSON.stringify(hidden.render()).includes('not in this version yet'));
    assert.equal(nodes(hidden.render()).some(node => node.type === 'button'), false);
    const build = harness(domain, null, {local: true, loop: {provider: 'codex', profile: 'web_app_vercel_v1', workspace_slug: 'workspace', phase: 'implement'}});
    assert.ok(nodes(build.render()).some(node => node.type === 'ProductBuildReview'));
  }
});

test('your activity uses saved Core updates and fetches the complete log only on opening it', async () => {
  for (const domain of ['Factory inventory', 'Family recipes']) {
    const events = Array.from({length: 52}, (_, at) => ({at, text: `${domain}: Saved step ${at}`}));
    const build = harness(domain, {state: 'running_coding', events: []}, {local: true,
      loop: {provider: 'codex', phase: 'implement', max_iterations: 2}, apiReply: () => ({ok: true, data: {events}})});
    build.saved.events = events.slice(-50);
    const activity = nodes(build.render()).find(node => node.props?.maximum === 2 && node.props?.events);
    assert.deepEqual(activity.props.events, build.saved.events);
    assert.deepEqual(build.calls, []);
    activity.props.onOpenActivity();
    await new Promise(done => setImmediate(done));
    assert.equal(build.calls[0].url, '/api/ventures/venture-a/device-work-orders/order-a/events');
    assert.equal(nodes(build.render()).find(node => node.props?.maximum === 2 && node.props?.events).props.events.length, 52);
  }
});

test('your local build activity occupies the full height across domains', () => {
  for (const domain of ['Warehouse dispatch', 'Neighborhood cooking']) {
    const build = harness(domain, null, {local: true, loop: {provider: 'codex', phase: 'plan', max_iterations: 3}});
    const screen = nodes(build.render()).find(node => node.props?.['aria-label'] === 'Your build activity');
    assert.ok(screen && Object.hasOwn(screen.props, 'className'), domain);
    const css = fs.readFileSync(path.join(__dirname, 'review.module.css'), 'utf8');
    assert.match(css, /\.activity\s*\{[^}]*min-height: 100%;[^}]*box-sizing: border-box/);
  }
});

test('your local Stop rejects another build receipt and waits for confirmed cleanup across domains', async () => {
  for (const title of ['Supplier inventory', 'Trail planner']) {
    let resolve;
    const running = {order_id: 'order-a', state: 'running_coding', tool: 'coding', task_verified: false};
    const app = harness(title, running, {local: true, loop: {provider: 'codex', profile: 'web_app_vercel_v1',
      phase: 'implement', workspace_slug: 'project', max_iterations: 6}, nativeReply: args => args.action === 'cancel'
        ? new Promise(done => {resolve = done;}) : running});
    const props = nodes(app.render()).find(node => node.type === 'ProductBuildReview').props;
    props.onStopBuild();
    assert.equal(nodes(app.render()).find(node => node.type === 'ProductBuildReview').props.watch.state, 'stopping');
    resolve({order_id: 'other-order', state: 'stopped', task_verified: true});
    await new Promise(done => setImmediate(done));
    assert.equal(nodes(app.render()).find(node => node.type === 'ProductBuildReview').props.watch.state, 'running_coding');
    assert.match(JSON.stringify(app.render()), /could not confirm|Could not confirm/);
    assert.equal(app.calls[0].args.action, 'cancel');
    assert.doesNotMatch(JSON.stringify(app.render()), /I reviewed this task|Confirm task permission|credit_ceiling/);
  }
});

test('Check Stop stays bound to the saved build in a plain browser across domains', async () => {
  for (const title of ['Supplier inventory', 'Trail planner']) {
    const app = harness(title, null, {local: true, desktop: false, loop: {provider: 'codex',
      profile: 'web_app_vercel_v1', phase: 'implement', workspace_slug: 'project', max_iterations: 6}});
    app.saved.state = 'claimed';
    app.saved.stop_reconciliation_required = true;
    const props = buildProps(app);
    assert.equal(props.watch.state, 'reconciliation_required');
    props.onStopBuild();
    await new Promise(done => setImmediate(done));
    const notice = nodes(app.render()).find(node => node.props?.source === 'builds.local_activity');
    assert.match(notice?.props.error, /Refresh build in Build activity/);
    assert.equal(props.onRetryBuild, undefined);
    assert.equal(app.calls[0].url, '/native/device-work-orders/venture-a/order-a/reconcile');
    assert.equal(app.calls[0].init.method, 'POST');
  }
});

test('your local finished result reveals the reviewed folder using existing native commands across domains', async () => {
  for (const title of ['Supplier inventory', 'Trail planner']) {
    const receipt = {order_id: 'order-a', state: 'stopped', task_verified: true,
      evidence: {processes_stopped: true, product_build: {outcome: 'pass'}}};
    const app = harness(title, receipt, {local: true, loop: {provider: 'codex', profile: 'web_app_vercel_v1',
      phase: 'implement', workspace_slug: 'reviewed-project', max_iterations: 6},
      nativeReply: () => '/Users/owner/Documents/Supymem/ventures/reviewed-project'});
    const props = nodes(app.render()).find(node => node.type === 'ProductBuildReview').props;
    props.onRevealFolder();
    await new Promise(done => setImmediate(done));
    assert.deepEqual(app.calls.map(call => call.command), ['venture_workspace_path', 'reveal_in_file_manager']);
    assert.equal(app.calls[0].args.folderName, 'reviewed-project');
    assert.equal(app.calls[1].args.path, '/Users/owner/Documents/Supymem/ventures/reviewed-project');
  }
});

test('a settled product starts preview without an existing preview or credit receipt, and only opens the matching version', async () => {
  for (const title of ['Warehouse dispatch', 'Neighborhood cooking']) {
    const build = {outcome: 'pass', source_commit: 'a'.repeat(40), file_manifest_sha256: 'b'.repeat(64)};
    const saved = {order_id: 'order-a', state: 'stopped', task_verified: true,
      evidence: {processes_stopped: true, product_build: build}};
    const loop = {provider: 'codex', profile: 'web_app_vercel_v1', phase: 'implement', workspace_slug: 'project', max_iterations: 4};
    for (const commit of [build.source_commit, 'f'.repeat(40)]) {
      const url = 'http://127.0.0.1:41723';
      const app = harness(title, saved, {local: true, loop, nativeReply: ({action}) => action === 'start'
        ? {...saved, product_preview: {...build, source_commit: commit, url, transport: 'device_order_v1', stop_available: true}}
        : saved});
      assert.deepEqual(app.calls, []);
      buildProps(app).onRetryPreview();
      await new Promise(done => setImmediate(done));
      assert.equal(app.calls[0].args.action, 'start');
      assert.equal(app.calls.find(call => call.open)?.open, commit === build.source_commit ? url : undefined);
      buildProps(app).onStopPreview();
      await new Promise(done => setImmediate(done));
      assert.equal(app.calls.at(-1).args.action, 'cancel');
    }
    const browser = harness(title, null, {local: true, desktop: false, loop});
    browser.saved.native_stop = {processes_stopped: true, task_verified: true};
    assert.equal(buildProps(browser).browserPreview, undefined);
    assert.equal(buildProps(browser).onRetryPreview, undefined);
  }
});

test('your local failed build retry requests a fresh saved order across domains', async () => {
  for (const title of ['Supplier inventory', 'Trail planner']) {
    const receipt = {order_id: 'order-a', state: 'stopped', task_verified: false,
      evidence: {processes_stopped: true, product_build: {outcome: 'tests_failed'}}};
    const app = harness(title, receipt, {local: true, loop: {provider: 'codex', profile: 'web_app_vercel_v1',
      phase: 'implement', workspace_slug: 'reviewed-project', max_iterations: 6},
      apiReply: () => ({ok: true, data: {review_path: '/v/venture-a/device-work-orders/retry-order'}})});
    nodes(app.render()).find(node => node.type === 'ProductBuildReview').props.onRetryBuild();
    await new Promise(done => setImmediate(done));
    assert.equal(app.calls[0].url, '/api/ventures/venture-a/device-work-orders/order-a/renew');
    assert.equal(app.calls[0].init.method, 'POST');
    assert.equal(app.calls.some(call => call.command === 'device_work_order_action'), false);
  }
});

test('browser build activity starts approved work, polls, stops, and retries through its scoped API', async () => {
  for (const domain of ['Supplier inventory', 'Trail planner']) {
    const loop = {provider: 'codex', profile: 'web_app_vercel_v1', phase: 'implement', workspace_slug: 'project', max_iterations: 4};
    const active = {order_id: 'order-a', state: 'running_coding', task_verified: false};
    const app = harness(domain, {order_id: 'order-a', state: 'not_started'}, {local: true, desktop: false, loop, apiReply: (url, _init, order) =>
      ({ok: true, data: url.includes('/native/') ? active : url.endsWith('/renew')
        ? {review_path: '/v/venture-a/device-work-orders/retry-order'} : order})});
    app.saved.state = 'approved'; delete app.saved.claimed_at;
    app.saved.expires_at = '2099-01-01T00:00:00Z';
    app.saved.requested_build = {request_sha256: 'a'.repeat(64)};
    app.state[13] = true;
    const prefix = '/native/device-work-orders/venture-a/order-a';
    buildProps(app).onStartBuild();
    await new Promise(done => setImmediate(done));
    assert.equal(app.calls[0].url, `${prefix}/start`);
    buildProps(app).onStopBuild();
    await new Promise(done => setImmediate(done));
    assert.ok(app.calls.some(call => call.url === `${prefix}/cancel` && call.init.method === 'POST'));
    app.saved.state = 'claimed';
    app.render();
    const poll = app.effects.find(row => row.effect.toString().includes('nativePollMs'));
    const cleanup = poll.effect();
    await new Promise(done => setImmediate(done));
    cleanup();
    assert.ok(app.calls.some(call => call.url === `${prefix}/status`));
    app.saved.state = 'stopped';
    app.state[8] = {...active, state: 'stopped', evidence: {processes_stopped: true, product_build: {outcome: 'tests_failed'}}};
    buildProps(app).onRetryBuild();
    await new Promise(done => setImmediate(done));
    assert.ok(app.calls.some(call => call.url.endsWith('/order-a/renew')));
    assert.ok(!app.calls.some(call => call.command));
  }
});

test('browser opens a verified result through the scoped preview action without desktop commands', async () => {
  for (const domain of ['Supplier inventory', 'Trail planner']) {
    const build = {source_commit: 'a'.repeat(40), file_manifest_sha256: 'b'.repeat(64), outcome: 'pass'};
    const folder = `/home/owner/Supymem/${domain}`;
    const saved = {order_id: 'order-a', state: 'stopped', task_verified: true, result_folder: folder,
      evidence: {processes_stopped: true, product_build: build}};
    const loop = {provider: 'codex', profile: 'web_app_vercel_v1', phase: 'implement', workspace_slug: 'project', max_iterations: 4};
    const url = 'http://127.0.0.1:41723';
    const app = harness(domain, saved, {local: true, desktop: false, loop,
      apiReply: () => ({ok: true, data: {...saved, product_preview: {...build, url, transport: 'device_order_v1', stop_available: true}}})});
    const review = buildProps(app);
    assert.equal(review.onRevealFolder, undefined);
    assert.ok(!review.browserPreview);
    const copy = nodes(app.render()).find(node => node.props?.children === 'Copy result folder');
    assert.ok(copy);
    copy.props.onClick();
    await new Promise(done => setImmediate(done));
    assert.ok(app.calls.some(call => call.copy === folder));
    review.onRetryPreview();
    await new Promise(done => setImmediate(done));
    assert.ok(app.calls.some(call => call.url === '/native/device-work-orders/venture-a/order-a/start'));
    assert.equal(app.calls.find(call => call.open)?.open, url);
  }
});

test('your approved local build starts once only when it carries your explicit request', async () => {
  for (const domain of ['Supplier inventory', 'Trail planner']) {
    for (const requested of [true, false]) {
      const app = harness(domain, null, {local: true, loop: {provider: 'codex', profile: 'web_app_vercel_v1',
        phase: 'implement', workspace_slug: 'reviewed-project', max_iterations: 6},
        nativeReply: () => ({order_id: 'order-a', state: 'running_coding', task_verified: false})});
      app.saved.state = 'approved'; delete app.saved.claimed_at;
      app.saved.expires_at = '2099-01-01T00:00:00Z'; app.state[13] = true;
      if (requested) app.saved.requested_build = {request_sha256: 'a'.repeat(64)};
      app.render();
      const start = app.effects.find(row => row.deps?.[1] === (requested ? 'a'.repeat(64) : undefined)
        && row.effect.toString().includes('requestedStart'));
      assert.ok(start);
      start.effect(); start.effect();
      await new Promise(done => setImmediate(done));
      assert.equal(app.calls.filter(call => call.command === 'device_work_order_action' && call.args.action === 'start').length, requested ? 1 : 0);
    }
  }
});

test('your local plan follows its verified coding continuation without accepting another order receipt', async () => {
  for (const domain of ['Supplier inventory', 'Trail planner']) {
    for (const wrongOrder of [true, false]) {
      const receipt = {order_id: 'order-a', state: 'stopped', task_verified: true,
        evidence: {processes_stopped: true, product_build: {outcome: 'plan_ready'}}};
      const app = harness(domain, receipt, {local: true, loop: {provider: 'codex', profile: 'web_app_vercel_v1',
        phase: 'plan', workspace_slug: 'reviewed-project', max_iterations: 2},
        nativeReply: () => ({...receipt, order_id: wrongOrder ? 'other-order' : 'order-a', next_order_id: 'coding-order'})});
      app.render();
      const cleanup = app.effects[1].effect();
      await new Promise(done => setImmediate(done)); cleanup();
      assert.equal(app.calls.filter(call => call.redirect).length, wrongOrder ? 0 : 1);
      if (!wrongOrder) assert.equal(app.calls.find(call => call.redirect).redirect, '/v/venture-a/device-work-orders/coding-order');
    }
  }
});

test('you retry an unstarted tool failure on the same authorized order instead of trying to renew a live request', async () => {
  for (const domain of ['Supplier inventory', 'Trail planner']) {
    const app = harness(domain, {order_id: 'order-a', state: 'not_started', failure_code: 'coding_sign_in_required'},
      {local: true, loop: {provider: 'codex', profile: 'web_app_vercel_v1', phase: 'plan',
        workspace_slug: 'reviewed-project', max_iterations: 2},
        nativeReply: () => ({order_id: 'order-a', state: 'running_coding', task_verified: false})});
    app.saved.state = 'approved'; delete app.saved.claimed_at;
    app.state[13] = true; app.saved.expires_at = '2099-01-01T00:00:00Z';
    nodes(app.render()).find(node => node.type === 'ProductBuildReview').props.onRetryBuild();
    await new Promise(done => setImmediate(done));
    assert.equal(app.calls[0].command, 'device_work_order_action');
    assert.equal(app.calls[0].args.action, 'start');
    assert.equal(app.calls.some(call => call.url?.endsWith('/renew')), false);
  }
});

test('you can still stop your build when live status is unavailable and a successful poll clears that warning', async () => {
  for (const domain of ['Supplier inventory', 'Trail planner']) {
    let failed = true;
    const receipt = {order_id: 'order-a', state: 'running_coding', task_verified: false};
    const app = harness(domain, receipt, {local: true, loop: {provider: 'codex', profile: 'web_app_vercel_v1',
      phase: 'implement', workspace_slug: 'reviewed-project', max_iterations: 6},
      nativeReply: () => {if (failed) throw new Error('private-fixture'); return receipt;}});
    app.render();
    let cleanup = app.effects[1].effect();
    await new Promise(done => setImmediate(done)); cleanup();
    assert.match(JSON.stringify(app.render()), /latest build status could not be checked.*Refresh build/);
    assert.equal(typeof nodes(app.render()).find(node => node.type === 'ProductBuildReview').props.onStopBuild, 'function');
    assert.doesNotMatch(JSON.stringify(app.render()), /private-fixture/);
    failed = false;
    cleanup = app.effects[1].effect();
    await new Promise(done => setImmediate(done)); cleanup();
    assert.doesNotMatch(JSON.stringify(app.render()), /latest build status could not be checked/);
  }
});

test('your local Stop does not wait for a folder action and that older action cannot overwrite the stopped result', async () => {
  for (const domain of ['Supplier inventory', 'Trail planner']) {
    let folder;
    const receipt = {order_id: 'order-a', state: 'running_coding', task_verified: false};
    const stopped = {...receipt, state: 'stopped', evidence: {processes_stopped: true, outcome: 'stopped'}};
    const app = harness(domain, receipt, {local: true, loop: {provider: 'codex', profile: 'web_app_vercel_v1',
      phase: 'implement', workspace_slug: 'reviewed-project', max_iterations: 6},
      nativeReply: args => args.action ? stopped : new Promise(resolve => {folder = resolve;})});
    const props = nodes(app.render()).find(node => node.type === 'ProductBuildReview').props;
    props.onRevealFolder(); props.onStopBuild();
    await new Promise(done => setImmediate(done));
    assert.equal(nodes(app.render()).find(node => node.type === 'ProductBuildReview').props.watch.state, 'stopped');
    folder('/Users/owner/Documents/Supymem/ventures/reviewed-project');
    await new Promise(done => setImmediate(done));
    assert.equal(nodes(app.render()).find(node => node.type === 'ProductBuildReview').props.watch.state, 'stopped');
    assert.equal(app.calls.filter(call => call.command === 'reveal_in_file_manager').length, 0);
    assert.equal(app.state[4], false);
  }
});

test('11: your Mac permission review never shows the internal spending ceiling', () => {
  for (const domain of ['Parts inventory', 'Garden planning']) {
    const app = harness(domain, null); app.saved.state = 'pending'; delete app.saved.claimed_at;
    app.saved.expires_at = '2099-01-01T00:00:00Z'; app.saved.manifest.credit_ceiling_micro = 7654321;
    const tree = JSON.stringify(app.render());
    assert.doesNotMatch(tree, /Maximum spending|7654321/);
    assert.match(tree, /I reviewed this task/);
  }
});

test('22: your Mac task capability and metadata reads start together without treating failed optional capabilities as approval', async () => {
  for (const domain of ['Stock planning', 'Cycling routes']) {
    let finish;
    const app = harness(domain, null, {deploy: {application_name: domain, target: 'vercel'}, apiReply: (url, _init, saved) => {
      if (url.endsWith('/capabilities')) return new Promise(resolve => {finish = resolve;});
      return {ok: true, data: saved};
    }});
    app.render(); app.effects[0].effect(); await new Promise(resolve => setImmediate(resolve));
    assert.ok(app.calls.some(call => call.url?.endsWith('/capabilities')));
    assert.ok(app.calls.some(call => call.url === '/api/ventures/venture-a/device-work-orders/order-a'));
    finish({ok: false}); await new Promise(resolve => setImmediate(resolve));
    assert.ok(app.calls.some(call => call.url?.endsWith('/deploy-receipt')));
    assert.equal(app.state[13], false);
    assert.ok(app.calls.every(call => !call.init));
    const source = ts.createSourceFile('page.tsx', fs.readFileSync(path.join(__dirname, 'page.tsx'), 'utf8'), ts.ScriptTarget.Latest, true);
    let reader;
    function find(node) {if (ts.isFunctionDeclaration(node) && node.name?.text === 'refreshProductReceipts') reader = node; ts.forEachChild(node, find);}
    find(source);
    const exports = {}, reads = [], values = {};
    vm.runInNewContext(ts.transpileModule(`exports.read = ${reader.getText(source)};`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText,
      {exports, path: `/api/${encodeURIComponent(domain)}`, alive: {current: true}, serial: {current: 9},
        verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, productBuildActorMatches: () => true,
        setLoopIterations: value => {values.iterations = value;}, setProductPlan: value => {values.plan = value;}, setDeployReceipt: value => {values.deploy = value;},
        api: url => {reads.push(url); return url.endsWith('/iterations') ? new Promise((_resolve, reject) => {finish = reject;})
          : Promise.resolve({ok: true, data: {plan: {name: domain}, receipt: {name: domain}}});}});
    const reading = exports.read({actor_id: 'founder', manifest: {coding_loop_verification: {phase: 'plan'}, deploy_verification: {target: 'vercel'}}}, 9);
    assert.equal(reads.length, 3); finish(new Error('optional read unavailable')); await reading;
    assert.equal(values.plan.name, domain); assert.equal(values.deploy.name, domain); assert.equal(values.iterations, undefined);
  }
});

test('12: your requested independent check uses its quote without a second approval', async () => {
  for (const domain of ['Parts inventory', 'Garden planning']) {
    const app = harness(domain, null);
    app.state[9] = {state: 'stored', sha256: 'a'.repeat(64)};
    app.state[10] = {status: 'requires_runtime_qa', runtime: 'static_landing_v1', findings: [], report_sha256: 'b'.repeat(64)};
    app.state[11] = {quote_id: 'quote', quote_digest: 'c'.repeat(64), qa_ceiling_micro: 1234567, expires_at: '2099-01-01T00:00:00Z'};
    const tree = app.render();
    assert.doesNotMatch(JSON.stringify(tree), /Maximum spending|1234567|Approve and run|Quote expires/);
    assert.ok(nodes(tree).some(node => node.props?.children === 'Run independent check'));
    const source = ts.createSourceFile('page.tsx', fs.readFileSync(path.join(__dirname, 'page.tsx'), 'utf8'), ts.ScriptTarget.Latest, true);
    let method;
    function find(node) { if (ts.isFunctionDeclaration(node) && node.name?.text === 'staticQaAction') method = node.getText(source); ts.forEachChild(node, find); }
    find(source);
    const calls = [], quote = app.state[11], context = {order: app.saved, inFlight: {current: false}, sourceReceipt: app.state[9], qaQuote: null,
      path: '/api/task', setBusy() {}, setError() {}, setQaQuote() {}, setQaRun() {}, apiFailure: () => 'Retry',
      api: async (url, init) => { calls.push({url, init}); return {ok: true, data: calls.length === 1 ? quote : {id: 'run', app_id: 'app'}}; }};
    vm.runInNewContext(ts.transpileModule(method + ';globalThis.run = staticQaAction;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    await context.run(false);
    assert.deepEqual(calls.map(call => call.url), ['/api/task/static-qa-quote', '/api/task/static-qa']);
    assert.deepEqual(JSON.parse(calls[1].init.body), {quote_id: quote.quote_id, quote_digest: quote.quote_digest, approve_qa_ceiling_micro: quote.qa_ceiling_micro});
  }
});

test('13: screenshot permission reflects your reviewed visual-review setting only', () => {
  for (const domain of ['Parts inventory', 'Garden planning']) for (const visual of [false, true]) {
    const app = harness(domain, null);
    app.saved.manifest.desktop_verification = {application_bundle: 'com.apple.TextEdit', window_id: 1,
      window_title: domain, reviewed_text: [], max_steps: 3, visual_review: visual};
    const tree = JSON.stringify(app.render());
    assert.doesNotMatch(tree, /Screenshots of the target app window are sent to OpenAI to decide each step/);
    assert.match(tree, visual ? /After an uncertain action or at a decision stop/ : /No screenshot is sent to OpenAI/);
  }
});

test('33: approval hands your keyboard focus to the next verified action after reloading the saved order', async () => {
  for (const domain of ['Retail inventory', 'Recipe planning']) {
    const app = harness(domain, null); app.saved.state = 'pending'; delete app.saved.claimed_at;
    app.saved.expires_at = '2099-01-01T00:00:00Z'; app.state[5] = true;
    const approve = nodes(app.render()).find(node => node.props?.['data-usage-control'] === 'mac_approve');
    assert.equal(approve.props.disabled, false); approve.props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(app.frames.length, 1); let focused = 0;
    nodes(app.render()).find(node => node.props?.id === 'review-mac-actions').props.ref.current = {
      scrollIntoView() {}, querySelector: () => ({focus: () => {focused++;}}),
    };
    app.frames[0](); assert.equal(focused, 1); assert.equal(app.saved.state, 'approved');
    assert.equal(app.calls.filter(call => call.url?.endsWith('/approve')).length, 1);
  }
});

test('34: you see ongoing task eligibility checks instead of unexplained disabled approval', () => {
  for (const domain of ['Retail inventory', 'Recipe planning']) {
    const app = harness(domain, null); app.saved.state = 'pending'; delete app.saved.claimed_at;
    app.saved.expires_at = '2099-01-01T00:00:00Z'; app.render();
    app.state[19] = true; app.state[4] = true;
    const tree = app.render(); assert.match(JSON.stringify(tree), /Checking task availability/);
    assert.equal(nodes(tree).find(node => node.props?.['data-usage-control'] === 'mac_approve').props.disabled, true);
    app.state[19] = false; app.state[4] = false;
    assert.doesNotMatch(JSON.stringify(app.render()), /Checking task availability/);
  }
});

test('you can explicitly clear an uncertain task without claiming success or starting another action', async () => {
  for (const title of ['Freight scheduling', 'Meal planning']) {
    for (const state of ['reconciliation_required', 'interrupted_reconciliation_required', 'stopped']) {
      const receipt = {state, task_verified: false, credits_reconciled: false};
      const app = harness(title, receipt, {reviewed: true}), tree = app.render();
      const buttons = nodes(tree).filter(node => node.type === 'button');
      const clear = buttons.filter(node => node.props.children === "I've checked; clear this task");
      assert.equal(clear.length, 1);
      assert.equal(clear[0].props.disabled, false);
      assert.equal(buttons.some(node => node.props.children === 'Looks right, mark done'), false);
      assert.match(JSON.stringify(tree), /check the saved result before requesting another task/);
      clear[0].props.onClick();
      await new Promise(resolve => setImmediate(resolve));
      const ack = app.calls.filter(call => call.url?.endsWith('/acknowledge'));
      assert.equal(ack.length, 1); assert.equal(ack[0].init.method, 'POST');
      assert.equal(JSON.parse(ack[0].init.body).review_digest, app.saved.review_digest);
      assert.equal(app.saved.reviewed_at, '2026-10-26T00:02:00Z');
      assert.equal(receipt.task_verified, false); assert.equal(receipt.credits_reconciled, false);
      assert.equal(app.calls.some(call => call.args?.action === 'start' || call.args?.action === 'reconcile'), false);
      assert.equal(nodes(harness(title, receipt, {busy: true}).render()).find(node =>
        node.props?.children === "I've checked; clear this task").props.disabled, true);
      assert.equal(nodes(harness(title, receipt, {desktop: false}).render()).some(node =>
        node.props?.children === "I've checked; clear this task"), false);
    }
    for (const receipt of [{state: 'acting', task_verified: false}, {state: 'needs_you', task_verified: false},
      {state: 'stopped', task_verified: false, credits_reconciled: true},
      {state: 'stopped', task_verified: false, credits_reconciled: false, reconciliation_reviewed_at: '2026-10-26T00:02:00Z'}]) {
      assert.equal(nodes(harness(title, receipt).render()).some(node =>
        node.props?.children === "I've checked; clear this task"), false);
    }
  }
});

const buildProps = app => nodes(app.render()).find(node => node.type === 'ProductBuildReview').props;
const loop = {profile: 'web_app_vercel_v1', phase: 'implement', provider: 'codex', workspace_slug: 'reviewed-app'};
const cleanReceipt = preview => ({order_id: 'order-a', state: 'stopped', task_verified: true, credits_reconciled: true,
  evidence: {product_build: {outcome: 'pass', source_commit: 'a'.repeat(40), file_manifest_sha256: 'b'.repeat(64)}},
  product_preview: preview});
const preview = {status: 'ready', ready: true, url: 'http://127.0.0.1:43127/', expires_at: Date.now() / 1000 + 600,
  source_commit: 'a'.repeat(40), file_manifest_sha256: 'b'.repeat(64), stop_available: true, transport: 'device_order_v1'};

test('your preview Start and Stop reuse installed order actions without cancelling Core or granting deploy', async () => {
  for (const title of ['Warehouse dispatch', 'Home fitness']) {
    const stopped = {...preview, status: 'stopped', ready: false, stop_available: false, url: undefined};
    const app = harness(title, cleanReceipt(stopped), {loop, nativeReply: args => cleanReceipt(args.action === 'cancel' ? stopped : preview)});
    const props = buildProps(app);
    assert.equal(props.preview.status, 'stopped');
    props.onRetryPreview();
    assert.equal(buildProps(app).onRetryPreview, undefined);
    await new Promise(resolve => setImmediate(resolve));
    const ready = buildProps(app);
    assert.equal(ready.preview.url, preview.url);
    ready.onStopPreview();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(buildProps(app).preview.status, 'stopped');
    assert.deepEqual(app.calls.map(call => [call.command, call.args.action]), [
      ['device_work_order_action', 'start'], ['device_work_order_action', 'cancel']]);
    assert.equal(app.calls.some(call => call.url), false);
    assert.equal(app.calls.some(call => /deploy|approve/.test(call.args.action)), false);
  }
});

test('you cannot restart a preview on an old bridge or an uncertain coding receipt', () => {
  for (const title of ['Warehouse dispatch', 'Home fitness']) {
    const stopped = {...preview, status: 'stopped', ready: false, stop_available: false};
    const unsupported = buildProps(harness(title, cleanReceipt({...preview, transport: undefined}), {loop}));
    assert.equal(unsupported.preview.status, 'unavailable');
    assert.equal(unsupported.preview.url, undefined);
    for (const changes of [{product_preview: {...stopped, transport: undefined}}, {task_verified: false},
      {credits_reconciled: false}, {journal_unconfirmed: true}, {actual_usage_unconfirmed: true},
      {effect_outcome_unconfirmed: true}, {order_id: 'another-order'}]) {
      const app = harness(title, {...cleanReceipt(stopped), ...changes}, {loop});
      assert.equal(buildProps(app).onRetryPreview, undefined);
      assert.equal(app.calls.length, 0);
    }
    for (const options of [{desktop: false}, {busy: true}]) {
      const props = buildProps(harness(title, cleanReceipt(preview), {loop, ...options}));
      assert.equal(props.onStopPreview, undefined);
      assert.equal(props.onRetryPreview, undefined);
      if (options.desktop === false) assert.equal(props.preview.url, undefined);
    }
  }
});

test('your preview URL is hidden on source drift while local Stop remains available', () => {
  for (const title of ['Warehouse dispatch', 'Home fitness']) {
    for (const field of ['source_commit', 'file_manifest_sha256']) {
      const app = harness(title, cleanReceipt({...preview, [field]: 'd'.repeat(preview[field].length)}), {loop});
      const props = buildProps(app);
      assert.equal(props.preview.status, 'unavailable');
      assert.equal(props.preview.ready, false); assert.equal(props.preview.url, undefined);
      assert.equal(typeof props.onStopPreview, 'function');
    }
  }
});

test('your source-bound native deployment preserves failed functional checks across Core refreshes', () => {
  for (const title of ['Warehouse dispatch', 'Home fitness']) {
    const deploy = {target: 'preview', source_commit: 'a'.repeat(40), file_manifest_sha256: 'b'.repeat(64)};
    const local = {...deploy, status: 'deployed', exit_status: 0, url: 'https://review.vercel.app',
      availability_status: 'available', functional_status: 'failed', http_journey: [{status: 'failed'}]};
    const core = {...deploy, status: 'deployed', exit_status: 0, url: local.url};
    const app = harness(title, {order_id: 'order-a', state: 'stopped', task_verified: true, deploy_receipt: local},
      {deploy, coreReceipt: core});
    assert.equal(buildProps(app).receipt, local);
    for (const changes of [{source_commit: 'd'.repeat(40)}, {file_manifest_sha256: 'd'.repeat(64)}, {target: 'production'}]) {
      assert.equal(buildProps(harness(title, {order_id: 'order-a', state: 'stopped', task_verified: true,
        deploy_receipt: {...local, ...changes}}, {deploy, coreReceipt: core})).receipt, core);
    }
    assert.equal(buildProps(harness(title, {order_id: 'other-order', state: 'stopped', task_verified: true,
      deploy_receipt: local}, {deploy, coreReceipt: core})).receipt, core);
  }
});

test('your preview status poll resumes after a local lifecycle action', async () => {
  for (const title of ['Warehouse dispatch', 'Home fitness']) {
    const app = harness(title, cleanReceipt({...preview, status: 'stopped', ready: false, stop_available: false}),
      {loop, nativeReply: () => cleanReceipt(preview)});
    buildProps(app).onRetryPreview();
    buildProps(app);
    assert.equal(app.effects[1].deps[2], true);
    assert.equal(app.effects[1].effect(), undefined);
    await new Promise(resolve => setImmediate(resolve));
    buildProps(app);
    assert.equal(app.effects[1].deps[2], false);
    const stop = app.effects[1].effect();
    await new Promise(resolve => setImmediate(resolve));
    stop();
    assert.equal(app.calls.some(call => call.command === 'device_work_order_action' && call.args.action === 'status'), true);
  }
});

test('you get an honest unconfirmed result when preview transport fails or returns another order', async () => {
  for (const title of ['Warehouse dispatch', 'Home fitness']) {
    for (const action of ['start', 'cancel']) {
      for (const wrongOrder of [true, false]) {
        const original = action === 'start' ? {...preview, status: 'stopped', ready: false, stop_available: false} : preview;
        const app = harness(title, cleanReceipt(original), {loop, nativeReply: () => {
          if (wrongOrder) return {...cleanReceipt(preview), order_id: 'other-order'};
          throw new Error('transport interrupted');
        }});
        const props = buildProps(app);
        (action === 'start' ? props.onRetryPreview : props.onStopPreview)();
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(buildProps(app).preview, original);
        assert.match(JSON.stringify(app.render()), new RegExp(`could not confirm ${action === 'start' ? 'Start' : 'Stop'}`));
        assert.equal(app.calls.length, 1);
        assert.equal(app.calls[0].command, 'device_work_order_action');
        assert.equal(app.calls[0].args.action, action);
      }
    }
  }
});

test('your preview becomes unavailable on a failed status poll without inventing Stop or losing its controls', async () => {
  for (const title of ['Warehouse dispatch', 'Home fitness']) {
    let failed = true;
    const app = harness(title, cleanReceipt(preview), {loop, nativeReply: () => {
      if (failed) throw new Error('status unavailable');
      return cleanReceipt(preview);
    }});
    buildProps(app);
    let stop = app.effects[1].effect();
    await new Promise(resolve => setImmediate(resolve));
    stop();
    const unavailable = buildProps(app);
    assert.equal(unavailable.preview.status, 'unavailable');
    assert.equal(unavailable.preview.ready, false); assert.equal(unavailable.preview.url, undefined);
    assert.equal(unavailable.preview.stop_available, true);
    assert.equal(typeof unavailable.onStopPreview, 'function');
    failed = false;
    stop = app.effects[1].effect();
    await new Promise(resolve => setImmediate(resolve));
    stop();
    assert.equal(buildProps(app).preview.ready, true);
  }
});
