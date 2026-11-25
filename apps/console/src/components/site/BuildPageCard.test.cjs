const chatRoutes = require("../../lib/chatRouteAction.ts");
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');
const creditCopy = require('../../lib/credits.ts');

test('full preview fills the available viewport without shrinking its scrollable frame', () => {
  const css = readFileSync(join(__dirname, 'BuildPageCard.module.css'), 'utf8');
  const opened = css.match(/\.fullPreview\[open\]\s*\{([^}]+)\}/)?.[1] || '';
  const frame = css.match(/\.fullPreview \.preview\s*\{([^}]+)\}/)?.[1] || '';
  for (const domain of ['courier dispatch', 'home gardening']) {
    assert.match(opened, /display:\s*flex/, domain);
    assert.match(opened, /flex-direction:\s*column/, domain);
    assert.match(frame, /flex:\s*1\s+1\s+0/, domain);
    assert.match(frame, /min-height:\s*0/, domain);
    assert.match(css, /height:\s*calc\(100dvh - 32px\)/, domain);
    assert.doesNotMatch(frame, /calc\(100%/, domain);
  }
});

function compile(path, require, context = {}) {
  const output = ts.transpileModule(readFileSync(path, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const exports = {};
  vm.runInNewContext(output, { exports, require, ...context }, { filename: path });
  return exports;
}

test('local build recovery opens the setting that can resolve the failure', () => {
  const { pageBuildFix } = harness({ local: true });
  for (const venture of ['ven_purchasing', 'ven_pets']) {
    assert.equal(pageBuildFix('pagebuilder_coding_tool_required', venture).href, '/settings/local#coding-tools');
    assert.equal(pageBuildFix('pagebuilder_provider_or_credits_unavailable', venture).href, '/settings/local#providers-title');
    assert.equal(pageBuildFix('pagebuilder_outcome_uncertain', venture,
      'Your page check did not finish. Open Settings > Keys and connections > Your AI provider.').href, '/settings/local#providers-title');
    assert.equal(pageBuildFix('pagebuilder_outcome_uncertain', venture, "Review your AI account's request history."), null);
    assert.equal(pageBuildFix('capability_unavailable', venture, 'In Settings > Capabilities, switch on Draft landing pages.').href, '/settings/local/capabilities#landing_pages-title');
    assert.equal(pageBuildFix('capability_unavailable', venture, 'In Settings > Keys and connections > Your AI provider, save your model.').href, '/settings/local#providers-title');
    assert.equal(pageBuildFix('pagebuilder_spend_limit', venture).href, '/settings/local#spend-title');
    assert.equal(pageBuildFix('pagebuilder_insufficient_credits', venture), null);
  }
});

function harness({ referenceCapable = true, localTools = null, nextStep = null, jobId, local = false, onSelection } = {}) {
  let owner = 'account-a', epoch = 1, cursor = 0, effects = [], tree, addressOverride = false;
  const hooks = [], requests = [], navigations = [], intervals = [], timeouts = new Map(), listeners = new Map(), nextStepReads = [], desktopCalls = [];
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const react = {
    useState(initial) {
      const i = cursor++;
      // The address field is stubbed: unless a test drives it, the saved address is already chosen.
      if (initial && initial.slug === null && 'current' in initial && !addressOverride) initial = {slug: 'nightshift', current: 'nightshift'};
      if (!hooks[i]) hooks[i] = { value: initial };
      return [hooks[i].value, value => { hooks[i].value = typeof value === 'function' ? value(hooks[i].value) : value; }];
    },
    useRef(initial) { const i = cursor++; return hooks[i] ||= { current: initial }; },
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
  const window = {
    location: { pathname: '/v/venture-a/site', assign: path => navigations.push(path) },
    history: { replaceState() { window.location.search = ''; } },
    setInterval: fn => intervals.push(fn), clearInterval() {},
    setTimeout(fn) { const id = timeouts.size + 1; timeouts.set(id, fn); return id; }, clearTimeout(id) { timeouts.delete(id); },
    addEventListener(type, fn) { (listeners.get(type) || listeners.set(type, new Set()).get(type)).add(fn); },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    dispatchEvent(event) { for (const callback of listeners.get(event.type) || []) callback(event); },
  };
  const helper = name => compile(join(__dirname, '../../lib/' + name + '.ts'), () => {});
  const jsx = (type, props) => ({ type, props });
  const { BuildPageCard: Component, pageBuildFix } = compile(join(__dirname, 'BuildPageCard.tsx'), id => {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    if (id === "@/lib/chatRouteAction") return chatRoutes;
    if (id === "@/components/settings/AccountSettings") return { AccountSettings: "account-settings" };

    if (id === 'react') return react;
    if (id === '@/components/layout/LocalModeBoundary') return { useLocalMode: () => local };
    if (id === '@/lib/localMode') return { isLocalConsole: () => local };
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id === 'next/navigation') return { useSearchParams: () => new URLSearchParams(window.location.search || '') };
    if (id === '@/lib/client') return {
      verifiedSessionSubject: () => owner, verifiedSessionEpoch: () => epoch,
      api: (path, init) => new Promise(resolve => requests.push({ path, init, resolve })),
    };
    if (id === '@/lib/desktop') return { isDesktopShell: () => true,
      desktopInvoke: async command => { desktopCalls.push(command); return command === 'device_work_order_capabilities'
        ? { pagebuilder_reference_v1: referenceCapable }
        : localTools ? localTools.promise : [{ name: 'codex', installed: true, cli_path: '/synthetic/codex' }]; } };
    if (id === '@/components/layout/NextStepStrip') return { NextStepButton: 'next-step-button',
      useNextStep: venture => { nextStepReads.push(venture); return venture ? nextStep : null; } };
    if (id === '@/lib/pageBuild') return helper('pageBuild');
    if (id === '@/lib/validationCopy') return helper('validationCopy');
    if (id === '@/lib/apiFailure') return { apiFailure: () => 'Could not confirm your idea. Review the current validation.' };
    if (id === '@/lib/credits') return creditCopy;
    if (id === '@/lib/godModeRequestScope') return helper('godModeRequestScope');
    if (id === '@/lib/runStatusPresentation') return {RUN_STATUS_REFRESH: 'supymem-run-status-refresh'};
    if (id === '@/lib/macTaskPrepareFailure') return { activeMacTaskReviewPath: () => null, macTaskPrepareFailure: () => 'Failed' };
    if (id === './ServerCandidateQa') return { ServerCandidateQa: 'server-qa' };
    if (id.endsWith('/OneCard')) return { OneCard: 'card' };
    if (id === './PageChoices') return { PageChoices: 'page-choices' };
    if (id === './SiteAddress') return { SiteAddress: 'site-address' };
    if (id.endsWith('/ErrorNotice')) return { ErrorNotice: 'error' };
    if (id.endsWith('.module.css')) return {default: {}};
    throw new Error(id);
  }, { window, AbortController, AbortSignal, Event,
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } } });
  const render = (venture = 'venture-a') => {
    cursor = 0; tree = Component({ ventureId: venture, jobId, onSelection });
    const pending = effects; effects = []; pending.forEach(fn => fn());
    return tree;
  };
  const flush = () => new Promise(setImmediate);
  return { requests, navigations, intervals, window, render, pageBuildFix, manualAddress() { addressOverride = true; }, flush, nextStepReads, desktopCalls, values: () => hooks.map(h => h.value),
    expireBuildRequest() { for (const callback of timeouts.values()) callback(); },
    refresh() { return hooks.find(h => h.fn).fn(); },
    switchOwner(value) { owner = value; epoch++; for (const fn of listeners.get('supymem-session-changed') || []) fn(); },
    unmount() { for (const hook of hooks) hook.cleanup?.(); },
    async ready() {
      render(); await flush();
      requests.find(r => r.path.endsWith('/device-work-orders')).resolve({ ok: true, data: { orders: [] } });
      requests.find(r => r.path.split('?')[0].endsWith('/pagebuilder/profile')).resolve({ ok: false, status: 404, data: { detail: { code: 'pagebuilder_disabled' } } });
      await flush();
      render().props.actions.props.onClick();
      return requests.find(r => r.init?.method === 'POST');
    },
    async candidateReady(profile, html = '<h1>Candidate</h1>') {
      render(); await flush();
      requests.find(r => r.path.endsWith('/device-work-orders'))?.resolve({ ok: true, data: { orders: [] } });
      requests.find(r => r.path.split('?')[0].endsWith('/pagebuilder/profile')).resolve({ ok: true, data: profile });
      await flush();
      const preview = requests.find(r => r.path.includes('/preview-document'));
      if (preview) { preview.resolve({ ok: true, data: { html } }); await flush(); }
      return render();
    },
  };
}

for (const retire of ['venture', 'account', 'navigation', 'unmount']) {
  test(`retired ${retire} cannot redirect from a delayed build response`, async () => {
    const h = harness(), request = await h.ready();
    assert.ok(request);
    if (retire === 'venture') { h.window.location.pathname = '/v/venture-b/site'; h.render('venture-b'); }
    if (retire === 'account') { h.switchOwner('account-b'); h.switchOwner('account-a'); }
    if (retire === 'navigation') h.window.location.pathname = '/connections';
    if (retire === 'unmount') h.unmount();
    request.resolve({ ok: true, data: { review_path: '/v/venture-a/device-work-orders/order-a' } });
    await h.flush();
    assert.deepEqual(h.navigations, []);
  });
}

test('current reviewed build navigates to its real order', async () => {
  const h = harness(), request = await h.ready();
  request.resolve({ ok: true, data: { review_path: '/v/venture-a/device-work-orders/order-a' } });
  await h.flush();
  assert.deepEqual(h.navigations, ['/v/venture-a/device-work-orders/order-a']);
});

test('retired account cannot display a delayed preparation error', async () => {
  const h = harness(), request = await h.ready();
  h.switchOwner('account-b');
  request.resolve({ ok: false, data: {} }); await h.flush();
  assert.ok(!h.values().includes('Failed'));
  assert.deepEqual(h.navigations, []);
});

test('earlier venture order fetch cannot replace the new venture card', async () => {
  const h = harness(); h.render(); await h.flush();
  const earlier = h.requests[0];
  h.window.location.pathname = '/v/venture-b/site'; h.render('venture-b'); await h.flush();
  earlier.resolve({ ok: true, data: { orders: [{ id: 'order-a', title: 'Build approved task with Codex',
    state: 'pending', review_path: '/v/venture-a/device-work-orders/order-a' }] } });
  await h.flush();
  assert.notEqual(h.render('venture-b').props.title, 'Approve the build');
});

