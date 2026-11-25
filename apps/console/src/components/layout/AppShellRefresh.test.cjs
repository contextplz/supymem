const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

test('local background errors verify your app connection without treating feature refusals as outages', async () => {
  const source = readFileSync(__dirname + '/AppShell.tsx', 'utf8');
  const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect;
  const visit = node => {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect'
        && node.arguments[0].getText(tree).includes('supymem-session-reconnecting')) effect = node.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  };
  visit(tree);
  for (const domain of ['warehouse teams', 'weekend walkers']) for (const local of [false, true]) {
    let listener, message = '', checks = 0, healthy = true;
    const context = {local, window: {addEventListener: (_, callback) => {listener = callback;}, removeEventListener() {}},
      setConnectionError: value => {message = value;},
      ensureSession: async (_, force) => {assert.equal(force, true); checks++; return {ok: healthy};}};
    const cleanup = vm.runInNewContext(ts.transpileModule(`(${effect})()`, {
      compilerOptions: {target: ts.ScriptTarget.ES2022},
    }).outputText, context);
    await listener();
    assert.equal(message, local ? '' : 'Reconnecting...', domain);
    if (local) {
      assert.equal(checks, 1);
      healthy = false; await listener(); assert.equal(message, '');
      await listener(); assert.equal(message, 'Reconnecting...');
      healthy = true; await listener(); assert.equal(message, '');
      cleanup(); healthy = false; await listener(); assert.equal(message, '');
    } else assert.equal(checks, 0);
  }
});

test('a local experiment draft reaches only the intended venture chat after navigation', () => {
  const source = readFileSync(__dirname + '/AppShell.tsx', 'utf8');
  const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const shell = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const launch = shell.body.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'openInChat');
  const receive = shell.body.statements.find(node => ts.isExpressionStatement(node) && node.getText(tree).startsWith('useEffect') && node.getText(tree).includes('const draft = localChatDraft'));
  assert.ok(launch && receive);
  const compile = text => ts.transpileModule(text, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  for (const [ventureId, audience] of [['ven_freight', 'depot managers'], ['ven_hikes', 'weekend hikers']]) {
    for (const accountChanged of [false, true]) {
      const paths = [], drafts = [];
      const context = {local: true, localSubpage: true, chatFirstHome: false, chatAvailable: true, authed: true,
        localChatDraft: null, effectiveVentureId: ventureId, sessionSubject: 'owner', epoch: 1, PREFILL_ACTION: 'chat.prefill',
        verifiedSessionSubject: () => context.sessionSubject, verifiedSessionEpoch: () => context.epoch,
        setPendingAsk: value => drafts.push(value), setChatPanelOpen() {}, router: {push: path => paths.push(path)},
        useEffect: work => work(), detail: {action: 'chat.prefill', payload: {text: `Design an experiment for ${audience}`}}};
      vm.runInNewContext(compile(`${launch.getText(tree)}\nopenInChat(detail);`), context);
      assert.deepEqual(paths, [`/v/${ventureId}`]);
      assert.equal(drafts.length, 0);
      context.chatFirstHome = true;
      if (accountChanged) context.epoch++;
      vm.runInNewContext(compile(receive.getText(tree)), context);
      vm.runInNewContext(compile(receive.getText(tree)), context);
      assert.deepEqual(drafts, accountChanged ? [] : [context.detail]);
    }
  }
});

test('deep-link forms wait for the saved layout before mounting across unrelated domains', async () => {
  const visibility = await import('../../lib/chatVisibility.ts');
  const source = readFileSync(__dirname + '/AppShell.tsx', 'utf8');
  const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const loading = component.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(tree).includes('!founderUILoaded'));
  const hidden = component.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(tree) === 'hiddenPage');
  const code = ts.transpileModule(`(function () { ${loading.getText(tree)} ${hidden.getText(tree)} return children; })`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  for (const [venture, domain] of [['ven_dispatch', 'Fleet maintenance teams'], ['ven_trails', 'Families planning weekend walks']]) {
    for (const pathname of ['/gtm', `/v/${venture}/research`, `/v/${venture}/idea`, `/v/${venture}/plan`]) {
      const children = { domain, audience: domain, ratings: { useful: 4, new: 3, actionable: 5 }, saved: true };
      const context = { ...visibility, pathname, children, founderUILoaded: false, hiddenPage: false,
        React: { createElement: (type, props, ...children) => ({ type, props, children }) } };
      const render = vm.runInNewContext(code, context);
      const pending = render();
      assert.equal(pending?.props?.role, 'status', `Do not mount ${pathname} in a guessed layout`);
      assert.notEqual(pending, children);
      context.founderUILoaded = true;
      assert.equal(render(), children, 'Classic deep links mount after their layout is verified');
      assert.equal(render(), children, 'A same-layout refresh preserves audience and feedback state');
      context.hiddenPage = true;
      assert.equal(render(), null, 'Chat-first deep links stay unmounted while redirecting');
    }
  }
});