const profile = current => ({ task_id: 'landing-a', plan_id: 'plan-a', artifact_profile: 'static_landing_v1',
  quote: current ? null : { ready: true, credits: 18, quote_fingerprint: 'a'.repeat(64), payer: 'platform_credits' }, current });

const control = (card, name) => descendants(card.props.actions)
  .find(child => child?.props?.['data-usage-control'] === name);

const descendants = node => node && typeof node === 'object'
  ? [node, ...[node.props?.children].flat(Infinity).flatMap(descendants)] : [];
const frame = card => descendants(card).find(child => child.type === 'iframe');

test('local ready pages show preview, ZIP and one More menu across domains', async () => {
  for (const title of ['Inventory planning', 'Weekend cycling']) {
    const h = harness({local: true, jobId: 'candidate-a'});
    const card = await h.candidateReady(serverProfile(), `<h1>${title}</h1>`);
    const all = descendants(card.props.actions);
    const top = all.filter(node => ['button', 'a'].includes(node.type) && !all.some(parent => parent.type === 'details' && descendants(parent).includes(node)));
    assert.deepEqual(top.map(node => node.props.children), ['Open preview', 'Export']);
    assert.equal(top[0].props.className, 'primary');
    assert.equal(all.filter(node => node.type === 'summary' && node.props.children === 'More').length, 1);
    assert.match(card.props.body, /signups/);
  }
});

for (const domain of ['Supplier catalog', 'Piano practice']) {
  test(`your running tool page opens the shared watch screen for ${domain}`, async () => {
    const state = harness({ local: true });
    const watch = `/v/${encodeURIComponent(domain)}/device-work-orders/dwo_page`;
    const card = await state.candidateReady(profile({ job_id: 'candidate-a', state: 'running', stage: 'building',
      progress: 0.4, watch_url: watch, native_ready: false, candidates: [] }));
    const action = descendants(card.props.actions).find(node => node.type === 'a' && node.props.href === watch);
    assert.ok(action);
    assert.match(action.props.children, /Watch.*build/);
  });

  test(`your local page builder reads its profile without native polling for ${domain}`, async () => {
    const state = harness({ local: true });
    const card = await state.candidateReady(profile(null));
    assert.equal(card.props.title, 'Build your landing page');
    assert.deepEqual(state.desktopCalls, []);
    assert.ok(state.requests.every(request => !request.path.includes('/device-work-orders') && request.init?.method !== 'POST'));
  });

  test(`your local page builder shows the saved-setting next step for ${domain}`, async () => {
    const state = harness({ local: true });
    state.render(); await state.flush();
    const message = 'In Settings > Keys and connections > Your AI provider, save your API key.';
    state.requests.find(request => request.path.split('?')[0].endsWith('/pagebuilder/profile')).resolve({ ok: false, status: 409,
      data: { detail: { code: 'capability_unavailable', message } } });
    await state.flush();
    const card = state.render();
    assert.equal(card.props.body, message);
    assert.equal(card.props.actions.props.href, '/settings/local#providers-title');
    const changed = harness({ local: true });
    const ready = await changed.candidateReady(profile(null));
    const pending = ready.props.actions.props.onBuild();
    await changed.flush();
    changed.requests.find(request => request.path.endsWith('/pagebuilder/build')).resolve({ ok: false, status: 409,
      data: { detail: { code: 'capability_unavailable', message } } });
    await pending;
    await changed.flush();
    assert.ok(changed.values().includes(message));
    assert.equal(changed.render().props.actions.props.href, '/settings/local#providers-title');
  });
}

for (const [domain, title] of [['b2b', 'Supplier catalog'], ['consumer', 'Piano practice']]) {
  test(`your ${domain} page exports HTML without a coding-tool connection`, async () => {
    const state = harness({referenceCapable: false, localTools: []});
    const note = 'For generated photos, save your OpenAI API key in Keys and connections > Model provider.';
    const saved = {...profile({job_id: `page-${domain}`, state: 'ready', native_ready: false, candidates: [], image_note: note}),
      task_id: null, plan_id: null};
    const card = await state.candidateReady(saved, `<h1>${title}</h1>`);
    assert.equal(frame(card).props.srcDoc, `<h1>${title}</h1>`);
    const download = control(card, 'website_export_html');
    assert.equal(download.props.href, `/api/ventures/venture-a/pagebuilder/jobs/page-${domain}/source?variant=0`);
    assert.equal(download.props.download, true);
    assert.equal(control(card, 'website_build_candidate'), undefined);
    assert.ok(descendants(card).some(node => node.props?.children === note));
  });
}

for (const [domain, title] of [['b2b', 'Supplier catalog'], ['consumer', 'Piano practice']]) {
  test(`both ${domain} preview frames let your consent form open without enabling page code`, async () => {
    const html = `<h1>${title}</h1><a href="about:srcdoc#waitlist">Explore early access</a>`
      + '<section id="waitlist"><a href="https://gateway.supymem.com/public/waitlists/venture-a" target="_blank" rel="noopener noreferrer">Join the waitlist</a></section>';
    const h = harness(), card = await h.candidateReady(serverProfile(), html);
    const frames = descendants(card).filter(node => node.type === 'iframe');
    assert.equal(frames.length, 2);
    for (const preview of frames) {
      assert.equal(preview.props.srcDoc, html);
      assert.deepEqual(preview.props.sandbox.split(' ').sort(), ['allow-popups', 'allow-popups-to-escape-sandbox']);
      assert.doesNotMatch(preview.props.sandbox, /allow-scripts|allow-same-origin|allow-forms|allow-top-navigation/);
    }
  });
}

test('Website uses natural-height cards with Publish above its contained preview', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile());
  const OneCard = compile(join(__dirname, '../primitives/OneCard.tsx'), id => {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    if (id === "@/lib/chatRouteAction") return chatRoutes;
    if (id === "@/components/settings/AccountSettings") return { AccountSettings: "account-settings" };

    if (id === 'react') return { useState: () => [false, () => {}] };
    if (id === 'react/jsx-runtime') return { jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props}) };
    if (id === '@/lib/detailLevel') return { useDetailLevel: () => ['brief'] };
    if (id === '@/lib/cx') return { cx: (...classes) => classes.filter(Boolean).join(' ') };
    if (id.endsWith('.module.css')) return {default: {}};
    throw new Error(id);
  }).OneCard;
  const nodes = descendants(OneCard(card.props));
  assert.ok(nodes.indexOf(control(card, 'website_publish_review')) < nodes.findIndex(node => node.type === 'iframe'));
  assert.equal(frame(card).props.sandbox, 'allow-popups allow-popups-to-escape-sandbox');
  const css = readFileSync(join(__dirname, 'BuildPageCard.module.css'), 'utf8');
  assert.match(css, /height:\s*min\(60dvh,\s*640px\)/);
  assert.match(css, /overscroll-behavior:\s*contain/);
  const stackCss = readFileSync(join(__dirname, '../primitives/OneCard.module.css'), 'utf8');
  // No card may become a full-screen slide: content-sized cards, no viewport heights, no scroll snapping.
  assert.ok(!/dvh|vh\b|scroll-snap|overflow-y:\s*auto/.test(stackCss), 'cards must size to their content');
  assert.match(stackCss, /\.card\s*\{[^}]*place-items:\s*start center/s);
  assert.ok(!/\.working\s*\{/.test(stackCss), 'working card status must not override the shared card grid');
  assert.match(stackCss, /\.workingIndicator\s*\{[^}]*display:\s*flex/s);
  assert.match(readFileSync(join(__dirname, '../../app/v/[ventureId]/site/page.tsx'), 'utf8'), /<OneCardStack\s+flow>/);
});

test('unchanged status refreshes fetch the preview once and keep its document visible', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile());
  for (let refresh = 0; refresh < 2; refresh++) {
    void h.refresh(); await h.flush();
    h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
    h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true, data: serverProfile()});
    await h.flush();
    assert.equal(h.requests.filter(request => request.path.includes('/preview-document')).length, 1);
    assert.equal(frame(h.render()).props.srcDoc, frame(card).props.srcDoc);
  }
});

test('overlapping status refreshes share the in-flight preview request', async () => {
  const h = harness(); h.render(); await h.flush();
  h.requests[0].resolve({ok: true, data: {orders: []}});
  h.requests[1].resolve({ok: true, data: serverProfile()}); await h.flush();
  void h.refresh(); await h.flush();
  h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
  h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true, data: serverProfile()});
  await h.flush();
  const previews = h.requests.filter(request => request.path.includes('/preview-document'));
  assert.equal(previews.length, 1);
  previews[0].resolve({ok: true, data: {html: '<h1>Shared preview</h1>'}}); await h.flush();
  assert.equal(frame(h.render()).props.srcDoc, '<h1>Shared preview</h1>');
});

test('a replacement variant waits for its own preview before Publish is enabled', async () => {
  const h = harness(); await h.candidateReady(serverProfile());
  void h.refresh(); await h.flush();
  h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
  h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true,
    data: serverProfile({server_candidates: [{variant: 1}]})}); await h.flush();
  const waiting = h.render();
  assert.equal(frame(waiting), undefined);
  assert.equal(control(waiting, 'website_publish_review').props.disabled, true);
  const previews = h.requests.filter(request => request.path.includes('/preview-document'));
  assert.equal(previews.length, 2);
  assert.ok(previews[1].path.endsWith('?variant=1'));
  previews[1].resolve({ok: true, data: {html: '<h1>Variant 1</h1>'}}); await h.flush();
  assert.equal(frame(h.render()).props.srcDoc, '<h1>Variant 1</h1>');
  assert.equal(control(h.render(), 'website_publish_review').props.disabled, false);
});

test('full preview opens a native dialog and keeps the inline preview intact', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile());
  const dialog = descendants(card).find(node => node.type === 'dialog');
  assert.ok(dialog);
  let opened = 0;
  dialog.props.ref.current = {showModal: () => opened++};
  control(card, 'website_open_full_preview').props.onClick();
  assert.equal(opened, 1);
  assert.equal(frame(dialog).props.srcDoc, frame(card).props.srcDoc);
  assert.equal(frame(dialog).props.sandbox, 'allow-popups allow-popups-to-escape-sandbox');
  assert.ok(descendants(dialog).some(node => node.type === 'form' && node.props.method === 'dialog'));
});

test('warming previews still let the founder queue exactly one build', async () => {
  const h = harness();
  const card = await h.candidateReady({ ...profile(null), renderer_ready: false });
  assert.match(card.props.body, /warming up/);
  card.props.actions.props.onBuild();
  card.props.actions.props.onBuild();
  assert.equal(h.requests.filter(r => r.init?.method === 'POST').length, 1);
});

test('one click builds the page with no cost review and no native order', async () => {
  const h = harness(), card = await h.candidateReady(profile(null));
  assert.equal(card.props.title, 'Build your landing page');
  assert.equal(card.props.actions.type, 'page-choices');
  assert.equal(card.props.actions.props.ventureId, 'venture-a');
  assert.ok(!/credit|cost|estimate/i.test(card.props.body));
  card.props.actions.props.onBuild();
  card.props.actions.props.onBuild();  // a double click shares the one request
  const posts = h.requests.filter(r => r.init?.method === 'POST');
  assert.equal(posts.length, 1);
  assert.ok(posts[0].path.endsWith('/pagebuilder/build'));
  assert.equal(h.requests.filter(r => r.path.endsWith('/from-chat')).length, 0);
  posts[0].resolve({ ok: false, status: 402, data: { detail: { code: 'pagebuilder_insufficient_credits' } } });
  await h.flush();
  assert.ok(h.values().some(value => value === creditCopy.USAGE_LIMIT_MESSAGE));
  assert.deepEqual(h.navigations, []);
});

test('a running build shows its real stage and progress', async () => {
  const h = harness(), card = await h.candidateReady(profile({ job_id: 'candidate-a', state: 'running', stage: 'variant_0:generation',
    progress: 0.4, queue_state: 'running', native_ready: false, candidates: [] }));
  assert.equal(card.props.title, 'Building your landing page');
  assert.ok(card.props.body.includes('40%') && card.props.body.toLowerCase().includes('generation'));
  assert.ok(h.intervals.length > 0);
});

test('a failed build explains why and offers one-click build again', async () => {
  const h = harness(), card = await h.candidateReady(profile({ job_id: 'candidate-a', state: 'failed', error: 'pagebuilder_renderer_unavailable',
    queue_state: 'failed_dead', native_ready: false, candidates: [] }));
  assert.equal(card.props.title, 'The landing page build stopped');
  assert.equal(card.props.step, 'Landing page');
  assert.ok(card.props.body.includes('preview could not start'));
  assert.ok(descendants(card.props.actions).some(node => node.type === 'button' && node.props.children === 'Refresh status'));
  control(card, 'website_build_retry').props.onClick();
  assert.ok(h.requests.find(r => r.init?.method === 'POST').path.endsWith('/pagebuilder/build'));
});

test('failed-candidate retry requests a fresh job, paints queued without GET, and ignores stale profile replies', async () => {
  const h = harness(), failedJob = {job_id: 'candidate-a', state: 'failed', error: 'pagebuilder_candidate_invalid', native_ready: false, candidates: []};
  const card = await h.candidateReady(profile(failedJob));
  const changes = [], statuses = [];
  h.window.addEventListener('supymem-venture-changed', event => changes.push(event.detail));
  h.window.addEventListener('supymem-run-status-refresh', event => statuses.push(event.type));
  const staleRefresh = h.refresh();
  control(card, 'website_build_retry').props.onClick();
  assert.equal(control(h.render(), 'website_build_retry').props.children, 'Starting…');
  const post = h.requests.find(request => request.init?.method === 'POST');
  assert.deepEqual(JSON.parse(post.init.body), {new_version_of: 'candidate-a'});
  post.resolve({ok: true, data: {job_id: 'candidate-b', state: 'queued', stage: 'queued', progress: 0}});
  await h.flush();
  assert.equal(changes.length, 1);
  assert.equal(changes[0].ventureId, 'venture-a');
  assert.equal(changes[0].actorId, 'account-a');
  assert.equal(changes[0].epoch, 1);
  assert.deepEqual(statuses, ['supymem-run-status-refresh']);
  assert.equal(h.render().props.title, 'Building your landing page');
  assert.match(h.render().props.body, /Queued.*0%/);
  assert.ok(h.intervals.length);
  const reads = h.requests.filter(request => !request.init?.method);
  reads.filter(request => request.path.endsWith('/device-work-orders')).slice(1).forEach(request => request.resolve({ok: true, data: {orders: []}}));
  reads.filter(request => request.path.endsWith('/pagebuilder/profile')).slice(1).forEach(request => request.resolve({ok: true, data: profile(failedJob)}));
  await staleRefresh; await h.flush();
  assert.equal(h.render().props.title, 'Building your landing page');
});

test('a verified free same-job requeue visibly starts and its later result still refreshes', async () => {
  const h = harness(), card = await h.candidateReady(profile({job_id: 'candidate-a', state: 'failed',
    error: 'pagebuilder_renderer_unavailable', native_ready: false, candidates: []}));
  control(card, 'website_build_retry').props.onClick();
  h.requests.filter(request => request.init?.method === 'POST').at(-1).resolve({ok: true,
    data: {job_id: 'candidate-a', state: 'queued', stage: 'queued', progress: 0, requeued: true}});
  await h.flush();
  assert.equal(h.render().props.title, 'Building your landing page');
  assert.match(h.render().props.body, /Queued.*0%/);
  h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
  h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true,
    data: profile({job_id: 'candidate-a', state: 'failed', error: 'pagebuilder_plan_rejected', native_ready: false, candidates: []})});
  await h.flush();
  assert.equal(h.render().props.title, 'The landing page build stopped');
  assert.match(h.render().props.body, /usable landing page plan/);
});

for (const result of [
  {ok: false, status: 409, data: {detail: {code: 'pagebuilder_build_limit'}}},
  {ok: false, status: 409, data: {detail: {code: 'pagebuilder_attempt_limit'}}},
  {ok: false, status: 409, data: {detail: {code: 'pagebuilder_new_version_unavailable'}}},
  {ok: true, data: {job_id: 'candidate-a', state: 'failed', error: 'pagebuilder_candidate_invalid'}},
  {ok: true, data: {job_id: 'candidate-a', state: 'ready'}},
  {ok: true, data: null},
]) test('retry response either confirms a fresh job or displays recovery guidance: ' + JSON.stringify(result), async () => {
  const h = harness(), card = await h.candidateReady(profile({job_id: 'candidate-a', state: 'failed', error: 'pagebuilder_generation_failed', native_ready: false, candidates: []}));
  for (let attempt = 0; attempt < 3; attempt++) {
    control(h.render(), 'website_build_retry').props.onClick();
    h.requests.filter(request => request.init?.method === 'POST').at(-1).resolve(result);
    await h.flush();
    const stopped = h.render();
    assert.equal(stopped.props.title, 'The landing page build stopped');
    assert.match(stopped.props.body, /ready landing page versions|safety limit|current build requirements|No fresh landing page build|could not start.*Refresh/);
    assert.equal(control(stopped, 'website_build_retry').props.disabled, false);
    assert.ok(control(stopped, 'website_find_customers'));
  }
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 3);
});

test('the hard attempt cap offers Find customers without blaming the idea or promising a reset', async () => {
  const h = harness(), card = await h.candidateReady(profile({job_id: 'candidate-a', state: 'failed', native_ready: false, candidates: []}));
  control(card, 'website_build_retry').props.onClick();
  h.requests.filter(request => request.init?.method === 'POST').at(-1).resolve({ok: false, status: 409, data: {detail: {code: 'pagebuilder_attempt_limit'}}});
  await h.flush();
  const stopped = h.render();
  assert.match(stopped.props.body, /eight-attempt safety limit.*Continue to Find customers.*no further build/);
  assert.doesNotMatch(stopped.props.body, /tomorrow|support|update your idea/i);
  assert.equal(control(stopped, 'website_find_customers').props.href, '/gtm?venture=venture-a');
});

for (const retire of ['venture', 'account', 'navigation', 'unmount']) {
  test(`retired ${retire} cannot paint a delayed server retry or clear a new scope's request`, async () => {
    const h = harness(), card = await h.candidateReady(profile({job_id: 'candidate-a', state: 'failed', native_ready: false, candidates: []}));
    control(card, 'website_build_retry').props.onClick();
    const old = h.requests.filter(request => request.init?.method === 'POST').at(-1);
    if (retire === 'venture') { h.window.location.pathname = '/v/venture-b/site'; h.render('venture-b'); }
    if (retire === 'account') h.switchOwner('account-b');
    if (retire === 'navigation') h.window.location.pathname = '/connections';
    if (retire === 'unmount') h.unmount();
    old.resolve({ok: true, data: {job_id: 'retired-private-job', state: 'queued'}});
    await h.flush();
    assert.ok(!JSON.stringify(h.values()).includes('retired-private-job'));
  });
}

test('a stalled retry times out visibly and a late response cannot replace a newer attempt', async () => {
  const h = harness(), card = await h.candidateReady(profile({job_id: 'candidate-a', state: 'failed', native_ready: false, candidates: []}));
  control(card, 'website_build_retry').props.onClick();
  const old = h.requests.filter(request => request.init?.method === 'POST').at(-1);
  control(card, 'website_build_retry').props.onClick();
  assert.match(h.render().props.body, /already running/);
  h.expireBuildRequest(); await h.flush();
  assert.match(h.render().props.body, /could not be confirmed.*Refresh/);
  assert.equal(control(h.render(), 'website_build_retry').props.disabled, false);
  control(h.render(), 'website_build_retry').props.onClick();
  old.resolve({ok: true, data: {job_id: 'retired-job', state: 'queued'}});
  await h.flush();
  assert.equal(control(h.render(), 'website_build_retry').props.children, 'Starting…');
  h.requests.filter(request => request.init?.method === 'POST').at(-1).resolve({ok: false, status: 503, data: null});
  await h.flush();
  assert.equal(control(h.render(), 'website_build_retry').props.disabled, false);
});

test('a thrown retry request releases the click guard and displays a next action', async () => {
  const h = harness(), card = await h.candidateReady(profile({job_id: 'candidate-a', state: 'failed', native_ready: false, candidates: []}));
  control(card, 'website_build_retry').props.onClick();
  h.requests.filter(request => request.init?.method === 'POST').at(-1).resolve(Promise.reject(new Error('private provider body')));
  await h.flush();
  assert.match(h.render().props.body, /could not be confirmed.*Refresh/);
  assert.doesNotMatch(h.render().props.body, /private provider body/);
  control(h.render(), 'website_build_retry').props.onClick();
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 2);
  h.requests.filter(request => request.init?.method === 'POST').at(-1).resolve({ok: false, data: null});
  await h.flush();
});