test('refresh reconciles keyed requests once and reconnect keeps your chat and draft in place across domains', async () => {
  const client = await import('../../lib/client.ts');
  const previousWindow = global.window, previousFetch = global.fetch, previousStorage = global.sessionStorage;
  // Node 22 (CI) has no Web Storage global; Node 25 does.
  const store = new Map();
  global.sessionStorage = { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, String(value)), removeItem: key => store.delete(key) };
  try {
    for (const domain of ['warehouse purchasing teams', 'weekend dog walkers']) {
      client.clearVerifiedSession();
      const calls = [], diagnostics = [];
      let interrupted = true;
      global.window = { __TAURI__: { core: { invoke: async (command, args) => {
        if (command === 'gateway_url') return 'https://fixture.invalid';
        if (command === 'desktop_auth_status') return {state: 'connected'};
        assert.equal(command, 'desktop_api_request');
        if (args.path === '/auth/session') return {status: 200, data: {authenticated: true, subject: 'usr_refresh'}};
        calls.push(args);
        if (interrupted) { interrupted = false; return {status: 401, data: {error: {code: 'account_changed_during_request'}}}; }
        return {status: 200, data: {id: 'saved_run', domain}};
      } } }, dispatchEvent: event => {diagnostics.push(event); return true;} };
      global.fetch = async () => assert.fail('Native requests cannot use cookie fallback');
      assert.equal((await client.ensureSession('unused', true)).ok, true);
      const path = '/api/ventures/ven_refresh/chat/confirm';
      const init = {method: 'POST', headers: {'Idempotency-Key': 'same-operation'}, body: JSON.stringify({domain})};
      const result = await client.api(path, init);
      assert.equal(result.ok, true);
      assert.equal(result.data.domain, domain);
      assert.equal(calls.length, 2);
      assert.deepEqual(calls[0], calls[1]);
      assert.equal(calls[1].headers['Idempotency-Key'], 'same-operation');
      assert.ok(!diagnostics.some(event => event.detail?.code === 'unauthenticated'));
      interrupted = true;
      const unkeyed = await client.api(path, {method: 'POST', body: '{}'});
      assert.equal(unkeyed.status, 401);
      assert.equal(calls.length, 3, 'Never retry an unkeyed mutation');
      const source = readFileSync(__dirname + '/AppShell.tsx', 'utf8');
      const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
      let reconnect;
      const visit = node => {if (ts.isFunctionDeclaration(node) && node.name?.text === 'reconnectInPlace') reconnect = node; ts.forEachChild(node, visit);};
      visit(tree);
      let chatOpen = false, attempts = 0;
      const draft = {text: domain};
      vm.runInNewContext(ts.transpileModule(reconnect.getText(tree) + '; reconnectInPlace();', {
        compilerOptions: {target: ts.ScriptTarget.ES2022},
      }).outputText, {chatShown: true, setChatPanelOpen: value => {chatOpen = value;},
        setSessionAttempt: update => {attempts = update(attempts);}, draft,
        router: {replace: () => assert.fail('Reconnect must not navigate')}});
      assert.equal(chatOpen, true);
      assert.equal(attempts, 1);
      assert.equal(draft.text, domain);
    }
  } finally { global.window = previousWindow; global.fetch = previousFetch; global.sessionStorage = previousStorage; client.clearVerifiedSession(); }
});