test('a usable draft prepares only its saved candidate and approved landing task', async () => {
  const h = harness(), card = await h.candidateReady(profile({ job_id: 'candidate-a', state: 'ready',
    queue_state: 'succeeded', native_ready: true, candidates: [{ variant: 1 }] }));
  const previewFrame = frame(card);
  assert.equal(previewFrame.props.sandbox, 'allow-popups allow-popups-to-escape-sandbox');
  assert.equal(previewFrame.props.srcDoc, '<h1>Candidate</h1>');
  control(card, 'website_build_candidate').props.onClick();
  const request = h.requests.find(r => r.init?.method === 'POST');
  const body = JSON.parse(request.init.body);
  assert.deepEqual(body.pagebuilder_candidate, { job_id: 'candidate-a', variant: 1 });
  assert.ok(body.instruction.includes('landing page') && body.instruction.includes('landing-a'));
  assert.equal(Object.keys(body.pagebuilder_candidate).length, 2);
  h.switchOwner('account-b'); h.switchOwner('account-a');
  request.resolve({ ok: true, data: { review_path: '/v/venture-a/device-work-orders/order-a' } });
  await h.flush();
  assert.deepEqual(h.navigations, []);
});

test('generated source awaiting durable custody shows the preview but cannot offer a reviewed build or publish', async () => {
  const h = harness(), card = await h.candidateReady(profile({ job_id: 'candidate-a', state: 'ready',
    queue_state: 'retry_wait', native_ready: false, candidates: [] }));
  assert.equal(card.props.title, 'Your landing page is ready');
  assert.ok(!h.requests.some(r => r.path.endsWith('/from-chat')));
  assert.ok(!control(card, 'website_build_candidate') && !control(card, 'website_publish_review'));
  assert.ok(control(card, 'website_find_customers'));
});

test('an older native wire still builds on the server but never prepares a native order', async () => {
  const h = harness({ referenceCapable: false }), card = await h.candidateReady(profile(null));
  assert.equal(card.props.title, 'Build your landing page');
  assert.equal(card.props.actions.type, 'page-choices');
  assert.equal(card.props.actions.props.ventureId, 'venture-a');
  assert.ok(!h.requests.some(r => r.init?.method === 'POST'));
});

test('same-venture delayed preview cannot replace the newer candidate binding', async () => {
  const h = harness(); h.render(); await h.flush();
  h.requests[0].resolve({ ok: true, data: { orders: [] } });
  h.requests[1].resolve({ ok: true, data: profile({ job_id: 'candidate-a', state: 'ready',
    queue_state: 'succeeded', native_ready: true, candidates: [{ variant: 0 }] }) });
  await h.flush();
  const earlier = h.requests.find(r => r.path.includes('candidate-a/preview-document'));
  h.render(); void h.refresh(); await h.flush();
  h.requests.filter(r => r.path.endsWith('/device-work-orders')).at(-1).resolve({ ok: true, data: { orders: [] } });
  h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ ok: true,
    data: { ...profile({ job_id: 'candidate-b', state: 'ready', queue_state: 'succeeded',
      native_ready: true, candidates: [{ variant: 1 }] }), task_id: 'landing-b' } });
  await h.flush();
  assert.equal(control(h.render(), 'website_build_candidate'), undefined);
  h.requests.find(r => r.path.includes('candidate-b/preview-document')).resolve({ ok: true, data: { html: '<h1>Candidate B</h1>' } });
  await h.flush();
  earlier.resolve({ ok: true, data: { html: '<h1>Candidate A</h1>' } });
  await h.flush();
  const card = h.render(), previewFrame = frame(card);
  assert.equal(previewFrame.props.srcDoc, '<h1>Candidate B</h1>');
  control(card, 'website_build_candidate').props.onClick();
  const request = h.requests.find(r => r.init?.method === 'POST'), body = JSON.parse(request.init.body);
  assert.deepEqual(body.pagebuilder_candidate, { job_id: 'candidate-b', variant: 1 });
  assert.ok(body.instruction.includes('landing-b'));
});

test('server rejects an outdated signed runner without dropping the selected candidate', async () => {
  const h = harness(), card = await h.candidateReady(profile({ job_id: 'candidate-a', state: 'ready',
    queue_state: 'succeeded', native_ready: true, candidates: [{ variant: 0 }] }));
  control(card, 'website_build_candidate').props.onClick();
  const request = h.requests.find(r => r.init?.method === 'POST');
  request.resolve({ ok: false, status: 409, data: { detail: { code: 'pagebuilder_runner_upgrade_required' } } });
  await h.flush();
  assert.ok(h.values().some(value => typeof value === 'string' && value.includes('Update the Supymem Mac app')));
  assert.equal(h.requests.filter(r => r.init?.method === 'POST').length, 1);
  assert.deepEqual(h.navigations, []);
});

test('exhausted storage needs an explicit same-job retry and never restarts generation', async () => {
  const h = harness(), card = await h.candidateReady(profile({ job_id: 'candidate-a', state: 'ready',
    queue_state: 'failed_dead', queue_attempt: 3, native_ready: false, candidates: [] }));
  assert.equal(card.props.title, 'Your landing page is ready');
  assert.equal(h.requests.filter(r => r.init?.method === 'POST').length, 0);
  const retry = control(card, 'website_retry_source');
  retry.props.onClick();
  const request = h.requests.find(r => r.init?.method === 'POST');
  assert.ok(request.path.endsWith('/jobs/candidate-a/retry-source'));
  assert.deepEqual(JSON.parse(request.init.body), { expected_attempt: 3 });
  assert.ok(!h.requests.some(r => r.path.endsWith('/pagebuilder/start') || r.path.endsWith('/from-chat')));
  request.resolve({ ok: false, status: 409, data: { detail: { code: 'pagebuilder_inputs_changed' } } });
  await h.flush();
  assert.equal(h.requests.filter(r => r.init?.method === 'POST').length, 1);
  assert.deepEqual(h.navigations, []);
});

test('another member gets the original creator handoff without paid or coding actions', async () => {
  const h = harness(), card = await h.candidateReady(profile({ job_id: 'candidate-a', state: 'ready',
    queue_state: 'leased', original_creator_required: true, native_ready: false, candidates: [] }));
  assert.equal(card.props.title, 'The draft creator needs to continue');
  assert.ok(card.props.body.includes('created this draft') && card.props.body.includes('their account'));
  assert.equal(card.props.actions.props.children[0].props.href, '/v/venture-a/plan');
  assert.equal(card.props.actions.props.children[1].props.children, 'Refresh status');
  assert.ok(!h.requests.some(r => r.path.includes('/preview-document') || r.init?.method === 'POST'));
  assert.equal(h.intervals.length, 0);
});

test('arriving from "Build my landing page" starts the build once without another click', async () => {
  const h = harness();
  const changes = [];
  h.window.addEventListener('supymem-venture-changed', event => changes.push(event.detail));
  h.window.location.search = '?build=1';
  await h.candidateReady(profile(null));
  const posts = h.requests.filter(r => r.init?.method === 'POST');
  assert.equal(h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).length, 1);
  assert.equal(h.requests.filter(r => r.path.endsWith('/device-work-orders')).length, 1);
  assert.equal(posts.length, 1);
  assert.ok(posts[0].path.endsWith('/pagebuilder/build'));
  posts[0].resolve({ ok: true, data: { job_id: 'candidate-a', state: 'queued' } });
  await h.flush();
  assert.equal(changes.length, 1);
  assert.equal(changes[0].ventureId, 'venture-a');
  assert.equal(changes[0].actorId, 'account-a');
  assert.equal(changes[0].epoch, 1);
  h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ ok: true, data: profile(null) });
  h.requests.filter(r => r.path.endsWith('/device-work-orders')).at(-1).resolve({ ok: true, data: { orders: [] } });
  await h.flush();
  assert.equal(h.requests.filter(r => r.init?.method === 'POST').length, 1);
});

test('a fresh build query while Website stays mounted refreshes once and starts one build per intent', async () => {
  const h = harness(); await h.candidateReady(profile(null));
  for (let attempt = 1; attempt <= 2; attempt++) {
    h.window.location.search = '?build=1';
    h.render(); h.render(); await h.flush();
    assert.equal(h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).length, attempt + 1);
    h.requests.filter(r => r.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
    h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true, data: profile(null)});
    await h.flush();
    const posts = h.requests.filter(r => r.init?.method === 'POST');
    assert.equal(posts.length, attempt);
    assert.ok(posts.at(-1).path.endsWith('/pagebuilder/build'));
    assert.equal(h.window.location.search, '');
    posts.at(-1).resolve({ok: false, status: 503, data: {}}); await h.flush();
    h.render(); h.render(); await h.flush();
    assert.equal(h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).length, attempt + 1);
  }
});

test('a mounted new-version query keeps the ready preview while requesting fresh source once', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile());
  h.window.location.search = '?build=1'; h.render(); await h.flush();
  assert.equal(h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).length, 2);
  assert.equal(frame(h.render()).props.srcDoc, frame(card).props.srcDoc);
  h.requests.filter(r => r.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
  h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true, data: serverProfile()});
  await h.flush();
  assert.equal(frame(h.render()).props.srcDoc, frame(card).props.srcDoc);
  assert.equal(h.requests.filter(r => r.path.includes('/preview-document')).length, 1);
  const posts = h.requests.filter(r => r.init?.method === 'POST');
  assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0].init.body), {new_version_of: 'candidate-a'});
});

async function refreshedProfile(h, current) {
  const pending = h.refresh(); await h.flush();
  h.requests.filter(r => r.path.endsWith('/device-work-orders')).at(-1).resolve({ ok: true, data: { orders: [] } });
  h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ ok: true, data: profile(current) });
  await h.flush();
  const preview = h.requests.filter(r => r.path.includes('/preview-document')).at(-1);
  if (current.state === 'ready' && preview) preview.resolve({ ok: true, data: { html: '<h1>Ready</h1>' } });
  await pending; return h.render();
}
const failed = (id, count = 1) => ({ job_id: id, consecutive_failures: count, state: 'failed', error: 'pagebuilder_renderer_unavailable', queue_state: 'failed_dead', native_ready: false, candidates: [] });
test('second consecutive failed build prioritizes finding customers using the durable failure count', async () => {
  const h = harness();
  const first = await h.candidateReady(profile(failed('build-a')));
  assert.equal(control(first, 'website_build_retry').props.className, 'primary');
  assert.equal(control(first, 'website_find_customers').props.className, 'secondary');
  assert.match(first.props.body, /could not start.*idea is saved/);
  assert.doesNotMatch(first.props.body, /service is down|few minutes|pagebuilder_renderer_unavailable/);
  const repeated = await refreshedProfile(h, failed('build-a'));
  assert.equal(control(repeated, 'website_build_retry').props.className, 'primary');
  const second = await refreshedProfile(h, failed('build-b', 2));
  assert.equal(control(second, 'website_find_customers').props.className, 'primary');
  assert.equal(control(second, 'website_build_retry').props.className, 'secondary');
  assert.equal(control(second, 'website_build_retry').props.children, 'Retry');
  assert.equal(control(second, 'website_build_retry').props.disabled, false);
  assert.match(second.props.body, /could not start.*idea is saved/);
  assert.ok(descendants(second).some(node => node.props?.children === 'Your idea is saved. Check the reason above, then retry your page.'));
  assert.equal([second.props.actions.props.children].flat().filter(child => child?.props?.className === 'primary').length, 1);
  const fresh = await refreshedProfile(h, failed('build-d', 1));
  assert.equal(control(fresh, 'website_build_retry').props.className, 'primary');
});
test('failure fallback remains a first failure when an older API omits the count', async () => {
  const h = harness(), job = failed('build-a'); delete job.consecutive_failures;
  const first = await h.candidateReady(profile(job));
  assert.equal(control(first, 'website_build_retry').props.className, 'primary');
});

const serverProfile = (current = {}) => ({ task_id: null, plan_id: null, artifact_profile: 'static_landing_v1',
  server_admission_enabled: true, quote: null, current: { job_id: 'candidate-a', state: 'ready', queue_state: 'succeeded',
    native_ready: false, candidates: [], server_publish_ready: true, server_candidates: [{variant: 0}], ...current } });
for (const [domain, title] of [['b2b', 'Equipment inspections'], ['consumer', 'Family pottery weekends']]) {
  test(`local ${domain} preview selects your saved page without hosted publishing or stale ownership`, async () => {
    const selections = [], localHarness = harness({ local: true, onSelection: selection => selections.push(selection) });
    const card = await localHarness.candidateReady(serverProfile(), `<h1>${title}</h1>`);
    assert.deepEqual(JSON.parse(JSON.stringify(selections)), [{ job_id: 'candidate-a', variant: 0 }]);
    assert.ok(descendants(card.props.actions).some(node => node.type === 'a' && node.props.href === '#local-publishing'));
    assert.equal(control(card, 'website_publish_review'), undefined);
    assert.ok(!descendants(card).some(node => node.type === 'site-address'));
    assert.match(card.props.body, /collect signups/i);
    assert.ok(!localHarness.requests.some(request => request.init?.method === 'POST'));
    localHarness.switchOwner('account-b'); localHarness.render();
    localHarness.window.location.pathname = '/v/venture-b/site'; localHarness.render('venture-b');
    assert.equal(selections.length, 1);
    const chatHarness = harness({ local: true, jobId: 'candidate-a' });
    const chatCard = await chatHarness.candidateReady(serverProfile(), `<h1>${title}</h1>`);
    assert.ok(descendants(chatCard.props.actions).some(node => node.type === 'a' && node.props.href === '/v/venture-a/site#local-publishing'));
  });
}
test('chat shows the selected private build and keeps publish and rebuild on that source', async () => {
  const dock = readFileSync(join(__dirname, '../chat/ChatDock.tsx'), 'utf8');
  assert.match(dock, /m.kind === "site_preview"/);
  assert.match(dock, /<BuildPageCard[^>]*ventureId=\{ventureId\} jobId=\{previewJob.job_id\}/);
  for (const idea of ['Inventory alerts for hospital procurement teams', 'A weekend birdwatching diary for families']) {
    const previewHarness = harness({jobId: 'saved-preview'});
    previewHarness.render(); await previewHarness.flush();
    previewHarness.requests.find(request => request.path.endsWith('/device-work-orders')).resolve({ok: true,
      data: {orders: [{id: 'unrelated-local-order', state: 'claimed', artifact_profile: 'static_landing_v1'}]}});
    previewHarness.requests.find(request => request.path.endsWith('/pagebuilder/profile?job_id=saved-preview'))
      .resolve({ok: true, data: serverProfile({job_id: 'saved-preview'})});
    await previewHarness.flush();
    previewHarness.requests.find(request => request.path.includes('/jobs/saved-preview/preview-document'))
      .resolve({ok: true, data: {html: `<h1>${idea}</h1>`}});
    await previewHarness.flush();
    const card = previewHarness.render();
    assert.equal(frame(card).props.srcDoc, `<h1>${idea}</h1>`);
    assert.equal(frame(card).props.sandbox, 'allow-popups allow-popups-to-escape-sandbox');
    assert.match(card.props.body, /private preview/);
    assert.equal(control(card, 'website_publish_review').props.disabled, false);
    control(card, 'website_build_new_version').props.onClick();
    const post = previewHarness.requests.find(request => request.init?.method === 'POST');
    assert.deepEqual(JSON.parse(post.init.body), {new_version_of: 'saved-preview'});
    post.resolve({ok: true, data: {job_id: 'new-preview', state: 'queued', stage: 'queued', progress: 0}});
    await previewHarness.flush();
    assert.equal(previewHarness.render().props.title, 'Building your landing page');
    void previewHarness.refresh(); await previewHarness.flush();
    assert.ok(previewHarness.requests.some(request => request.path.endsWith('/pagebuilder/profile?job_id=new-preview')));
    assert.deepEqual(previewHarness.navigations, []);
    previewHarness.unmount();
  }
});

const publication = (state, extra = {}) => ({run_id: 'run-a', state, live_url: state === 'live' ? 'https://nightshift.supymem.com/' : null,
  error_code: state === 'failed' ? 'pagebuilder_qa_failed' : null, retryable: false, job_attempt: null, ...extra});

test('a completed fallback page offers Improve again as a fresh build', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile({improvement_available: true}));
  const improve = control(card, 'website_build_new_version');
  assert.equal(improve.props.children, 'Improve again');
  assert.match(descendants(card).map(node => node.props?.children).flat(Infinity).filter(value => typeof value === 'string').join(' '), /simpler version/);
  improve.props.onClick(); improve.props.onClick();
  await h.flush();
  const requests = h.requests.filter(request => request.path.endsWith('/pagebuilder/build'));
  assert.equal(requests.length, 1);
  assert.deepEqual(JSON.parse(requests[0].init.body), {new_version_of: 'candidate-a'});
});
const correctablePublication = (attempt = 1) => publication('failed', {
  error_code: 'pagebuilder_publication_refused', retryable: true, job_attempt: attempt});

test('one no-plan Publish click checks and goes live on Website without another paid POST or native action', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile());
  const previewFrame = frame(card);
  assert.equal(previewFrame.props.sandbox, 'allow-popups allow-popups-to-escape-sandbox');
  const publish = control(card, 'website_publish_review');
  assert.ok(publish && !control(card, 'website_build_candidate'));
  publish.props.onClick(); publish.props.onClick();
  const posts = h.requests.filter(request => request.init?.method === 'POST');
  assert.equal(posts.length, 1);
  assert.ok(posts[0].path.endsWith('/pagebuilder-admission/publish'));
  assert.deepEqual(JSON.parse(posts[0].init.body), {job_id: 'candidate-a', variant: 0,
    plan_id: null, plan_task_id: null, confirmed: true});
  posts[0].resolve({ok: true, data: publication('checking')}); await h.flush();
  publish.props.onClick(); // A retained callback cannot repost after the response, before the next render.
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
  const checking = h.render();
  publish.props.onClick(); // The same retired callback remains inert after the render.
  assert.equal(checking.props.title, 'Publishing your landing page');
  assert.equal(control(checking, 'website_publish_review'), undefined);
  h.intervals.at(-1)(); await h.flush();
  const status = h.requests.at(-1);
  assert.ok(status.path.endsWith('/pagebuilder-admission/publish/run-a'));
  assert.equal(status.init.method, undefined);
  status.resolve({ok: true, data: publication('live')}); await h.flush();
  const live = h.render();
  assert.equal(live.props.title, 'Your landing page is live');
  assert.equal(control(live, 'website_open_url').props.href, 'https://nightshift.supymem.com/');
  assert.equal(control(live, 'website_find_customers').props.href, '/gtm?venture=venture-a');
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
  assert.deepEqual(h.navigations, []);
});

test('saved publication resumes status polling without repeating the Publish request', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile({publication: publication('checking')}));
  assert.equal(card.props.title, 'Publishing your landing page');
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 0);
  h.intervals.at(-1)(); await h.flush();
  h.requests.at(-1).resolve({ok: true, data: publication('failed')}); await h.flush();
  const failed = h.render();
  assert.equal(failed.props.status, 'needs_you');
  assert.equal(control(failed, 'website_open_url'), undefined);
  assert.equal(control(failed, 'website_publish_review'), undefined);
  assert.ok(failed.props.body.includes('private'));
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 0);
});

for (const retire of ['venture', 'account', 'navigation', 'unmount']) {
  test(`retired ${retire} cannot show a live URL from delayed publication`, async () => {
    const h = harness(), card = await h.candidateReady(serverProfile());
    control(card, 'website_publish_review').props.onClick();
    const request = h.requests.at(-1);
    if (retire === 'venture') { h.window.location.pathname = '/v/venture-b/site'; h.render('venture-b'); }
    if (retire === 'account') { h.switchOwner('account-b'); h.switchOwner('account-a'); }
    if (retire === 'navigation') h.window.location.pathname = '/connections';
    if (retire === 'unmount') h.unmount();
    request.resolve({ok: true, data: publication('live')}); await h.flush();
    assert.ok(!h.values().some(value => value?.value?.state === 'live'));
    assert.deepEqual(h.navigations, []);
  });
}

test('publication status for another run never exposes its live URL', async () => {
  const h = harness(); await h.candidateReady(serverProfile({publication: publication('checking')}));
  h.intervals.at(-1)(); await h.flush();
  h.requests.at(-1).resolve({ok: true, data: {...publication('live'), run_id: 'run-other'}}); await h.flush();
  assert.equal(control(h.render(), 'website_open_url'), undefined);
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 0);
});

test('native refinement uses the server variant actually shown in the restored preview', async () => {
  const h = harness(), card = await h.candidateReady({...serverProfile({native_ready: true, candidates: [{variant: 0}, {variant: 1}],
    server_candidates: [{variant: 1}], publication: publication('checking')}), plan_id: 'plan-a', task_id: 'landing-a'});
  assert.ok(h.requests.find(request => request.path.includes('preview-document?variant=1')));
  control(card, 'website_build_candidate').props.onClick();
  const request = h.requests.at(-1);
  assert.ok(request.path.endsWith('/from-chat'));
  assert.deepEqual(JSON.parse(request.init.body).pagebuilder_candidate, {job_id: 'candidate-a', variant: 1});
});