test('shell keeps your workspace during outages, retries with capped backoff, and signs out only on rejection', async () => {
  const { isInvalidSession } = await import('../../lib/client.ts');
  const { nativeSessionNeedsSignIn } = await import('../../lib/nativeSessionGate.ts');
  const source = readFileSync(__dirname + '/AppShell.tsx', 'utf8');
  const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'AppShellInner');
  const effects = [];
  let notice;
  const visit = node => {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect') effects.push(node.arguments[0].getText(tree));
    if (ts.isJsxExpression(node) && node.getText(tree).startsWith('{connectionError && (') && node.getText(tree).includes('style={{ margin:')) notice = node.expression.getText(tree);
    ts.forEachChild(node, visit);
  };
  visit(tree);
  const effect = effects.find(code => code.includes('await ensureSession(') && code.includes('setChecking(true)'));
  const retry = effects.find(code => code.includes('connectionError !== "Reconnecting..."'));
  assert.ok(effect && retry && notice);
  const compile = code => ts.transpileModule(`(${code})`, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React } }).outputText;
  for (const audience of ['industrial safety teams', 'families learning music']) {
    for (const native of [false, true]) {
      let authed = true, connectionError = '', nativeState, session = { ok: true, status: 200, data: { subject: 'usr_owner' } };
      const draft = { audience };
      const context = {
        local: false, browserEntry: false, pathname: '/account', sessionAttempt: 0, isDesktopShell: () => native,
        ensureSession: async () => session, isInvalidSession, nativeSessionNeedsSignIn,
        desktopInvoke: async () => ({ state: 'connected' }), nativeAuthAvailabilityMessage: () => undefined,
        setChecking() {}, setSessionSubject() {}, setNativeAccountState: value => { nativeState = value; },
        setAuthed: value => { authed = value; }, setConnectionError: value => { connectionError = value; },
      };
      const run = vm.runInNewContext(compile(effect), context);
      for (const status of [502, 503, 504, 0, 401, 403]) {
        session = { ok: false, status, data: { error: { code: 'core_unavailable' } } };
        const cleanup = run(); await new Promise(resolve => setImmediate(resolve)); cleanup();
        assert.equal(authed, true, `${audience}: ${status} must preserve your workspace`);
        assert.equal(connectionError, 'Reconnecting...');
        assert.deepEqual(draft, { audience });
      }
      const delays = [], timers = new Map();
      const retryContext = {
        local: false, connectionError, checking: false, reconnectAttempts: { current: 0 },
        window: { setTimeout: (callback, delay) => { delays.push(delay); timers.set(delays.length, callback); return delays.length; }, clearTimeout: id => timers.delete(id) },
        setSessionAttempt: update => { retryContext.sessionAttempt = update(retryContext.sessionAttempt); }, sessionAttempt: 0,
      };
      const retryRun = vm.runInNewContext(compile(retry), retryContext);
      for (let attempt = 0; attempt < 7; attempt++) {
        const cleanup = retryRun(); timers.get(delays.length)(); cleanup();
      }
      assert.deepEqual(delays, [1000, 2000, 4000, 8000, 16000, 30000, 30000]);
      assert.equal(timers.size, 0, 'Unmount cancels pending retries');
      const React = require('react');
      const html = require('react-dom/server').renderToStaticMarkup(vm.runInNewContext(compile(`() => (${notice})`), {
        React, local: false, connectionError, connectionMessage: connectionError, reconnecting: true, accountPending: false, checking: false, reconnectInPlace() {},
      })());
      assert.match(html, /role="status"/); assert.match(html, /Reconnecting\.\.\./);
      assert.doesNotMatch(html, /role="alert"|<button/);
      const messages = component.body.statements.filter(node => node.getText(tree).startsWith('const reconnecting =') || node.getText(tree).startsWith('const connectionMessage ='));
      const localContext = {React, local: true, connectionError, accountPending: false, checking: false, reconnectInPlace() {}};
      const localHtml = require('react-dom/server').renderToStaticMarkup(vm.runInNewContext(compile(`() => { ${messages.map(node => node.getText(tree)).join('\n')} return (${notice}); }`), localContext)());
      assert.match(localHtml, /role="alert"/);
      assert.match(localHtml, /Check that your app is running/);
      assert.match(localHtml, /Retry/);
      assert.doesNotMatch(localHtml, /Reconnecting/);
      retryContext.local = true;
      retryRun();
      assert.equal(timers.size, 0, 'You choose when to retry a local connection error');
      retryContext.local = false;
      session = { ok: true, status: 200, data: { subject: 'usr_owner' } };
      let cleanup = run(); await new Promise(resolve => setImmediate(resolve)); cleanup();
      assert.equal(connectionError, ''); assert.equal(authed, true);
      retryContext.connectionError = ''; retryRun(); assert.equal(retryContext.reconnectAttempts.current, 0);
      session = { ok: false, status: 401, data: { error: { code: 'session_invalid' } } };
      cleanup = run(); await new Promise(resolve => setImmediate(resolve)); cleanup();
      assert.equal(authed, false); assert.equal(connectionError, '');
      if (native) assert.equal(nativeSessionNeedsSignIn(nativeState), true);
    }
  }
});

test('a visitor who was never signed in reaches sign-in instead of Reconnecting', async () => {
  const { isInvalidSession } = await import('../../lib/client.ts');
  const { nativeSessionNeedsSignIn } = await import('../../lib/nativeSessionGate.ts');
  const source = readFileSync(__dirname + '/AppShell.tsx', 'utf8');
  const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const effects = [];
  const visit = node => {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect') effects.push(node.arguments[0].getText(tree));
    ts.forEachChild(node, visit);
  };
  visit(tree);
  const effect = effects.find(code => code.includes('await ensureSession(') && code.includes('setChecking(true)'));
  const compile = code => ts.transpileModule(`(${code})`, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React } }).outputText;
  for (const [status, code] of [[401, 'unauthenticated'], [401, 'invalid_passphrase'], [403, 'forbidden']]) {
    let authed = true, connectionError = 'x';
    const context = {
      local: false, browserEntry: false, pathname: '/auth', sessionAttempt: 0, isDesktopShell: () => false,
      ensureSession: async () => ({ ok: false, status, data: { error: { code } } }), isInvalidSession, nativeSessionNeedsSignIn,
      desktopInvoke: async () => null, nativeAuthAvailabilityMessage: () => undefined,
      setChecking() {}, setSessionSubject() {}, setNativeAccountState() {},
      setAuthed: value => { authed = value; }, setConnectionError: value => { connectionError = value; },
    };
    const cleanup = vm.runInNewContext(compile(effect), context)(); await new Promise(resolve => setImmediate(resolve)); cleanup();
    assert.equal(authed, false, `${status} ${code} must show sign-in`);
    assert.equal(connectionError, '');
  }
});