test('a server preview without the same eligible native variant cannot prepare another variant', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile({native_ready: true, candidates: [{variant: 0}],
    server_candidates: [{variant: 1}]}));
  assert.ok(h.requests.find(request => request.path.includes('preview-document?variant=1')));
  assert.equal(control(card, 'website_build_candidate'), undefined);
});

test('a successful Publish binding retires with the source and cannot be replayed against its replacement', async () => {
  const h = harness(), original = await h.candidateReady(serverProfile());
  const stale = control(original, 'website_publish_review');
  stale.props.onClick();
  h.requests.at(-1).resolve({ok: true, data: publication('checking')}); await h.flush(); h.render();
  void h.refresh(); await h.flush();
  h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
  h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true,
    data: serverProfile({job_id: 'candidate-b'})}); await h.flush();
  h.requests.find(request => request.path.includes('candidate-b/preview-document')).resolve({ok: true, data: {html: '<h1>Candidate B</h1>'}});
  await h.flush(); const replacement = h.render();
  stale.props.onClick();
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
  control(replacement, 'website_publish_review').props.onClick();
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 2);
  assert.equal(JSON.parse(h.requests.at(-1).init.body).job_id, 'candidate-b');
});

test('a correctable publication failure retries only its saved run and returns to live without another QA or initial Publish', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile());
  control(card, 'website_publish_review').props.onClick();
  h.requests.at(-1).resolve({ok: true, data: publication('checking')}); await h.flush(); h.render();
  h.intervals.at(-1)(); await h.flush();
  h.requests.at(-1).resolve({ok: true, data: correctablePublication()}); await h.flush();
  const failed = h.render(), retry = control(failed, 'website_retry');
  assert.equal(retry.props.children, 'Retry publication');
  retry.props.onClick(); retry.props.onClick();
  const requests = h.requests.filter(request => request.init?.method === 'POST');
  assert.equal(requests.length, 2);
  assert.ok(requests[0].path.endsWith('/pagebuilder-admission/publish'));
  assert.ok(requests[1].path.endsWith('/pagebuilder-admission/publish/run-a/retry'));
  assert.deepEqual(JSON.parse(requests[1].init.body), {expected_attempt: 1, confirmed: true});
  requests[1].resolve({ok: true, data: publication('publishing', {job_attempt: 1})}); await h.flush();
  retry.props.onClick(); h.render(); retry.props.onClick();
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 2);
  h.intervals.at(-1)(); await h.flush();
  h.requests.at(-1).resolve({ok: true, data: publication('live', {job_attempt: 2})}); await h.flush();
  const live = h.render();
  assert.equal(live.props.title, 'Your landing page is live');
  assert.ok(control(live, 'website_open_url') && control(live, 'website_find_customers'));
  assert.ok(!h.requests.some(request => /\/(?:quote|admit|from-chat|pagebuilder\/build)$/.test(request.path)));
  assert.deepEqual(h.navigations, []);
});

for (const saved of [publication('failed'), correctablePublication(null), correctablePublication(-1),
  correctablePublication('1'), correctablePublication(1.5), correctablePublication(true)]) {
  test(`irrecoverable or invalid observed publication attempt ${JSON.stringify(saved.job_attempt)} offers only refresh`, async () => {
    const h = harness(), card = await h.candidateReady(serverProfile({publication: saved}));
    assert.equal(control(card, 'website_retry'), undefined);
    assert.equal(control(card, 'website_publish_review'), undefined);
    assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 0);
  });
}

test('a consumed retry attempt remains consumed through same-source profile refresh and a later attempt retires older callbacks', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile({publication: correctablePublication()}));
  const oldRetry = control(card, 'website_retry');
  oldRetry.props.onClick();
  h.requests.at(-1).resolve({ok: true, data: publication('publishing', {job_attempt: 1})}); await h.flush(); h.render();
  const reload = async saved => {
    void h.refresh(); await h.flush();
    h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
    h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true,
      data: serverProfile({publication: saved})}); await h.flush();
    h.requests.filter(request => request.path.includes('/preview-document')).at(-1).resolve({ok: true, data: {html: '<h1>Saved preview</h1>'}});
    await h.flush(); return h.render();
  };
  const consumed = await reload(correctablePublication());
  assert.equal(control(consumed, 'website_retry'), undefined);
  oldRetry.props.onClick();
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
  const newer = await reload(correctablePublication(2));
  oldRetry.props.onClick();
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
  control(newer, 'website_retry').props.onClick();
  assert.equal(JSON.parse(h.requests.at(-1).init.body).expected_attempt, 2);
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 2);
});

test('an unconfirmed retry permits a new explicit click only after an authoritative same-attempt failure receipt', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile({publication: correctablePublication()}));
  const firstRetry = control(card, 'website_retry');
  firstRetry.props.onClick();
  h.requests.at(-1).resolve({ok: false, status: 503, data: {}}); await h.flush();
  const unconfirmed = h.render();
  assert.equal(control(unconfirmed, 'website_retry'), undefined);
  firstRetry.props.onClick();
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);

  void h.refresh(); await h.flush();
  h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
  h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: false, status: 503, data: {}});
  await h.flush();
  assert.equal(control(h.render(), 'website_retry'), undefined); // A failed read cannot grant another retry.
  firstRetry.props.onClick();
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);

  void h.refresh(); await h.flush();
  h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
  h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true,
    data: serverProfile({publication: correctablePublication()})}); await h.flush();
  h.requests.filter(request => request.path.includes('/preview-document')).at(-1).resolve({ok: true, data: {html: '<h1>Saved preview</h1>'}});
  await h.flush();
  const observed = h.render(), secondRetry = control(observed, 'website_retry');
  assert.ok(secondRetry);
  firstRetry.props.onClick(); // The pre-reconciliation callback cannot spend the new observation.
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
  secondRetry.props.onClick(); secondRetry.props.onClick();
  const retries = h.requests.filter(request => request.init?.method === 'POST');
  assert.equal(retries.length, 2);
  assert.ok(retries.every(request => request.path.endsWith('/publish/run-a/retry')));
  assert.deepEqual(JSON.parse(retries[1].init.body), {expected_attempt: 1, confirmed: true});
  retries[1].resolve({ok: true, data: publication('publishing', {job_attempt: 1})}); await h.flush();
  secondRetry.props.onClick(); h.render(); secondRetry.props.onClick();
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 2);
  h.intervals.at(-1)(); await h.flush();
  h.requests.at(-1).resolve({ok: true, data: publication('live', {job_attempt: 2})}); await h.flush();
  assert.equal(h.render().props.title, 'Your landing page is live');
  assert.ok(!h.requests.some(request => /\/(?:quote|admit|from-chat|pagebuilder\/build|pagebuilder-admission\/publish)$/.test(request.path)));
});

for (const timing of ['during', 'after']) {
for (const response of [{ok: false, status: 503, data: {}}, {ok: true,
  data: serverProfile({publication: publication('publishing', {job_attempt: 1})})}]) {
  test(`a pre-existing profile refresh ${response.ok ? 'queued' : 'failed'} response ${timing} the publication retry cannot retire it`, async () => {
    const h = harness(), card = await h.candidateReady(serverProfile({publication: correctablePublication()}));
    void h.refresh(); await h.flush();
    const profileRequest = h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1);
    const ordersRequest = h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1);
    const staleRetry = control(card, 'website_retry');
    staleRetry.props.onClick();
    const retryRequest = h.requests.at(-1), requestsBefore = h.requests.length;
    void h.refresh(); await h.flush();
    assert.equal(h.requests.length, requestsBefore); // Refresh never starts another read during the mutation.
    if (timing === 'during') {
      ordersRequest.resolve({ok: true, data: {orders: []}}); profileRequest.resolve(response); await h.flush();
    }
    retryRequest.resolve({ok: false, status: 503, data: {}}); await h.flush();
    if (timing === 'after') {
      ordersRequest.resolve({ok: true, data: {orders: []}}); profileRequest.resolve(response); await h.flush();
    }
    const unconfirmed = h.render();
    assert.equal(control(unconfirmed, 'website_retry'), undefined);
    assert.equal(unconfirmed.props.actions.props.children.find(item => item?.props?.children === 'Refresh status').props.disabled, false);
    staleRetry.props.onClick();
    assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
    void h.refresh(); await h.flush();
    h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
    h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true,
      data: serverProfile({publication: correctablePublication()})}); await h.flush();
    h.requests.filter(request => request.path.includes('/preview-document')).at(-1).resolve({ok: true, data: {html: '<h1>Saved preview</h1>'}});
    await h.flush();
    const recovered = h.render(), retry = control(recovered, 'website_retry');
    assert.ok(retry && !retry.props.disabled);
    retry.props.onClick();
    assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 2);
  });
}

}

test('a pre-existing failed profile refresh cannot overwrite an accepted publication retry after its response', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile({publication: correctablePublication()}));
  void h.refresh(); await h.flush();
  const profileRequest = h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1);
  const ordersRequest = h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1);
  control(card, 'website_retry').props.onClick();
  const retryRequest = h.requests.at(-1);
  const pending = h.render();
  assert.equal(pending.props.actions.props.children.find(item => item?.props?.children === 'Refresh status').props.disabled, true);
  retryRequest.resolve({ok: true, data: publication('publishing', {job_attempt: 1})}); await h.flush();
  ordersRequest.resolve({ok: true, data: {orders: []}});
  profileRequest.resolve({ok: false, status: 503, data: {}}); await h.flush();
  const saved = h.render();
  assert.equal(saved.props.title, 'Publishing your landing page');
  assert.equal(control(saved, 'website_retry'), undefined);
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
});

for (const state of ['checking', 'publishing', 'live']) {
  for (const attempt of [1, 2]) {
    test(`an accepted ${state} receipt at attempt ${attempt} retires an unconfirmed retry before stale failure data`, async () => {
      const h = harness(), card = await h.candidateReady(serverProfile({publication: correctablePublication()}));
      const staleRetry = control(card, 'website_retry');
      staleRetry.props.onClick();
      h.requests.at(-1).resolve({ok: false, status: 503, data: {}}); await h.flush(); h.render();
      const reload = async saved => {
        void h.refresh(); await h.flush();
        h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
        h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true,
          data: serverProfile({publication: saved})}); await h.flush();
        h.requests.filter(request => request.path.includes('/preview-document')).at(-1).resolve({ok: true, data: {html: '<h1>Saved preview</h1>'}});
        await h.flush(); return h.render();
      };
      await reload(publication(state, {job_attempt: attempt}));
      const staleFailure = await reload(correctablePublication());
      assert.equal(control(staleFailure, 'website_retry'), undefined);
      staleRetry.props.onClick();
      assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
    });
  }
}

for (const retire of ['venture', 'account', 'navigation', 'unmount']) {
  test(`retired ${retire} cannot retry or show a delayed publication retry result`, async () => {
    const h = harness(), card = await h.candidateReady(serverProfile({publication: correctablePublication()}));
    const retry = control(card, 'website_retry');
    retry.props.onClick(); const request = h.requests.at(-1);
    if (retire === 'venture') { h.window.location.pathname = '/v/venture-b/site'; h.render('venture-b'); }
    if (retire === 'account') { h.switchOwner('account-b'); h.switchOwner('account-a'); }
    if (retire === 'navigation') h.window.location.pathname = '/connections';
    if (retire === 'unmount') h.unmount();
    retry.props.onClick(); request.resolve({ok: true, data: publication('live', {job_attempt: 2})}); await h.flush();
    assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
    assert.ok(!h.values().some(value => value?.value?.state === 'live'));
    assert.deepEqual(h.navigations, []);
  });
}

test('a saved retry callback cannot target the replacement preview source', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile({publication: correctablePublication()}));
  const stale = control(card, 'website_retry');
  void h.refresh(); await h.flush();
  h.requests.filter(request => request.path.endsWith('/device-work-orders')).at(-1).resolve({ok: true, data: {orders: []}});
  h.requests.filter(request => request.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ok: true,
    data: serverProfile({job_id: 'candidate-b', publication: {...correctablePublication(), run_id: 'run-b'}})}); await h.flush();
  h.requests.find(request => request.path.includes('candidate-b/preview-document')).resolve({ok: true, data: {html: '<h1>Candidate B</h1>'}});
  await h.flush(); const replacement = h.render();
  stale.props.onClick();
  assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 0);
  control(replacement, 'website_retry').props.onClick();
  assert.ok(h.requests.at(-1).path.endsWith('/publish/run-b/retry'));
});

test('final checks that could not run offer one plain Publish again that starts a fresh check', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile({publication: publication('failed', {
    error_code: 'pagebuilder_publication_refused', retryable: true, job_attempt: null, next_action: 'publish_again'})}));
  assert.equal(card.props.body, "Our final checks couldn't run. Your preview stays private. Try publishing again.");
  assert.equal(control(card, 'website_retry'), undefined);
  const again = control(card, 'website_publish_again');
  assert.equal(again.props.className, 'primary');
  again.props.onClick(); again.props.onClick();
  const posts = h.requests.filter(request => request.init?.method === 'POST');
  assert.equal(posts.length, 1);
  assert.ok(posts[0].path.endsWith('/pagebuilder-admission/publish'));
  assert.deepEqual(JSON.parse(posts[0].init.body), {job_id: 'candidate-a', variant: 0, plan_id: null, plan_task_id: null, confirmed: true});
  posts[0].resolve({ok: true, data: publication('checking', {run_id: 'run-b', next_action: null})}); await h.flush();
  const checking = h.render();
  assert.equal(checking.props.title, 'Publishing your landing page');
  assert.equal(control(checking, 'website_publish_again'), undefined);
});

for (const saved of [publication('failed', {next_action: 'build_new_version'}), publication('failed')]) {
  test(`a final page-check failure (${saved.next_action ?? 'older server'}) leads to a new version, never only refresh`, async () => {
    const h = harness(), card = await h.candidateReady(serverProfile({publication: saved}));
    assert.match(card.props.body, /didn't pass the final checks.*new version/);
    assert.doesNotMatch(card.props.body, /pagebuilder_|run-a/);
    const next = control(card, 'website_build_new_version');
    assert.equal(next.props.className, 'primary');
    assert.equal(next.type, 'button');
    next.props.onClick();
    assert.deepEqual(JSON.parse(h.requests.find(request => request.init?.method === 'POST').init.body), {new_version_of: 'candidate-a'});
    assert.equal(control(card, 'website_publish_again'), undefined);
    assert.equal(h.requests.filter(request => request.init?.method === 'POST').length, 1);
  });
}

const unconfirmed = { ok: false, status: 409, data: { detail: { code: 'pagebuilder_confirmed_revision_required' } } };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

for (const [sessionId, revisionId, confirmationOk] of [['idea-a', 'revision-a', true], ['idea-b', 'revision-b', false]]) {
test(`landing page choice confirms ${revisionId} before any build or profile read (${confirmationOk})`, async () => {
  const h = harness(); h.render(); await h.flush();
  h.requests.find(r => r.path.endsWith('/device-work-orders')).resolve({ ok: true, data: { orders: [] } });
  h.requests.find(r => r.path.endsWith('/pagebuilder/profile')).resolve(unconfirmed);
  await h.flush();
  const sessions = h.requests.find(r => r.path.endsWith('/idea/sessions'));
  assert.ok(sessions, 'read the saved idea before offering confirmation');
  sessions.resolve({ ok: true, data: { items: [{ id: sessionId, state: 'validating' }] } });
  await h.flush();
  const saved = h.requests.find(r => r.path.endsWith('/idea/sessions/' + sessionId));
  saved.resolve({ ok: true, data: { id: sessionId, state: 'validating', case: 'explore',
    current_revision: { id: revisionId }, validation_runs: ['gstack_office_hours', 'gstack_ceo_review', 'gbrain_think']
      .map(method => ({ id: method, method, revision_id: revisionId, state: 'complete' })) } });
  await h.flush();
  const button = control(h.render(), 'website_confirm_idea');
  assert.ok(button); assert.equal(button.props.children, 'Build my landing page now');
  button.props.onClick(); button.props.onClick(); await h.flush();
  const choices = descendants(h.render().props.actions).find(node => node.type === 'page-choices');
  assert.ok(choices);
  assert.equal(h.requests.filter(r => r.init?.method === 'POST').length, 0);
  choices.props.onBuild(); choices.props.onBuild(); await h.flush();
  const posts = h.requests.filter(r => r.init?.method === 'POST');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, '/api/ventures/venture-a/idea/sessions/' + sessionId + '/confirm');
  assert.deepEqual(JSON.parse(posts[0].init.body), { revision_id: revisionId, start_research: false, accept_validation: true });
  assert.equal(h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).length, 1);
  posts[0].resolve(confirmationOk ? { ok: true, data: { dossier_job_id: null } }
    : { ok: false, status: 409, data: { detail: { code: 'idea_review_conflict' } } }); await h.flush();
  const build = h.requests.find(r => r.path.endsWith('/pagebuilder/build'));
  assert.equal(Boolean(build), confirmationOk);
  assert.equal(h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).length, 1);
  if (confirmationOk) {
    build.resolve({ ok: true, data: { job_id: 'page-job', state: 'queued' } }); await h.flush();
    assert.equal(h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).length, 2);
    h.requests.filter(r => r.path.endsWith('/pagebuilder/profile')).at(-1).resolve({ ok: true, data: profile({ job_id: 'page-job', state: 'queued', candidates: [] }) });
    h.requests.filter(r => r.path.endsWith('/device-work-orders')).at(-1).resolve({ ok: true, data: { orders: [] } });
    await h.flush();
    assert.match(h.render().props.title, /Building your landing page/);
  } else {
    assert.match(JSON.stringify(h.render()), /Review the current validation/);
    assert.equal(control(h.render(), 'website_continue_clarification').props.href, '/v/venture-a/idea');
  }
});
}

test('an unconfirmed idea renders from the first server answer, without waiting for local tool probes', async () => {
  const localTools = deferred(), h = harness({ localTools });  // `claude`/`codex` probes can take ~12 s on a Mac
  h.render(); await h.flush();
  // Both server reads go out on mount; neither waits for local detection.
  assert.ok(h.requests.some(r => r.path.endsWith('/pagebuilder/profile')));
  assert.ok(h.requests.some(r => r.path.endsWith('/device-work-orders')));
  h.requests.find(r => r.path.endsWith('/device-work-orders')).resolve({ ok: true, data: { orders: [] } });
  h.requests.find(r => r.path.endsWith('/pagebuilder/profile')).resolve(unconfirmed);
  await h.flush();
  const card = h.render();
  assert.equal(card.props.title, 'Your landing page needs attention');
  assert.match(card.props.body, /Confirm your idea first/);
  // The primary action continues clarification; a Refresh cannot change this outcome.
  assert.equal(card.props.actions.type, 'a');
  assert.equal(card.props.actions.props.href, '/v/venture-a/idea');
  assert.equal(card.props.actions.props.className, 'primary');
  assert.equal(card.props.actions.props['data-usage-control'], 'website_continue_clarification');
  assert.equal(card.props.actions.props.children, 'Continue clarification');
  localTools.resolve([]); await h.flush();
});

test('an unconfirmed idea reuses the founder next-step button when the server has one', async () => {
  const step = { key: 'idea.clarify', cta_label: 'Continue clarification', href: '/v/venture-a/idea' };
  const h = harness({ nextStep: step });
  h.render(); await h.flush();
  assert.ok(h.nextStepReads.every(venture => venture === undefined), 'no next-step read until it is needed');
  h.requests.find(r => r.path.endsWith('/device-work-orders')).resolve({ ok: true, data: { orders: [] } });
  h.requests.find(r => r.path.endsWith('/pagebuilder/profile')).resolve(unconfirmed);
  await h.flush();
  const card = h.render();
  assert.equal(card.props.actions.type, 'next-step-button');
  assert.equal(card.props.actions.props.step, step);
  assert.equal(card.props.actions.props.className, 'primary');
});

test('only problems that re-reading can change keep Refresh as the primary action', async () => {
  const h = harness();
  h.render(); await h.flush();
  h.requests.find(r => r.path.endsWith('/device-work-orders')).resolve({ ok: true, data: { orders: [] } });
  h.requests.find(r => r.path.endsWith('/pagebuilder/profile')).resolve({ ok: false, status: 503, data: {} });
  await h.flush();
  const card = h.render();
  assert.equal(card.props.actions.type, 'button');
  assert.equal(card.props.actions.props.children, 'Refresh');
});

test('a build that needs credits or the waitlist leads to the page that fixes it', async () => {
  const h = harness(), card = await h.candidateReady(profile(null));
  card.props.actions.props.onBuild();
  h.requests.find(r => r.init?.method === 'POST').resolve({ ok: false, status: 402, data: { detail: { code: 'pagebuilder_insufficient_credits' } } });
  await h.flush();
  const after = h.render();
  assert.equal(after.props.actions.props.href, '/v/venture-a/settings');
  assert.equal(after.props.actions.props['data-usage-control'], 'website_open_settings');
  const waitlist = harness(), failedCard = await waitlist.candidateReady(profile({ job_id: 'candidate-a', state: 'failed',
    error: 'pagebuilder_waitlist_disabled', consecutive_failures: 1, native_ready: false, candidates: [] }));
  const link = control(failedCard, 'website_open_waitlist');
  assert.equal(link.props.href, '/gtm?venture=venture-a');
  assert.equal(link.props.className, 'primary');
  assert.equal(control(failedCard, 'website_build_retry').props.className, 'secondary');
});

const fieldOf = card => descendants(card).find(node => node.type === 'site-address');

test('Publish waits for a valid address and saves an edited one first', async () => {
  const h = harness(); await h.candidateReady(serverProfile());
  h.manualAddress();
  const waiting = h.render();
  assert.equal(fieldOf(waiting).props.target, 'pagebuilder');
  const publish = () => control(h.render(), 'website_publish_review');
  fieldOf(waiting).props.onReady(null, 'nightshift');
  assert.equal(publish().props.disabled, true);
  publish().props.onClick();
  assert.equal(h.requests.filter(r => r.init?.method).length, 0);          // nothing is sent without an available address
  fieldOf(h.render()).props.onReady('night-shift', 'nightshift');
  assert.equal(publish().props.disabled, false);
  publish().props.onClick();
  const put = h.requests.find(r => r.init?.method === 'PUT');
  assert.ok(put.path.endsWith('/site-address'));
  assert.deepEqual(JSON.parse(put.init.body), {slug: 'night-shift', target: 'pagebuilder'});
  assert.equal(h.requests.filter(r => r.init?.method === 'POST').length, 0);  // not published before the address is saved
  put.resolve({ok: false, status: 409, data: {detail: {message: 'That address is already taken.', suggestion: 'night-shift-2'}}});
  await h.flush();
  assert.equal(h.requests.filter(r => r.init?.method === 'POST').length, 0);
  assert.match(JSON.stringify(h.values()), /already taken\. Try night-shift-2/);
});

test('the live card offers Change address with the old-link note, then moves the page through a new Publish', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile({publication: publication('live')}));
  assert.equal(card.props.title, 'Your landing page is live');
  control(card, 'website_address_change') || assert.ok(descendants(card).find(n => n.props?.['data-usage-control'] === 'website_address_change'));
  descendants(card).find(n => n.props?.['data-usage-control'] === 'website_address_change').props.onClick();
  h.manualAddress();
  const open = h.render();
  assert.match(descendants(open).map(n => n.props?.children).flat(Infinity).filter(x => typeof x === 'string').join(' '), /Your old link keeps working\./);
  const save = () => descendants(h.render()).find(n => n.props?.['data-usage-control'] === 'website_address_save');
  fieldOf(open).props.onReady('nightshift', 'nightshift');
  assert.equal(save().props.disabled, true);                               // unchanged address
  fieldOf(h.render()).props.onReady('better-name', 'nightshift');
  save().props.onClick();
  const put = h.requests.find(r => r.init?.method === 'PUT');
  put.resolve({ok: true, data: {changed: true}}); await h.flush();
  const post = h.requests.find(r => r.init?.method === 'POST');
  assert.ok(post.path.endsWith('/pagebuilder-admission/publish'));
  post.resolve({ok: true, data: publication('checking', {run_id: 'run-b'})}); await h.flush();
  assert.equal(h.render().props.title, 'Publishing your landing page');
});

test('a live landing page puts Visit and Copy first and a new version actually requests fresh source', async () => {
  const h = harness(), card = await h.candidateReady(serverProfile({publication: publication('live')}));
  assert.equal(card.props.status, 'done');
  assert.equal(control(card, 'website_open_url').props.children, 'Visit landing page');
  assert.ok(control(card, 'website_copy_url'));
  control(card, 'website_build_new_version').props.onClick();
  const request = h.requests.find(r => r.init?.method === 'POST');
  assert.deepEqual(JSON.parse(request.init.body), {new_version_of: 'candidate-a'});
});

test('failed generation keeps the safe server reason even after two attempts', async () => {
  const h = harness();
  const reason = 'The landing page builder could not produce a complete, usable landing page plan. Your idea is saved.';
  const card = await h.candidateReady(profile({job_id: 'candidate-a', state: 'failed', native_ready: false,
    candidates: [], consecutive_failures: 2, error: 'pagebuilder_plan_rejected', error_reason: reason}));
  assert.equal(card.props.body, reason);
  assert.ok(control(card, 'website_build_retry'));
});

for (const idea of ['grocery stock planning', 'family outdoor activities'])
test(`Retry displays the saved specific QA reason for ${idea}`, async () => {
  const h = harness({local: true});
  const card = await h.candidateReady(profile({job_id: 'candidate-a', state: 'failed', native_ready: false,
    candidates: [], error: 'pagebuilder_qa_failed'}));
  control(card, 'website_build_retry').props.onClick();
  const request = h.requests.find(entry => entry.path.endsWith('/pagebuilder/build'));
  const reason = `Your page for ${idea} failed these checks: working page controls; signup destination. On Website, choose Retry. Nothing was published.`;
  request.resolve({ok: true, data: {job_id: 'candidate-a', state: 'failed',
    error: 'pagebuilder_qa_failed', error_reason: reason}});
  await h.flush();
  assert.equal(h.render().props.body, reason);
});

for (const reason of [undefined, "Our final checks couldn't complete. Your preview stays private; no landing page-content failure was confirmed."])
test(`a failed final-check attempt without a report never claims the landing page failed QA (${reason ? 'server reason' : 'fallback'})`, async () => {
  const h = harness();
  const card = await h.candidateReady(serverProfile({publication: publication('failed', {
    next_action: 'build_new_version', qa_report_available: false,
    error_reason: reason})}));
  assert.match(card.props.body, /no landing page-content failure was confirmed/);
  assert.doesNotMatch(card.props.body, /didn't pass/);
  assert.ok(control(card, 'website_build_new_version'));
  assert.equal(descendants(card).find(node => node.props?.children === 'Review the failed checks'), undefined);
});

test('failed QA with a saved report links to the exact Website findings', async () => {
  const h = harness();
  const card = await h.candidateReady(serverProfile({publication: publication('failed', {
    next_action: 'build_new_version', qa_report_available: true,
    error_reason: 'The landing page did not pass its final checks and stays private.'})}));
  const link = descendants(card).find(node => node.type === 'a' && node.props.children === 'Review the failed checks');
  assert.equal(link.props.href, '/v/venture-a/site?run=run-a#build-review');
  assert.ok(control(card, 'website_build_new_version'));
});

test('saved failure remains visible when current publication admission is unavailable', async () => {
  const h = harness();
  const ready = serverProfile({publication: publication('failed', {next_action: 'build_new_version',
    qa_report_available: false, error_reason: 'The approved landing page inputs changed. Review the current idea.'})});
  ready.current.server_publish_ready = false;
  ready.current.server_candidates = [];
  const card = await h.candidateReady(ready);
  assert.equal(card.props.body, 'The approved landing page inputs changed. Review the current idea.');
  assert.ok(control(card, 'website_build_new_version'));
  assert.equal(control(card, 'website_publish_review'), undefined);
});

test('Website evidence automatically loads failed checks and hides raw runtime errors', async () => {
  let cursor = 0, tree;
  const hooks = [], effects = [], calls = [];
  const build = {id: 'run-a', app_id: 'app-a', artifact_id: 'source-a', qa_run_id: 'qa-a', runtime_version: 1,
    state: 'failed', landing_page: {qa_report_available: true}};
  const receipt = {id: 'qa-a', source_artifact_id: 'source-a', verified_report: true};
  const report = {cases: [
    {id: 'copy', suite: 'render', target: 'index.html', status: 'failed', observed: 'The main heading is missing.', expected: 'Visible heading'},
    {id: 'runtime:failure', suite: 'runtime', status: 'failed', observed: 'qa_private_exception_id', expected: 'Checks finish'},
    {id: 'runtime:sandbox', suite: 'runtime', status: 'passed', observed: 'qa_runtime_internal_id', expected: 'Sandboxed browser'},
  ], exclusions: [], evidence: []};
  const react = {
    useRef(initial) { return hooks[cursor++] ||= {current: initial}; },
    useState(initial) { const index = cursor++; hooks[index] ||= {value: initial}; return [hooks[index].value, value => {hooks[index].value = value;}]; },
    useEffect(callback, deps) { const index = cursor++; if (!hooks[index] || JSON.stringify(hooks[index]) !== JSON.stringify(deps)) {hooks[index] = deps; effects.push(callback);} },
  };
  const jsx = (type, props) => ({type, props});
  const Component = compile(join(__dirname, '../builds/BuildEvidence.tsx'), id => {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    if (id === "@/lib/chatRouteAction") return chatRoutes;
    if (id === "@/components/settings/AccountSettings") return { AccountSettings: "account-settings" };

    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    if (id === '@/lib/client') return {verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
      api: async path => {calls.push(path); return {ok: true, data: receipt};},
      authenticatedFetch: async () => ({ok: true, arrayBuffer: async () => new ArrayBuffer(0)})};
    if (id === '@/lib/buildReview') return {...compile(join(__dirname, '../../lib/buildReview.ts'), () => ({})), buildReceiptRoute: () => 'owned-route', reviewMatches: () => false,
      currentReviewReady: () => false, checkedReport: async () => report, reviewReadinessDescription: () => ''};
    if (id === '@/lib/apiFailure') return {};
    if (id === '@/lib/credits') return creditCopy;
    if (id === '@/lib/uiDiagnostics') return {announceDiagnostic() {}};
    if (id === './PrivateCorrection') return {PrivateCorrection: 'correction'};
    if (id.endsWith('/ErrorNotice')) return {ErrorNotice: 'error'};
    throw new Error(id);
  }, {window: {location: {}}}).BuildEvidence;
  function render() {cursor = 0; tree = Component({build, onReviewed() {}}); effects.splice(0).forEach(callback => callback());}
  render();
  await new Promise(resolve => setImmediate(resolve));
  render();
  assert.deepEqual(calls, ['/api/builds/run-a/qa/qa-a']);
  const copy = JSON.stringify(tree);
  assert.match(copy, /Checks that need attention/);
  assert.match(copy, /The main heading is missing/);
  assert.match(copy, /runner stopped/);
  assert.match(copy, /runtime check passed/);
  assert.doesNotMatch(copy, /qa_private_exception_id|qa_runtime_internal_id/);
  build.landing_page.qa_report_available = false;
  render();
  assert.equal(calls.length, 1);
  assert.match(JSON.stringify(tree), /No verified final-check report is available/);
});
