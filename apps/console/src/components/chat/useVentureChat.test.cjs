const assert = require('node:assert/strict'), test = require('node:test');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript');
const read = file => fs.readFileSync(path.resolve(__dirname, file), 'utf8');
const compile = source => ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;

function composerSubmit(chat, storage, body, setDraft) {
  const tree = ts.createSourceFile('useChatComposer.ts', read('useChatComposer.ts'), ts.ScriptTarget.Latest, true);
  let submit;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'submit') submit = node.initializer.arguments[0].getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree);
  const draftKey = 'draft';
  return vm.runInNewContext(compile(`(${submit})`), {...helpers('../../lib/chatComposer.ts'), draftKey, storage,
    previousKeyRef: {current: draftKey}, activeKeyRef: {current: draftKey}, draftRef: {current: body},
    pendingRef: {current: false}, sendEpoch: {current: 0}, restoredDraftRef: {current: null},
    announceUsageControl() {}, setPending() {}, setError() {}, setDraft,
    onSend: text => chat.context.sendChat(text), sendFailed: result => result === false});
}

test('native chat keeps an optimistic turn and saves it once when the same owner reconnects', async () => {
  const {api, ensureSession, clearVerifiedSession} = await import('../../lib/client.ts');
  const {apiErrorCode} = await import('../../lib/apiFailure.ts');
  const originalWindow = globalThis.window, originalStorage = globalThis.sessionStorage;
  try {
    for (const domain of ['Supplier bookkeeping', 'Weekend dance classes']) {
      clearVerifiedSession();
      const chat = macChat(), stored = new Map(), posts = [], history = [], optimistic = [];
      const storage = {getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key)};
      const body = `Make a working prototype of ${domain} with Codex`;
      const watch = '/v/ven_owned/device-work-orders/dwo_owned';
      let draft = body;
      globalThis.sessionStorage = storage;
      globalThis.window = {localStorage: storage, dispatchEvent() {}, setTimeout,
        __TAURI_INTERNALS__: {invoke: async (command, args) => {
          if (command === 'gateway_url') return 'http://127.0.0.1:3200';
          if (command === 'desktop_auth_status') return {state: 'connected'};
          assert.equal(command, 'desktop_api_request');
          if (args.path === '/auth/session') return {status: 200, data: {authenticated: true, subject: 'owner'}};
          assert.equal(args.path, '/api/ventures/ven_owned/chat');
          if (args.method === 'GET') return {status: 200, data: {items: history}};
          assert.equal(args.method, 'POST');
          optimistic.push(chat.messages.filter(message => message.role === 'founder' && message.body_md === body).length);
          posts.push(args);
          if (posts.length === 1) return {status: 401, data: {error: {code: 'account_changed_during_request'}}};
          history.push({id: 'saved_user', role: 'user', body_md: args.body.message, client_request_id: args.body.idempotency_key},
            {id: 'saved_reply', role: 'assistant', kind: 'navigation', body_md: 'Your build is already planned.',
              actions: [{label: 'Watch your tool', href: watch}], client_request_id: args.body.idempotency_key});
          return {status: 200, data: history.at(-1)};
        }}};
      assert.equal((await ensureSession(undefined, true)).ok, true);
      const {mergeChat} = helpers('../../lib/chatMerge.ts');
      Object.assign(chat.context, {window: globalThis.window, api, apiErrorCode, TextEncoder,
        crypto: require('node:crypto').webcrypto, checkpointScope: 'ven_owned', checkpointKey: 'checkpoint',
        chatRequestRef: {current: {attempt: {key: 'previous', inFlight: false, uncertain: true}}},
        resolveCheckpoint: async () => ({state: 'done', saved: {nonce: 'previous'}}),
        parseEmployeeStamp: text => ({text}), conversationResponseMatches: () => true, mergeChat,
        startRequestedToolBuild: async message => message, withLocalMacTaskObservation: async message => message,
        navigateOnResponse: false, apiFailure: () => 'Check your connection, then retry.',
        sendRefusal: () => ({role: 'system', body_md: 'Check your connection, then retry.'})});
      const sending = composerSubmit(chat, storage, body, value => {draft = value;})();
      assert.equal(draft, '');
      assert.equal(chat.messages[0]?.body_md, body);
      const accepted = await sending;
      assert.equal(posts.length, 2, 'The native bridge must retry the same keyed POST after the identity fence');
      assert.deepEqual(optimistic, [1, 1]);
      assert.equal(accepted, true);
      assert.deepEqual(posts[0], posts[1]);
      assert.equal(posts[0].headers['Idempotency-Key'], posts[0].body.idempotency_key);
      const saved = await api('/api/ventures/ven_owned/chat');
      const merged = mergeChat(chat.messages, saved.data.items, true);
      assert.deepEqual(Array.from(merged, message => message.id), ['saved_user', 'saved_reply']);
      assert.equal(merged[0].body_md, body);
      assert.equal(merged[1].actions[0].href, watch);
      assert.equal(draft, '');
      assert.equal(stored.has('draft'), false);
      assert.equal(stored.has('checkpoint'), false);
    }
  } finally {
    clearVerifiedSession();
    globalThis.window = originalWindow;
    globalThis.sessionStorage = originalStorage;
  }
});

test('chat failures replace internal reports with a real coding-tool setup step across domains', async () => {
  const {macTaskPrepareFailure} = await import('../../lib/macTaskPrepareFailure.ts');
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  const helper = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'sendRefusal');
  for (const domain of ['Factory maintenance', 'Family meal planning']) {
    const result = {ok: false, status: 409, data: {detail: {code: 'device_terminal_task_unavailable',
      message: 'Report a problem in Account settings and include code device_terminal_task_unavailable. Open Builds.'}}};
    const context = {Date, window: {__SUPYMEM__: {mode: 'local'}},
      apiErrorCode: value => value.data?.detail?.code, activeMacTaskReviewPath: () => undefined, macTaskPrepareFailure};
    vm.runInNewContext(compile(helper.getText(tree)), context);
    const card = context.sendRefusal(result, macTaskPrepareFailure(result), 'ven_fixture', 'Charlie', domain);
    assert.doesNotMatch(card.body_md, /Report a problem|include code|device_terminal_task_unavailable|Open Builds/);
    assert.match(card.body_md, /Settings > Keys and connections > Coding tools/);
    assert.equal(card.actions[0].href, '/settings/local');
  }
});

test('a rejected coding request restores the composer without a phantom sent turn across domains', async () => {
  for (const domain of ['Supplier bookkeeping', 'Weekend dance classes']) {
    const chat = macChat();
    const stored = new Map(), calls = [];
    const storage = {getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key)};
    Object.assign(chat.context, {window: {__SUPYMEM__: {mode: 'local'}, localStorage: storage},
      TextEncoder, crypto: require('node:crypto').webcrypto, checkpointKey: 'request', checkpointScope: 'ven_owned',
      chatRequestRef: {current: {attempt: null}}, resolveCheckpoint: async () => null,
      apiErrorCode: result => result.data?.detail?.code, apiFailure: () => 'Connect your tool in Settings.',
      macTaskPrepareFailure: () => 'Connect your tool in Settings.',
      sendRefusal: () => ({id: 'refused', role: 'assistant', body_md: 'Connect your coding tool in Settings, then send your request again.'}),
      api: async (url, init) => {calls.push({url, body: JSON.parse(init.body)}); return {ok: false, status: 409};}});
    const body = `Rebuild the landing page with Claude Code for ${domain}`;
    let draft = body;
    const send = composerSubmit(chat, storage, body, value => {draft = value;});
    assert.equal(await send(), false);
    assert.equal(draft, body);
    assert.equal(stored.get('draft'), body);
    assert.equal(calls[0].url, '/api/ventures/ven_owned/chat', 'Your rebuild must reach the connected coding-tool path');
    assert.equal(calls[0].body.message, body);
    assert.equal(chat.messages.some(message => message.role === 'founder'), false);
    assert.equal(chat.messages.at(-1).role, 'assistant');
    assert.equal(await chat.context.sendChat(body, {replyToQuestion: true}), false, 'An unanswered question stays available');
    assert.equal(chat.messages.at(-1).body_md, 'Connect your coding tool in Settings, then send your request again.');
  }
});

test('polish3: refused sends answer as Charlie with the exact reason and a safe next step across domains', async () => {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  const helper = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'sendRefusal');
  assert.ok(helper, 'Both send paths need the same Charlie refusal presentation');
  const apiErrorCode = result => result.data?.detail?.code || result.data?.error?.code;
  const {usageLimitMessage} = require('../../lib/credits.ts');
  for (const [ventureId, domain] of [['ven_dispatch', 'Dispatch teams'], ['ven_hiking', 'Hiking clubs']]) {
    const context = {Date, window: {__SUPYMEM__: {mode: 'local'}}, apiErrorCode, usageLimitMessage,
      activeMacTaskReviewPath: result => result.data?.detail?.review_path, AI_SETTINGS_HREF: '/settings/local#providers-title'};
    vm.runInNewContext(compile(helper.getText(tree)), context);
    for (const [reason, result, expected] of [
      ['Open Builds in your venture and check the saved result.', {status: 409}, 'status'],
      ['Open Settings > Keys and connections > Coding tools and connect your tool.', {status: 409}, '/settings/local'],
      ['Open Settings > Capabilities and turn on background work.', {status: 409}, '/settings/local/capabilities'],
      [usageLimitMessage(true), {status: 402, data: {detail: {message: 'Your credit balance is 0.'}}}, '/settings/local#providers-title'],
      ['Finish your current task first.', {status: 409, data: {detail: {code: 'device_task_has_active_order', review_path: `/v/${ventureId}/device-work-orders/dwo_current`}}}, `/v/${ventureId}/device-work-orders/dwo_current`],
      ['Your request needs a narrower scope. Edit your request and try again.', {status: 422}, null],
    ]) {
      const message = context.sendRefusal(result, reason, ventureId, 'Charlie', domain);
      assert.equal(message.role, 'assistant'); assert.equal(message.kind, 'needs_you');
      assert.equal(message.body_md, `<!--employee:Charlie:Manager:manager-->\n${expected === 'status'
        ? 'Choose Show status here below to review your latest build before retrying.' : reason}`);
      assert.equal(message.actions.length, 1); assert.equal(message.actions[0].style, 'primary');
      if (expected === 'status') assert.equal(message.actions[0].action, 'status');
      else if (expected) assert.equal(message.actions[0].href, expected);
      else {assert.equal(message.actions[0].action, 'chat.prefill'); assert.equal(message.actions[0].payload.text, domain);}
    }
    const foreign = context.sendRefusal({status: 409, data: {detail: {review_path: '/v/ven_someone_else/device-work-orders/dwo_current'}}}, 'Finish your current task first.', ventureId, 'Charlie', domain);
    assert.equal(foreign.actions[0].href, undefined);
    let refusal;
    function find(node) {
      if (ts.isIfStatement(node) && node.expression.getText(tree) === '!r.ok' && node.getText(tree).includes('sendRefusal')) refusal = node.thenStatement.getText(tree);
      ts.forEachChild(node, find);
    }
    find(tree); assert.ok(refusal);
    const replies = [], reason = 'Open Settings > Keys and connections and save your search key.';
    Object.assign(context, {r: {ok: false, status: 409, data: {detail: {message: reason}}}, requestAttempt: {uncertain: false},
      chatRequestRef: {current: {attempt: null}}, clearCheckpoint() {}, dropOut() {}, ventureId, body: domain,
      latestManagerName: () => 'Charlie', apiFailure: () => 'Generic fallback.', setMessages: update => replies.push(...update([]))});
    Object.assign(context, {opts: undefined});
    assert.equal(vm.runInNewContext(compile(`(() => ${refusal})()`), context), false);
    assert.equal(replies[0].body_md, `<!--employee:Charlie:Manager:manager-->\n${reason}`);
    assert.equal(replies[0].actions[0].href, '/settings/local');
    const chat = macChat();
    Object.assign(chat.context, {window: context.window, sendRefusal: context.sendRefusal, macTaskPrepareFailure: () => 'Open Builds in your venture and check the saved result.',
      api: async () => ({ok: false, status: 409})});
    assert.equal(await chat.context.sendChat(`On my Mac, read https://${ventureId}.example and summarize the page for ${domain}`), true);
    assert.equal(chat.messages.at(-1).role, 'assistant');
    assert.match(chat.messages.at(-1).body_md, /Show status here/);
    assert.equal(chat.messages.at(-1).actions[0].action, 'status');
  }
});

test('local browser sessions bypass the hosted-only task refusal across domains', () => {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let fallback;
  function visit(node) {
    if (ts.isIfStatement(node) && node.expression.getText(tree).startsWith('!isDesktopShell()') && node.getText(tree).includes('No Mac task was prepared')) fallback = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(fallback);
  for (const domain of ['Invoice scheduling', 'Garden planning']) for (const local of [false, true]) {
    let messages = [];
    vm.runInNewContext(compile(`(() => {${fallback}})()`), {Date, isDesktopShell: () => false,
      window: {__SUPYMEM__: {mode: local ? 'local' : 'hosted'}}, setMessages: update => {messages = update(messages);}});
    if (local) assert.equal(messages.length, 0);
    else assert.match(messages[0].body_md, /This task needs Supymem for Mac/);
    assert.ok(domain);
  }
});

test('local saved-request copy preserves your hosted warning and nonce across domains', () => {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let renderer;
  const visit = node => {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'showCheckpoint') renderer = node.initializer.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  };
  visit(tree);
  for (const domain of ['warehouse teams', 'weekend walkers']) for (const local of [false, true]) {
    let messages = [];
    const show = vm.runInNewContext(compile(`(${renderer})`), {
      window: {__SUPYMEM__: {mode: local ? 'local' : 'hosted'}},
      setMessages: update => {messages = update(messages);},
    });
    show({state: 'unknown', saved: {nonce: domain}, raw: domain});
    const notice = messages[0];
    assert.equal(notice.body_md.includes('unconfirmed'), !local, domain);
    assert.match(notice.body_md, /may still finish.*won't cancel.*Show status or start fresh/);
    assert.equal(notice.id, `local-uncertain-${domain}`);
    assert.equal(notice.kind, local ? 'needs_you' : 'confirmation');
    assert.equal(notice.actions[0].payload?.nonce, local ? domain : undefined);
    assert.equal(notice.actions.find(action => action.action === 'chat.startFresh').payload.checkpoint, domain);
  }
});

test('your request status checks its scoped receipt instead of sending a generic next-step request', async () => {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let renderer, run, poll;
  const visit = node => {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'showCheckpoint') renderer = node.initializer.arguments[0].getText(tree);
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'runChatAction') run = node.initializer.arguments[0].getText(tree);
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'poll' && node.initializer.getText(tree).includes('busyRef.current')) poll = node.initializer.getText(tree);
    ts.forEachChild(node, visit);
  };
  visit(tree);
  for (const domain of ['warehouse teams', 'weekend walkers']) {
    let messages = [], checks = 0;
    const checkpoint = JSON.stringify({nonce: 'original-request'});
    const context = {current: () => true, setMessages: update => {messages = update(messages);},
      window: {__SUPYMEM__: {mode: 'local'}, localStorage: {getItem: () => checkpoint}, clearTimeout() {}, setTimeout() {}}, checkpointKey: domain,
      load: async () => {checks++; return [];}, chatRequestRef: {current: {attempt: null}},
      api: async () => assert.fail('Your status check must not dispatch work or generic guidance'),
      flight: {current: {busy: () => false, begin: () => new AbortController(), finish() {}}},
      setThinking() {}, setNextMovesLoading() {}, Date, AbortController,
      local: false, live: true, inFlight: false, busyRef: {current: false}, timer: 1, delay: 8000, document: {hidden: false}};
    const show = vm.runInNewContext(compile(`(${renderer})`), context);
    show({state: 'unknown', saved: {nonce: 'original-request'}, raw: checkpoint});
    const status = messages[0].actions.find(action => action.label === 'Show status');
    assert.equal(status.payload?.nonce, 'original-request');
    const check = vm.runInNewContext(compile(`(${run})`), context);
    assert.equal(await check(status.action, status.payload), true);
    assert.equal(checks, 1);
    const refresh = vm.runInNewContext(compile(`(${poll})`), context);
    await refresh(); assert.equal(checks, 2);
    context.load = async () => undefined;
    assert.equal(await check(status.action, status.payload), false);
    context.window.__SUPYMEM__.mode = 'hosted';
    context.load = async () => {checks++; return [];};
    context.flight.current.busy = () => true;
    assert.equal(await check(status.action, status.payload), false);
    assert.equal(checks, 2, 'Hosted status keeps the production busy guard');
    let reads = 0;
    Object.assign(context, {ventureId: domain, conversationId: null, controller: new AbortController(),
      conversationChatPath: () => domain, apiLive: async () => {reads++; return {ok: false};}, setHistoryError() {}});
    await refresh();
    assert.equal(checks, 2); assert.equal(reads, 1, 'Hosted polling keeps its original history read');
  }
});

test('Start fresh is a scoped explicit action with a warning, not another approval, in both domains', async () => {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let branch, renderer;
  function find(node) {
    if (ts.isIfStatement(node) && node.expression.getText(tree) === 'action === "chat.startFresh"') branch = node.thenStatement.getText(tree);
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'showCheckpoint') renderer = node.initializer.arguments[0].getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(branch && renderer);
  for (const domain of ['Warehouse dispatch', 'Family garden planning']) {
    const checkpoint = JSON.stringify({nonce: `earlier-${domain}`}), key = `checkpoint-${domain}`;
    let stored = checkpoint, scopeCurrent = true, messages = [], approvals = 0;
    const calls = [], request = {current: {attempt: {inFlight: false}}};
    const context = {checkpointKey: key, chatRequestRef: request, current: () => scopeCurrent,
      crypto: {randomUUID: () => `fresh-${domain}`},
      window: {localStorage: {getItem: () => stored, removeItem: () => {stored = null;}}, confirm: () => {approvals++; return false;}},
      setMessages: update => {messages = update(messages);}, sendChat: (...args) => {calls.push(args); return true;}};
    const show = vm.runInNewContext(compile(`(${renderer})`), context);
    const fresh = vm.runInNewContext(compile(`(async (payload) => ${branch})`), context);
    show({state: 'unknown', saved: {nonce: domain}, raw: checkpoint}, {text: `Prepare your page for ${domain}.`});
    assert.match(messages[0].body_md, /may still finish/);
    assert.match(messages[0].body_md, /won't cancel/);
    const payload = messages[0].actions.find(action => action.action === 'chat.startFresh').payload;
    request.current.attempt.inFlight = true;
    assert.equal(await fresh(payload), false);
    request.current.attempt.inFlight = false; scopeCurrent = false;
    assert.equal(await fresh(payload), false);
    scopeCurrent = true;
    assert.equal(await fresh({...payload, checkpoint: 'changed'}), false);
    assert.equal(await fresh(payload), true);
    assert.equal(approvals, 0);
    assert.equal(stored, null);
    assert.equal(request.current.attempt, null);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], payload.text);
    assert.equal(calls[0][1].idempotencyKey, `fresh-${domain}`);
    assert.equal(await fresh(payload), false);
    assert.equal(calls.length, 1);
  }
});

test('unchanged history polls retain every bubble and the list identity in unrelated domains', () => {
  const {mergeChat} = helpers('../../lib/chatMerge.ts');
  const tree = ts.createSourceFile('chat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  const scopeState = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'useChatScopeState').getText(tree);
  for (const domain of ['Factory inventory planning', 'Family weekend walks']) {
    const founder = {id: 'question', role: 'user', body_md: domain, client_request_id: 'request', created_at: '2026-10-20T12:00:00Z'};
    const started = {id: 'started', role: 'assistant', kind: 'gtm_search_progress', body_md: `Your search for ${domain} is running.`, client_request_id: 'request', created_at: '2026-10-20T12:00:01Z'};
    const result = {id: 'result', role: 'assistant', kind: 'text', body_md: `Your results for ${domain} are ready.`, client_request_id: 'request', created_at: '2026-10-20T12:00:02Z'};
    const messages = mergeChat([], [founder, started, result], true);
    for (let poll = 0; poll < 3; poll++) {
      const saved = JSON.parse(JSON.stringify([founder, started, result]));
      assert.equal(mergeChat(messages, saved, true), messages, 'a no-content poll cannot replace selected bubbles');
    }
    let state;
    const context = vm.createContext({useRef: value => ({current: value}), useCallback: callback => callback,
      useState: value => {state = value; return [state, update => {const next = update(state); assert.equal(next, state); state = next;}];}});
    vm.runInContext(compile(scopeState + '; globalThis.scopeState = useChatScopeState;'), context);
    context.scopeState(domain, () => true, messages)[1](current => mergeChat(current, JSON.parse(JSON.stringify([founder, started, result])), true));
    const updated = {...result, body_md: `Your updated results for ${domain} are ready.`};
    const next = mergeChat(messages, [founder, started, updated], true);
    assert.notEqual(next, messages);
    assert.equal(next[0], messages[0], 'an actual change retains unrelated bubbles');
    assert.equal(next.at(-1).body_md, updated.body_md);
  }
});

test('a new search request settles at the end instead of following an older founder message across domains', async context => {
  const {mergeChat} = helpers('../../lib/chatMerge.ts');
  const ids = messages => Array.from(messages, message => message.id);
  for (const domain of ['Air quality monitoring for procurement teams', 'Watercolor classes for retirees']) {
    await context.test(domain, () => {
      const actions = [{label: 'Search again', action: 'gtm.find', payload: {audience: domain}}];
      for (const anchorRequest of ['typed-request', undefined]) {
        for (const olderRequest of ['typed-request', undefined]) {
          for (const olderActions of [[], actions]) {
            const founder = {id: 'typed-message', role: 'user', body_md: `Find matches for ${domain}.`,
              client_request_id: anchorRequest, created_at: '2026-11-02T10:00:00Z'};
            const older = {id: 'first-reply', role: 'assistant', body_md: `Your first matches for ${domain} are ready. Open your matches to review them.`,
              client_request_id: olderRequest, actions: olderActions, created_at: '2026-11-02T10:00:01Z'};
            const route = {id: 'route-reply', role: 'assistant', body_md: 'You can review your current matches before starting another search.',
              created_at: older.created_at};
            const progress = {id: 'new-progress', role: 'assistant', kind: 'gtm_search_progress',
              body_md: `Your search for ${domain} is running. You can review your saved matches while you wait.`,
              client_request_id: 'button-request', reply_to_message_id: founder.id,
              actions: [{label: 'View saved matches', href: '/matches/saved'}],
              citations: [{label: 'Your earlier matches', href: 'https://earlier.invalid/matches'}],
              created_at: '2026-11-02T10:00:04Z'};
            const result = {...progress, id: 'new-result', kind: 'text',
              body_md: `Your updated matches for ${domain} are ready. Open your matches to choose your next step.`,
              actions, citations: [{label: 'Your updated matches', href: 'https://updated.invalid/matches'}],
              created_at: '2026-11-02T10:03:04Z'};
            const history = [founder, older, route];
            const saved = [...history, progress, result];
            const live = mergeChat(mergeChat([], history, true), [progress], true);
            assert.deepEqual(ids(live), [...ids(history), progress.id]);
            for (const messages of [mergeChat(live, [result], true), mergeChat(live, saved, true),
              mergeChat([], JSON.parse(JSON.stringify(saved)), true)]) {
              assert.deepEqual(ids(messages), [...ids(history), result.id]);
              assert.deepEqual(JSON.parse(JSON.stringify(messages.at(-1))), result);
              assert.equal(messages.filter(message => message.client_request_id === result.client_request_id).length, 1);
              assert.equal(mergeChat(messages, JSON.parse(JSON.stringify(saved)), true), messages);
            }
          }
        }
      }
    });
  }
});

test('replayed started receipts keep their server request anchor instead of following later results across domains', async () => {
  const {chatFlight} = helpers('../../lib/chatStop.ts'), {mergeChat} = helpers('../../lib/chatMerge.ts');
  for (const domain of ['Factory inventory planning', 'Family weekend walks']) {
    const question = {id: 'question', role: 'user', body_md: domain, client_request_id: 'request', created_at: '2026-10-20T12:00:00Z'};
    const started = {id: 'started', role: 'assistant', body_md: 'Your work is starting. You can keep chatting.', client_request_id: 'request', created_at: '2026-10-20T12:00:01Z'};
    const result = {id: 'result', role: 'assistant', body_md: `Your result for ${domain} is ready.`, created_at: '2026-10-20T12:00:02Z'};
    const messages = [question, {...started, reply_to_message_id: result.id}, result];
    const context = vm.createContext({...helpers('../../lib/chatPrefill.ts'), ...helpers('../../lib/chatConversations.ts'),
      Date, crypto: require('node:crypto').webcrypto, ventureId: 'venture', conversationId: null,
      current: () => true, flight: {current: chatFlight()}, confirmationNonce: {current: null}, messagesRef: {current: messages},
      setThinking() {}, mergeChat, setMessages: update => messages.splice(0, messages.length, ...update(messages)),
      api: async () => ({ok: true, data: {...started, reply_to_message_id: question.id}}),
      window: {dispatchEvent() {}}, CustomEvent: class {}, startRequestedToolBuild: async message => message,
    });
    vm.runInContext('globalThis.run = ' + macChat().context.run.toString(), context);
    await context.run('research.start');
    assert.deepEqual(messages.map(message => message.id), [question.id, started.id, result.id]);
    const card = {id: 'card', role: 'assistant', kind: 'confirmation', body_md: domain, created_at: '2026-10-20T12:00:00.500Z'};
    const receipt = {...started, client_request_id: 'canonical-action', reply_to_message_id: question.id,
      confirmed_action: {message_id: card.id, action: 'research.start', state: 'started'}};
    assert.deepEqual(Array.from(mergeChat([question, card, result], [receipt], true), message => message.id),
      [question.id, card.id, started.id, result.id], 'a saved action receipt belongs after its exact card, not the generic founder line');
  }
});

test('chat reply retry carries its exact saved task through transport and request identity in unrelated domains', async () => {
  const source = read('useVentureChat.ts');
  const tree = ts.createSourceFile('useVentureChat.ts', source, ts.ScriptTarget.Latest, true);
  let replyBranch, requestPayload, canonicalPayload;
  function find(node) {
    if (ts.isIfStatement(node) && node.expression.getText(tree) === 'action === "chat.reply"') replyBranch = node.thenStatement.getText(tree);
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'requestPayload') requestPayload = node.initializer.getText(tree);
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'canonicalPayload') canonicalPayload = node.initializer.getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(replyBranch && requestPayload && canonicalPayload);
  for (const [domain, jobId] of [['freight dispatchers', 'dispatch-task'], ['garden tool sharing', 'garden-task']]) {
    const calls = [];
    const handler = vm.runInNewContext(compile(`(async (payload) => ${replyBranch})`), {sendChat: async (text, opts) => {calls.push({text, opts}); return true;}});
    assert.equal(await handler({text: `Help me safely retry my failed task for ${domain}.`, job_id: jobId}), true);
    assert.equal(calls[0].opts.jobId, jobId);
    const context = {opts: calls[0].opts, body: calls[0].text, conversationId: undefined};
    assert.equal(JSON.parse(vm.runInNewContext(compile(requestPayload), context)).job_id, jobId);
    assert.equal(JSON.parse(vm.runInNewContext(compile(canonicalPayload), context)).job_id, jobId);
  }
});

test('a fresh-round next step prefills the composer without starting work in either domain', async () => {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let branch;
  function find(node) {
    if (ts.isIfStatement(node) && node.expression.getText(tree) === 'action === "chat.prefill"' && ts.isBlock(node.thenStatement)) branch = node.thenStatement.getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(branch);
  for (const domain of ['freight dispatchers', 'garden tool sharing']) {
    const drafts = [];
    const handler = vm.runInNewContext(compile(`(async (payload) => ${branch})`), {conversations: {contextCurrent: () => true}, applyChatPrefill: text => drafts.push(text)});
    const text = `Review my saved result for ${domain} before starting a new round.`;
    assert.equal(await handler({text}), true);
    assert.deepEqual(drafts, [text]);
    assert.equal(await handler({text: ''}), false);
    assert.equal(await handler(null), false);
    assert.deepEqual(drafts, [text]);
  }
});

test('failed history loads keep polling, back off, and resume safely after backgrounding across domains', async () => {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let load, updates;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'load') load = node.initializer.arguments[0].getText(tree);
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect' && node.arguments[0].getText(tree).includes('busyRef.current')) updates = node.arguments[0].getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(load); assert.ok(updates);
  const { mergeChat } = helpers('../../lib/chatMerge.ts');
  const { conversationChatPath, conversationResponseMatches } = helpers('../../lib/chatConversations.ts');
  for (const domain of ['dispatchers at transport companies', 'neighbors sharing garden tools']) {
    let active = true, error = '', ready = false, mode = 'fail', finishRead;
    const messages = [], timers = new Map(), listeners = new Map(), calls = [];
    let timerId = 0;
    const surface = prefix => ({
      addEventListener: (event, callback) => listeners.set(prefix + event, callback),
      removeEventListener: event => listeners.delete(prefix + event),
    });
    const window = { ...surface('window:'), localStorage: {getItem: () => null}, setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; }, clearTimeout: id => timers.delete(id) };
    const document = { ...surface('document:'), hidden: false };
    const context = vm.createContext({ window, document, AbortController, current: () => active, isLocalConsole: () => false,
      ventureId: 'ven_saved', conversationId: 'cnv_saved', historyReady: true, busyRef: { current: false }, reasoningRef: { current: false },
      chatRequestRef: { current: { attempt: null } }, checkpointKey: domain, ensureSession: async () => {}, resolveCheckpoint: async () => null,
      conversationChatPath, conversationResponseMatches, mergeChat, parseChatItems: data => data.items,
      withLocalMacTaskObservation: async message => message, setNextHistoryCursor() {},
      setHistoryReady: value => { ready = value; }, setHistoryError: value => { error = value; },
      setMessages: update => messages.splice(0, messages.length, ...update(messages)),
      apiLive: async path => {
        calls.push(path);
        if (mode === 'fail') return { ok: false, status: 503 };
        if (mode === 'throw') throw new TypeError('offline');
        if (mode === 'pending') return new Promise(resolve => { finishRead = resolve; });
        return { ok: true, data: { conversation_id: 'cnv_saved', items: [{ id: 'saved', role: 'assistant', body_md: domain }] } };
      },
    });
    const flush = async () => { for (let turn = 0; turn < 30; turn++) await Promise.resolve(); };
    const readHistory = vm.runInContext(compile(`(${load})`), context);
    await readHistory(); assert.equal(ready, true); assert.match(error, /You.*Try again/);
    const cleanup = vm.runInContext(compile(`(${updates})()`), context);
    let timer = [...timers][0]; assert.equal(timer[1].delay, 8000);
    timers.delete(timer[0]); mode = 'throw'; timer[1].callback(); await flush();
    timer = [...timers][0]; assert.equal(timer[1].delay, 15000, 'A rejected read must schedule another attempt');
    mode = 'ok'; document.hidden = true;
    listeners.get('document:visibilitychange')(); assert.equal(messages.length, 0);
    document.hidden = false; listeners.get('window:online')(); await flush();
    assert.equal(error, ''); assert.equal(messages[0].body_md, domain);
    mode = 'pending'; listeners.get('window:focus')(); await flush();
    const before = calls.length; listeners.get('window:online')(); await flush(); assert.equal(calls.length, before, 'Do not overlap reads');
    active = false; cleanup(); finishRead({ ok: true, data: { conversation_id: 'cnv_saved', items: [{ id: 'retired', role: 'assistant', body_md: 'retired' }] } });
    await flush(); assert.equal(messages.length, 1); assert.equal(timers.size, 0); assert.equal(listeners.size, 0);
    assert.ok(calls.every(path => path === '/api/ventures/ven_saved/chat?conversation_id=cnv_saved'));
  }
});

test('local history paints before saved work details return across domains', async () => {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let load;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'load') load = node.initializer.arguments[0].getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree);
  const {mergeChat} = helpers('../../lib/chatMerge.ts');
  for (const body of ['Review supplier inventory', 'Plan weekend hikes']) {
    const saved = {id: 'saved', role: 'assistant', body_md: body};
    let ready = false, painted = [], finish;
    const loadHistory = vm.runInNewContext(compile(`(${load})`), {
      ventureId: 'ven_saved', conversationId: 'saved-chat', current: () => true, isLocalConsole: () => true,
      ensureSession: async () => {}, resolveCheckpoint: async () => null, chatRequestRef: {current: {attempt: null}},
      apiLive: async () => ({ok: true, data: {items: [saved]}}), conversationChatPath: () => '/chat',
      conversationResponseMatches: () => true, parseChatItems: data => data.items, mergeChat,
      setHistoryReady: value => {ready = value;}, setMessages: update => {painted = update(painted);},
      setNextHistoryCursor() {}, setHistoryError() {}, withLocalMacTaskObservation: () => new Promise(resolve => {finish = resolve;}),
    });
    const pending = loadHistory();
    await new Promise(resolve => setImmediate(resolve));
    try { assert.equal(ready, true); assert.equal(painted[0]?.body_md, body); }
    finally { finish({...saved, body_md: `${body}. Work verified.`}); await pending; }
    assert.equal(painted[0]?.body_md, `${body}. Work verified.`);
  }
});

test('local chat reads follow activity instead of timers while the stream is live', async context => {
  const effects = [['useVentureChat.ts', 'busyRef.current'], ['ChatDock.tsx', '/chat/work'], ['useChatConversations.ts', 'loadConversations(false']].map(([file, text]) => {
    const tree = ts.createSourceFile(file, read(file), ts.ScriptTarget.Latest, true);
    let effect;
    function find(node) {
      if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect' && node.arguments[0].getText(tree).includes(text)) effect = node.arguments[0].getText(tree);
      ts.forEachChild(node, find);
    }
    find(tree);
    return compile(`(${effect})()`);
  });
  const {conversationChatPath, conversationListPath} = helpers('../../lib/chatConversations.ts');
  const counts = [];
  for (const ventureId of ['ven_supply_chain', 'ven_weekend_hikes']) {
    const reads = [], timers = new Map();
    let now = 0, nextId = 0;
    const flush = () => new Promise(resolve => setImmediate(resolve));
    const schedule = (repeat, callback, delay) => {timers.set(++nextId, {at: now + delay, callback, repeat}); return nextId;};
    const clear = id => timers.delete(id);
    const events = {addEventListener() {}, removeEventListener() {}};
    const window = {...events, __SUPYMEM__: {mode: 'local'}, localStorage: {getItem: () => null},
      setTimeout: (...args) => schedule(0, ...args), clearTimeout: clear,
      setInterval: (callback, delay) => schedule(delay, callback, delay), clearInterval: clear};
    const document = {...events, hidden: false};
    const api = async path => {reads.push(path); return {ok: true, data: {items: []}};};
    const conversationId = 'saved-chat';
    const fixture = vm.createContext({window, document, AbortController, queueMicrotask, exports: {},
      require: () => ({createContext: () => ({})}), isLocalConsole: () => true, local: true,
      ventureId, conversationId, conversationChatPath, conversationResponseMatches: () => true,
      current: () => true, historyReady: true, visible: true, watchWork: true, voiceConversationId: undefined,
      onAction() {}, busyRef: {current: false}, reasoningRef: {current: false}, setMessages() {},
      setHistoryError() {}, checkpointKey: 'checkpoint', api, apiLive: api, parseChatItems: data => data.items,
      workKey: ventureId, activeJobs: {current: null}, stopEpoch: {current: 0}, owner: {actor: 'owner'},
      loadConversations: async () => {await api(conversationListPath(ventureId, conversationId)); return true;},
      clearTimeout: clear});
    vm.runInContext(compile(read('../../lib/activityRefresh.ts')), fixture);
    const feed = fixture.exports.createActivityRefresh(ventureId);
    Object.assign(fixture, {activity: feed, watchActivity: fixture.exports.watchActivity});
    feed.setConnected(true);
    const cleanup = effects.map(effect => vm.runInContext(effect, fixture));
    const count = () => ['chat?', 'chat/work?', 'chat/conversations?'].map(path => reads.filter(read => read.includes(`/${path}`)).length);
    const advance = async duration => {
      const end = now + duration;
      for (;;) {
        const entry = [...timers].filter(([, timer]) => timer.at <= end).sort(([, left], [, right]) => left.at - right.at)[0];
        if (!entry) break;
        const [id, timer] = entry;
        now = timer.at;
        if (timer.repeat) timer.at += timer.repeat; else timers.delete(id);
        timer.callback(); await flush();
      }
      now = end; await flush();
    };
    await flush(); reads.length = 0;
    await advance(20_000); const idle = count(); reads.length = 0;
    feed.changed(); await flush(); const event = count(); reads.length = 0;
    feed.setConnected(false);
    await advance(20_000); const down20s = count(); await advance(10_000); const down30s = count();
    counts.push({idle, event, down20s, down30s});
    cleanup.forEach(stop => stop());
    assert.equal(timers.size, 0);
  }
  context.diagnostic(`GETs [chat, work, conversations]: ${JSON.stringify(counts)}`);
  assert.deepEqual(counts, Array.from({length: 2}, () => ({idle: [0,0,0], event: [1,1,1], down20s: [0,0,0], down30s: [1,1,1]})));
});

function helpers(file) {
  const exports = {};
  vm.runInNewContext(compile(read(file)), {exports, AbortController, URLSearchParams});
  return exports;
}

test('every declared client action stays off chat confirm, including retired referrals and drafts across domains', async () => {
  const source = read('useVentureChat.ts');
  const tree = ts.createSourceFile('useVentureChat.ts', source, ts.ScriptTarget.Latest, true);
  const callbacks = {};
  function find(node) {
    if (ts.isVariableDeclaration(node) && ['runChatAction', 'confirmChatAction'].includes(node.name.getText(tree))) {
      callbacks[node.name.getText(tree)] = node.initializer.arguments[0].getText(tree);
    }
    ts.forEachChild(node, find);
  }
  find(tree);
  const declared = read('../../../../../services/company_os/app/chat/actions.py').match(/client_actions = \{([\s\S]*?)\}/)[1];
  const actions = [...declared.matchAll(/"([^"]+)"/g)].map(match => match[1]);
  const client = helpers('../../lib/chatPrefill.ts');
  const {webcrypto} = require('node:crypto');
  for (const [ventureId, audience] of [['ven_supply', 'Purchasing teams'], ['ven_books', 'Neighborhood book clubs']]) {
    for (const ready of [false, true]) {
      for (const conversationId of [null, 'cnv_saved']) {
        for (const action of actions) {
          assert.equal(client.isClientChatAction(action), action !== 'referrals.show', action);
          const events = [], calls = [], messages = [];
          let scopeCurrent = true;
          const values = new Map([['checkpoint', 'saved']]);
          const text = `Review the saved work for ${audience}.`;
          const payload = {text, checkpoint: 'saved', task_id: 'tsk_saved', proposal_id: 'proposal', quote_message_id: 'quote'};
          const context = vm.createContext({Date, Promise, setTimeout, crypto: webcrypto, exports: {}, ventureId, conversationId,
            current: () => ready, flight: {current: helpers('../../lib/chatStop.ts').chatFlight()},
            confirmationNonce: {current: null}, chatRequestRef: {current: {attempt: null}}, checkpointKey: 'checkpoint',
            messagesRef: {current: messages}, ...helpers('../../lib/chatTaskRun.ts'), ...helpers('../../lib/chatAnswered.ts'),
            setThinking() {}, setMessages: update => messages.splice(0, messages.length, ...update(messages)),
            conversations: {contextCurrent: () => scopeCurrent, newConversation: async () => {calls.push('new conversation'); return true;}},
            sendChat: async body => {calls.push(body); return true;}, load: async () => {},
            api: async url => {assert.ok(!url.endsWith('/chat/confirm'), action); calls.push(url); return {ok: true};},
            window: {dispatchEvent: event => events.push(event), confirm: () => true, localStorage: {
              getItem: key => values.get(key) ?? null, removeItem: key => values.delete(key)}},
            CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options.detail;}},
          });
          vm.runInContext(compile(read('../../lib/chatPrefill.ts')), context);
          Object.assign(context, context.exports);
          vm.runInContext(compile(`const runChatAction = ${callbacks.runChatAction}; globalThis.confirm = ${callbacks.confirmChatAction};`), context);
          const result = await context.confirm(action, payload);
          if (action === 'referrals.show') {
            assert.equal(result, false);
            assert.deepEqual(events, []);
            assert.deepEqual(calls, []);
          } else if (action === 'chat.prefill') {
            assert.equal(result, true);
            assert.equal(calls.length, 0);
            assert.deepEqual(events.map(event => event.type), ['supymem-chat-prefill']);
            assert.equal(events[0].detail.text, text);
            scopeCurrent = false;
            assert.equal(await context.confirm(action, payload), false);
            assert.equal(events.length, 1, 'a retired page or account must not receive a local action');
          } else if (!ready) {
            assert.equal(result, false);
            assert.equal(calls.length, 0);
          } else if (action.startsWith('entry.direction.')) {
            assert.equal(result, false);
            assert.equal(messages.at(-1).actions[0].href, `/new?venture=${ventureId}`);
          } else {
            assert.equal(result, true, action);
            assert.equal(calls.length, 1, action);
          }
          assert.equal(events.some(event => event.type === 'supymem:ask'), false, 'consuming a prefill must not redispatch an ask');
        }
      }
    }
  }
});

test('confirmed reconciliation removes only its uncertainty notice and keeps replies stacked across domains', () => {
  const {mergeChat} = helpers('../../lib/chatMerge.ts');
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let update;
  function find(node) {
    if (ts.isArrowFunction(node) && node.body.getText(tree).includes('const next = mergeChat(')) update = node.getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(update);
  for (const idea of ['Invoice reconciliation for purchasing teams', 'Meal planning for busy households']) {
    const requestKey = 'original-request';
    const first = {id: 'local-out-first', role: 'founder', body_md: idea, client_request_id: requestKey};
    const second = {id: 'local-out-second', role: 'founder', body_md: 'What happens next?', client_request_id: 'second-request'};
    const notice = {id: `local-uncertain-${requestKey}`, role: 'system', body_md: 'Your earlier request is unconfirmed.'};
    const other = {...notice, id: 'local-uncertain-other-request'};
    const grounded = {id: 'saved-answer', role: 'assistant', body_md: `Your request for ${idea} is saved.`};
    const settle = vm.runInNewContext(compile(`(${update})`), {mergeChat, grounded, requestKey, wantHint: false});
    const merged = settle([first, notice, second, other]);
    assert.deepEqual(Array.from(merged, message => message.id), [first.id, grounded.id, second.id, other.id]);
    assert.equal(merged[1].client_request_id, requestKey);
    assert.equal(merged[1].body_md, grounded.body_md);
    assert.deepEqual(Array.from(mergeChat(merged, [merged[1]], true), message => message.id),
      [first.id, grounded.id, second.id, other.id]);
  }
});

test('replies stay with their requests and pending follow-ups settle in one bubble across domains', async () => {
  const {mergeChat} = helpers('../../lib/chatMerge.ts');
  const {chatFlight} = helpers('../../lib/chatStop.ts');
  const {webcrypto} = require('node:crypto');
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let run;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'runChatAction') run = node.initializer.arguments[0].getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree);
  for (const idea of ['Invoice reconciliation for purchasing teams', 'Meal planning for busy households']) {
    const first = {id: 'founder-first', role: 'user', body_md: idea, client_request_id: 'first', created_at: '2026-10-28T10:00:00Z'};
    const pending = {id: 'pending-first', role: 'assistant', kind: 'idea_clarification_pending',
      body_md: 'Your answer is saved. Preparing the next question.', client_request_id: 'first', created_at: '2026-10-28T10:00:01Z'};
    const second = {id: 'founder-second', role: 'user', body_md: 'Explain the plan', client_request_id: 'second', created_at: '2026-10-28T10:00:02Z'};
    const secondReply = {id: 'reply-second', role: 'assistant', body_md: 'Your plan is saved.', client_request_id: 'second', created_at: '2026-10-28T10:00:03Z'};
    const completed = {id: 'ready-first', role: 'assistant', kind: 'idea_clarification_ready', body_md: 'Which problem should we solve first?',
      client_request_id: 'first', created_at: '2026-10-28T10:00:04Z', actions: [{label: 'Choose a problem', action: 'chat.reply'}],
      next_moves: {body_md: '', actions: [{label: 'Find customers', action: 'gtm.find'}]}};
    const ids = list => Array.from(list, message => message.id);
    const current = [first, pending, second, secondReply];
    const saved = [first, pending, second, secondReply, completed];
    const merged = mergeChat(current, saved, true);
    assert.deepEqual(ids(merged), [first.id, completed.id, second.id, secondReply.id]);
    assert.equal(merged.filter(message => message.role === 'assistant').length, 2);
    assert.deepEqual(merged[1].actions, completed.actions);
    assert.deepEqual(merged[1].next_moves, completed.next_moves);
    assert.deepEqual(ids(mergeChat(merged, saved, true)), ids(merged));
    assert.deepEqual(ids(mergeChat([first, second, secondReply], [completed], true)), ids(merged));
    assert.deepEqual(ids(mergeChat([first, second], [secondReply, completed], true)), ids(merged));
    const local = {...first, id: 'local-out-first', role: 'founder'};
    assert.deepEqual(ids(mergeChat([local, completed, second], saved, true)), ids(merged));
    assert.deepEqual(ids(mergeChat([local], [{...second, body_md: first.body_md}], true)), [local.id, second.id]);
    const skewed = {...second, id: 'local-out-second', role: 'founder', ts: '2026-10-28T09:00:00Z'};
    assert.deepEqual(ids(mergeChat([first, pending, skewed], [completed], true)), [first.id, completed.id, skewed.id]);
    const source = {id: 'option', role: 'assistant', body_md: 'Choose your next step', created_at: first.created_at};
    const optionReply = {...completed, id: 'option-reply', client_request_id: 'option-click',
      reply_to_message_id: source.id};
    assert.deepEqual(ids(mergeChat([source, second, secondReply], [optionReply], true)),
      [source.id, optionReply.id, second.id, secondReply.id]);
    const clicked = {...source, reply_request_ids: ['option-click']};
    const savedOptionReply = {...optionReply, reply_to_message_id: undefined};
    assert.deepEqual(ids(mergeChat([clicked, second, secondReply], [source, savedOptionReply], true)),
      [source.id, optionReply.id, second.id, secondReply.id]);
    const legacyPending = {...pending, client_request_id: undefined, reply_to_message_id: first.id};
    const legacyReady = {...completed, client_request_id: undefined, reply_to_message_id: first.id};
    assert.deepEqual(ids(mergeChat([first, legacyPending, second, secondReply], [legacyReady], true)), ids(merged));
    const clickedPending = {...pending, reply_request_ids: ['option-click']};
    const childReply = {...optionReply, reply_to_message_id: pending.id};
    assert.deepEqual(ids(mergeChat([first, clickedPending, second, secondReply], [completed, childReply], true)),
      [first.id, completed.id, childReply.id, second.id, secondReply.id]);
    const stamp = '<!--employee:Charlie:Manager:manager-->\n';
    const question = `${idea}\n\nWhich problem should we solve first?`;
    const original = {...completed, id: 'original-question', created_at: pending.created_at, body_md: stamp + question};
    const followup = {...completed, body_md: stamp + `Your answer is saved.\n\n${question}`};
    const combined = mergeChat([first, original, second, secondReply], [followup], true);
    assert.equal(combined[1].body_md, followup.body_md);
    assert.equal(mergeChat(combined, [original], true)[1].body_md, followup.body_md);
    assert.equal(mergeChat(combined, [original, followup], true)[1].body_md, followup.body_md);
    const messages = [source, second, secondReply], flight = {current: chatFlight()};
    let finish, request;
    const context = vm.createContext({...helpers('../../lib/chatPrefill.ts'), Date, crypto: webcrypto, ventureId: 'venture', conversationId: null, current: () => true,
      messagesRef: {current: messages}, flight, confirmationNonce: {current: null}, setThinking() {}, mergeChat,
      setMessages: update => messages.splice(0, messages.length, ...update(messages)),
      api: async (_url, init) => {request = JSON.parse(init.body); return new Promise(resolve => {finish = resolve;});},
    });
    vm.runInContext(compile('globalThis.run = ' + run + ';'), context);
    const waiting = context.run('gtm.find', {prepare: true, reply_to_message_id: source.id});
    assert.equal(request.payload.reply_to_message_id, undefined);
    assert.deepEqual(Array.from(messages[0].reply_request_ids), [request.idempotency_key]);
    assert.equal(messages[2].reply_request_ids, undefined);
    flight.current.stop();
    const newer = {...second, id: 'newer-founder', client_request_id: 'newer', created_at: '2026-10-28T10:00:05Z'};
    messages.push(newer);
    const late = {...completed, id: 'late-option', client_request_id: request.idempotency_key};
    finish({ok: true, data: late});
    assert.equal(await waiting, false);
    assert.deepEqual(ids(mergeChat(messages, [source, late], true)), [source.id, late.id, second.id, secondReply.id, newer.id]);
  }
});

function macChat() {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  const callbacks = {};
  function find(node) {
    if (ts.isVariableDeclaration(node) && ['sendChat', 'runChatAction'].includes(node.name.getText(tree))) {
      callbacks[node.name.getText(tree)] = node.initializer.arguments[0].getText(tree);
    }
    ts.forEachChild(node, find);
  }
  find(tree);
  const requests = {};
  vm.runInNewContext(compile(read('../../lib/reviewableDeviceRequest.ts')), {
    exports: requests, require: () => helpers('../../lib/desktopAppMention.ts'),
  });
  const messages = [], calls = [];
  let thinking = false;
  const context = vm.createContext({ ...requests, AbortController,
    window: {}, load: async () => [],
    ventureId: 'ven_owned', conversationId: null, current: () => true,
    flight: {current: helpers('../../lib/chatStop.ts').chatFlight()},
    setThinking: value => {thinking = value;},
    setMessages: update => messages.splice(0, messages.length, ...update(messages)),
    messagesRef: {current: messages}, isDesktopShell: () => true, verifiedSessionEpoch: () => 1,
    latestManagerName: () => 'Charlie', ...helpers('../../lib/desktopExecutionAvailability.ts'),
    desktopInvoke: async () => assert.fail('Browser or unavailable native tasks must not inspect permissions'),
    selectDesktopApplication: async () => assert.fail('Browser or unavailable native tasks must not select a native app'),
    api: async (url, init) => {
      calls.push({url, body: init ? JSON.parse(init.body) : null});
      if (url.endsWith('/device-desktop-pause')) return {ok: true, data: {paused: false, execution_available: false}};
      assert.ok(url.endsWith('/device-work-orders/from-chat'), url);
      return {ok: true, data: {id: 'dwo_owned', review_path: '/v/ven_owned/device-work-orders/dwo_owned'}};
    },
  });
  const digest = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'chatPayloadDigest').getText(tree);
  vm.runInContext(compile(`${digest}\nglobalThis.sendChat = ${callbacks.sendChat}; globalThis.run = ${callbacks.runChatAction};`), context);
  return {context, requests, messages, calls, thinking: () => thinking};
}

test('a local send reads the saved turn and replaces its optimistic bubble without another activity event', async () => {
  const {webcrypto: crypto} = require('node:crypto');
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let load;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'load') load = node.initializer.arguments[0].getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(load);
  for (const domain of ['Commercial inventory', 'Weekend baking']) {
    const chat = macChat(), storage = new Map();
    const body = `Rebuild my landing page for ${domain} with Codex`;
    const reply = {id: 'requested', role: 'assistant', kind: 'text', body_md: 'Your private request is saved.',
      actions: [{label: 'Show status here', action: 'status', payload: {job_id: 'page_job'}}]};
    const preview = {id: 'preview', role: 'assistant', kind: 'site_preview', body_md: 'Your private preview is ready.',
      actions: [{label: 'Open preview', href: '/api/ventures/ven_owned/pagebuilder/jobs/page_job/preview'}]};
    const next = {id: 'next', role: 'assistant', kind: 'next_moves', body_md: 'You can review your draft.',
      actions: [{label: 'Review your draft', action: 'gtm.status'}]};
    let items, writes = 0, reads = 0;
    Object.assign(chat.context, {crypto, TextEncoder, isDesktopShell: () => false, isLocalConsole: () => true,
      window: {__SUPYMEM__: {mode: 'local'}, localStorage: {
        getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)}},
      checkpointScope: 'ven_owned', checkpointKey: 'checkpoint', chatRequestRef: {current: {attempt: null}},
      resolveCheckpoint: async () => null, ensureSession: async () => ({ok: true}),
      ...helpers('../../lib/chatMerge.ts'), ...helpers('../../lib/chatConversations.ts'),
      parseEmployeeStamp: text => ({text}), parseChatItems: data => data.items,
      setNextHistoryCursor() {}, setHistoryError() {}, setHistoryReady() {}, navigateOnResponse: false,
      startRequestedToolBuild: async message => message, withLocalMacTaskObservation: async message => message,
      api: async (path, init) => {
        assert.equal(path, '/api/ventures/ven_owned/chat'); assert.equal(init.method, 'POST');
        assert.equal(chat.messages.filter(message => message.role === 'founder').length, 1);
        writes++;
        const nonce = JSON.parse(init.body).idempotency_key;
        items = [{id: 'saved_user', role: 'user', body_md: body, client_request_id: nonce},
          {...reply, client_request_id: nonce}, preview, next];
        return {ok: true, data: {...reply, body_md: 'Your request is queued.', actions: []}};
      },
      apiLive: async path => {
        assert.equal(path, '/api/ventures/ven_owned/chat'); reads++;
        return {ok: true, data: {items}};
      }});
    vm.runInContext(compile(`globalThis.load = ${load};`), chat.context);
    assert.equal(await chat.context.sendChat(body), true);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(writes, 1);
    assert.equal(reads, 1);
    assert.deepEqual(JSON.parse(JSON.stringify(chat.messages)), items);
  }
});

test('requested coding builds post and start without a native bridge but keep hosted browser refusal across domains', async () => {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  const start = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'startRequestedToolBuild').getText(tree);
  for (const domain of ['RotorProbe', 'TrailNest']) {
    for (const instruction of [`Build my landing page for ${domain} with Codex`,
      `Rebuild my landing page for ${domain} with Claude Code`,
      `Build the first version of ${domain} with Codex`,
      'Build the first version of my product with Claude Code',
      `Make a working prototype of ${domain} with Codex`]) for (const [mode, desktop] of [['local', false], ['local', true], ['hosted', false]]) {
      if (mode === 'hosted' && !instruction.includes('landing page')) continue;
      const chat = macChat();
      const values = new Map();
      chat.context.window = {__SUPYMEM__: {mode}, localStorage: {
        getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)}};
      Object.assign(chat.context, {TextEncoder, crypto: require('node:crypto').webcrypto, checkpointScope: 'ven_owned', checkpointKey: 'checkpoint',
        chatRequestRef: {current: {attempt: null}}, resolveCheckpoint: async () => null,
        conversationResponseMatches: () => true, ...helpers('../../lib/chatMerge.ts'),
        parseEmployeeStamp: text => ({text}), withLocalMacTaskObservation: async message => message,
        navigateOnResponse: false});
      chat.context.isDesktopShell = () => desktop;
      chat.context.verifiedSessionSubject = () => 'owner';
      chat.context.desktopInvoke = async () => {
        chat.calls.push({command: 'unavailable'});
        return null;
      };
      chat.context.api = async (url, init) => {
        chat.calls.push({url, body: init?.body ? JSON.parse(init.body) : null, method: init?.method});
        if (url.endsWith('/chat')) return {ok: true, data: {id: 'build_reply', role: 'assistant', kind: 'build_tool_started',
          body_md: `Your build for ${domain} uses your connected coding tool. Open Build activity to watch or stop it.`,
          actions: [{href: '/v/ven_owned/device-work-orders/dwo_owned', payload: {order_id: 'dwo_owned'}}]}};
        if (url === '/api/ventures/ven_owned/device-work-orders/dwo_owned') return {ok: true, data: {
          id: 'dwo_owned', venture_id: 'ven_owned', state: 'approved', requested_build: true}};
        assert.equal(url, '/native/device-work-orders/ven_owned/dwo_owned/start');
        assert.equal(init.method, 'POST');
        return {ok: true, data: {state: 'running'}};
      };
      vm.runInContext(compile(start), chat.context);
      assert.equal(await chat.context.sendChat(instruction), true);
      if (mode === 'hosted') {
        assert.equal(chat.calls.length, 0);
        assert.equal(chat.messages.at(-1).body_md,
          'This task needs Supymem for Mac. Open this venture in the Mac app and ask for the task there. No Mac task was prepared or started.');
      } else {
        assert.equal(chat.calls.length, 3, 'Your local build must create an order, verify it, and start your tool');
        assert.equal(chat.calls[0].url, '/api/ventures/ven_owned/chat');
        assert.equal(chat.calls[0].method, 'POST');
        assert.equal(chat.calls[0].body.message, instruction);
        assert.equal(chat.calls[1].url, '/api/ventures/ven_owned/device-work-orders/dwo_owned');
        assert.equal(chat.calls[2].url, '/native/device-work-orders/ven_owned/dwo_owned/start');
        assert.equal(values.size, 0, 'A confirmed send must not leave an unknown checkpoint');
        assert.equal(chat.messages.at(-1).kind, 'build_tool_started');
        assert.equal(chat.messages.at(-1).actions[0].href, '/v/ven_owned/device-work-orders/dwo_owned');
        assert.doesNotMatch(chat.messages.at(-1).body_md, /approve|confirm|cannot control/);
      }
      assert.equal(chat.thinking(), false);
    }
  }
});

test('a local build blocked before chat dispatch offers Retry without leaving an unknown send across domains', async () => {
  for (const audience of ['Wholesale inventory teams', 'Weekend hikers']) {
    const chat = macChat();
    const instruction = `Build a landing page for ${audience} with Codex`;
    Object.assign(chat.context, {
      window: {__SUPYMEM__: {mode: 'local'}, localStorage: {setItem() { throw new Error('Storage unavailable'); }}},
      isDesktopShell: () => false, TextEncoder, crypto: require('node:crypto').webcrypto,
      checkpointScope: 'ven_owned', checkpointKey: 'checkpoint', chatRequestRef: {current: {attempt: null}},
      resolveCheckpoint: async () => null,
    });
    assert.equal(await chat.context.sendChat(instruction), false);
    assert.equal(chat.calls.length, 0);
    assert.equal(chat.messages.at(-1).actions[0].label, 'Retry');
    assert.equal(chat.messages.at(-1).actions[0].payload.text, instruction);
    assert.equal(chat.context.chatRequestRef.current.attempt, null);
    assert.equal(chat.thinking(), false);
  }
});

test('local page and code changes reach the console server send path without preparing Mac tasks across domains', async () => {
  for (const desktop of [false, true]) for (const instruction of [
    'Rebuild my landing page with Claude Code',
    'Change the headline on my page with Codex',
    'Use Cursor to rebuild my page',
    'build the first version of my product with Claude Code',
    'update my site',
    'Redo the warehouse scheduling website with OpenCode',
    'Fix the family recipe app with Cursor on this Mac',
    'Improve the code for supplier invoices with Codex',
    'Edit the page for home gardening with Claude Code',
    'Create a scraper for wholesale catalogues with OpenCode',
    'Write a small experiment tool for music lessons with Codex',
    'Use my Mac to open the website and change its headline with Codex',
  ]) {
    const chat = macChat(), values = new Map();
    Object.assign(chat.context, {window: {__SUPYMEM__: {mode: 'local'}, localStorage: {
      getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)}},
      TextEncoder, crypto: require('node:crypto').webcrypto, checkpointScope: 'ven_owned', checkpointKey: 'checkpoint',
      chatRequestRef: {current: {attempt: null}}, resolveCheckpoint: async () => null,
      conversationResponseMatches: () => true, ...helpers('../../lib/chatMerge.ts'),
      parseEmployeeStamp: text => ({text}), startRequestedToolBuild: async message => message,
      withLocalMacTaskObservation: async message => message, navigateOnResponse: false,
      isDesktopShell: () => desktop,
      api: async (url, init) => {
        chat.calls.push({url, body: init?.body ? JSON.parse(init.body) : null});
        assert.equal(url, '/api/ventures/ven_owned/chat');
        return {ok: true, data: {id: 'reply', role: 'assistant', body_md: 'Your coding tool is ready. Open Build activity to watch it.'}};
      }});
    assert.equal(await chat.context.sendChat(instruction), true, instruction);
    assert.equal(chat.calls.length, 1, instruction);
    assert.equal(chat.calls[0].body.message, instruction);
    assert.equal(chat.messages.at(-1).id, 'reply');
    assert.equal(chat.thinking(), false);
  }
  const chat = macChat();
  chat.context.window = {__SUPYMEM__: {mode: 'local'}};
  assert.equal(await chat.context.sendChat('Open Notes on this Mac'), true);
  assert.equal(chat.calls[0].url, '/api/ventures/ven_owned/device-desktop-pause');
});

test('local browser build starts retain saved order and session verification across domains', async () => {
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  const start = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'startRequestedToolBuild').getText(tree);
  for (const domain of ['Warehouse shipment scheduling', 'Home cooking lessons']) {
    for (const failure of ['order_id', 'venture_id', 'requested_build', 'saved_request', 'session', 'scope', 'bridge']) {
      let epoch = 1, current = true;
      const calls = [];
      const message = {kind: 'build_tool_started', body_md: domain,
        actions: [{href: '/v/ven_owned/device-work-orders/dwo_owned', payload: {order_id: 'dwo_owned'}}]};
      const context = {window: {__SUPYMEM__: {mode: 'local'}}, isDesktopShell: () => false,
        verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => epoch,
        api: async (url, init) => {
          calls.push({url, method: init?.method});
          if (init) return {ok: false};
          const order = {id: 'dwo_owned', venture_id: 'ven_owned', state: 'approved', requested_build: true};
          if (failure === 'order_id') order.id = 'dwo_foreign';
          if (failure === 'venture_id') order.venture_id = 'ven_foreign';
          if (failure === 'requested_build') order.requested_build = false;
          if (failure === 'session') epoch++;
          if (failure === 'scope') current = false;
          return {ok: failure !== 'saved_request', data: order};
        }};
      vm.runInNewContext(compile(start), context);
      const reply = await context.startRequestedToolBuild(message, 'ven_owned', () => current);
      assert.equal(calls.length, failure === 'bridge' ? 2 : 1);
      if (failure === 'session' || failure === 'scope') assert.equal(reply, message);
      else {
        assert.equal(reply.kind, 'text');
        assert.match(reply.body_md, /Watch your tool below to check its status before retrying/);
      }
    }
  }
});

test('Mac website requests including whole-page reading bypass native availability and preserve the exact goal', async () => {
  const fixture = JSON.parse(read('../../../../../tests/fixtures/device_chat_intent_parity.json'));
  const {requests} = macChat();
  for (const phrase of fixture.phrases) {
    assert.equal(requests.reviewableDeviceRequest(phrase.text), phrase.client_reviewable, phrase.text);
  }
  const goals = [
    'On my Mac, open https://en.wikipedia.org/wiki/Houseplant and give me the key points from the whole page, including the sections near the bottom',
    'Could you please use my Mac to read https://www.atlassian.com/software/jira and summarize every section?',
    'Read https://en.wikipedia.org/wiki/Houseplant on my Mac, including the bottom sections',
    'On this Mac, open the website at https://www.atlassian.com/software/jira and compare its plans',
  ];
  for (const goal of goals) {
    for (const action of [false, true]) {
      const chat = macChat();
      assert.equal(chat.requests.browserTaskRequest(goal), true, goal);
      if (action) await chat.context.run('mac.task.prepare', {text: goal});
      else assert.equal(await chat.context.sendChat(goal), true);
      assert.deepEqual(chat.calls, [{url: '/api/ventures/ven_owned/device-work-orders/from-chat', body: {instruction: goal}}]);
      const reply = chat.messages.at(-1);
      assert.equal(reply.role, 'assistant');
      assert.equal(reply.kind, 'confirmation');
      assert.equal(reply.actions[0].href, '/v/ven_owned/device-work-orders/dwo_owned');
      assert.match(reply.body_md, /Nothing runs.*until.*review and approve/);
      assert.equal(chat.thinking(), false);
    }
  }
  for (const goal of ['On my Mac, open the website for business billing plans', 'Use my Mac to read the site for indoor gardening']) {
    const chat = macChat();
    assert.equal(chat.requests.browserTaskRequest(goal), true, goal);
    assert.equal(await chat.context.sendChat(goal), true);
    assert.deepEqual(chat.calls, []);
    assert.equal(chat.messages.at(-1).role, 'assistant');
    assert.match(chat.messages.at(-1).body_md, /Which website.*HTTPS/);
  }
  for (const goal of ['Do not open https://www.atlassian.com/software/jira on my Mac',
    'The note says "On my Mac, open https://en.wikipedia.org/wiki/Houseplant"',
    'Use my Mac to open TextEdit and type https://www.atlassian.com/software/jira',
    'Use my Mac to open Photos and search for https://en.wikipedia.org/wiki/Houseplant',
    'Open my website for business billing plans', 'Read my site for indoor gardening']) {
    assert.equal(requests.browserTaskRequest(goal), false, goal);
  }
});

test('unavailable native tasks answer as Charlie with a working manual next step, including prepare actions', async () => {
  for (const goal of ['Use my Mac to open TextEdit and read the project checklist',
    'Use my Mac to open Photos and inspect the holiday album']) {
    for (const action of [false, true]) {
      const chat = macChat();
      if (action) await chat.context.run('mac.task.prepare', {text: goal});
      else assert.equal(await chat.context.sendChat(goal), true);
      assert.deepEqual(chat.calls.map(call => call.url), ['/api/ventures/ven_owned/device-desktop-pause']);
      const reply = chat.messages.at(-1);
      assert.equal(reply.role, 'assistant');
      assert.match(reply.body_md, /<!--employee:Charlie:Manager:manager-->/);
      assert.match(reply.body_md, /manual steps/i);
      assert.ok(!reply.body_md.includes('\u2014'));
      const next = reply.actions.find(item => item.action === 'chat.reply');
      assert.ok(next.payload.text.includes(goal));
      assert.equal(chat.requests.reviewableDeviceRequest(next.payload.text), false);
      chat.context.sendChat = async text => {chat.calls.push({manual: text}); return true;};
      assert.equal(await chat.context.run(next.action, next.payload), true);
      assert.deepEqual(chat.calls.at(-1), {manual: next.payload.text});
      assert.equal(chat.thinking(), false);
    }
  }
});

test('an unavailable saved-chat list recovers on its next poll or reconnect without refreshing across domains', async () => {
  const tree = ts.createSourceFile('useChatConversations.ts', read('useChatConversations.ts'), ts.ScriptTarget.Latest, true);
  let load, bootstrap;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'loadConversations') load = node.initializer.arguments[0].getText(tree);
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect' && node.arguments[0].getText(tree).includes('loadConversations(false')) bootstrap = node.arguments[0].getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(load); assert.ok(bootstrap);
  const { chatConversationCatalog, conversationListPath, conversationSelectionKey } = helpers('../../lib/chatConversations.ts');
  for (const domain of ['quality teams at manufacturers', 'cyclists planning local rides']) {
    const listeners = new Map(), timers = new Map();
    let timerId = 0, active = true, failing = true, model = null, reads = 0;
    const surface = prefix => ({ addEventListener: (name, callback) => listeners.set(prefix + name, callback), removeEventListener: name => listeners.delete(prefix + name) });
    const document = { ...surface('document:'), hidden: false };
    const window = { ...surface('window:'), sessionStorage: { getItem: () => null } };
    const context = vm.createContext({ window, document, AbortController, current: () => active, isLocalConsole: () => false,
      owner: { actor: 'usr_saved', workspace: null }, ventureId: 'ven_saved',
      selection: { current: null }, loads: { current: [0, 0] }, counter: { current: 0 }, composerVersion: { current: 0 }, flight: { current: null },
      chatConversationCatalog, conversationListPath, conversationSelectionKey,
      setState: update => { model = typeof update === 'function' ? update(model) : update; },
      setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; }, clearTimeout: id => timers.delete(id),
      apiLive: async () => {
        reads++;
        return failing ? { ok: false, status: 503 } : { ok: true, data: {
          venture_id: 'ven_saved', workspace_id: 'wsp_saved', actor_id: 'usr_saved', legacy_conversation_id: 'chat_legacy',
          items: [{ id: 'chat_legacy', venture_id: 'ven_saved', workspace_id: 'wsp_saved', visibility: 'legacy_shared', title: domain, created_at: null, updated_at: null }],
        } };
      },
    });
    vm.runInContext(compile(`globalThis.loadConversations = ${load};`), context);
    const cleanup = vm.runInContext(compile(`(${bootstrap})()`), context);
    const flush = async () => { for (let turn = 0; turn < 30; turn++) await Promise.resolve(); };
    await flush(); assert.equal(model.ready, false); assert.match(model.error, /You.*Try again/);
    const [id, timer] = [...timers][0]; assert.equal(timer.delay, 15000);
    timers.delete(id); timer.callback(); await flush(); assert.equal(reads, 2);
    failing = false; document.hidden = true; listeners.get('window:focus')(); await flush(); assert.equal(reads, 2);
    document.hidden = false; listeners.get('window:online')(); await flush();
    assert.equal(model.ready, true); assert.equal(model.error, ''); assert.equal(model.selected, 'chat_legacy');
    assert.equal(model.catalog.items[0].title, domain); assert.equal(timers.size, 0);
    active = false; cleanup(); assert.equal(listeners.size, 0);
  }
});

test('stopping a foreground confirmation preserves an unrelated saved research request across domains', async () => {
  const {chatFlight} = helpers('../../lib/chatStop.ts');
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let callback;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'runChatAction') {
      callback = node.initializer.arguments[0].getText(tree);
    }
    ts.forEachChild(node, find);
  }
  find(tree);
  for (const [ventureId, idea] of [['ven_stock', 'Stock counts for pharmacies'], ['ven_trails', 'Trail planning for hikers']]) {
    const messages = [], calls = [], values = new Map();
    const checkpointKey = `checkpoint:${ventureId}`;
    const saved = JSON.stringify({nonce: 'research-turn', scope: ventureId});
    values.set(checkpointKey, saved);
    const request = {key: 'research-turn', payload: JSON.stringify({idea}), inFlight: false, uncertain: true};
    const chatRequestRef = {current: {attempt: request}};
    const flight = {current: chatFlight()};
    flight.current.begin();
    const confirmationNonce = {current: 'next-turn'};
    const context = vm.createContext({Date, JSON, encodeURIComponent, ventureId, conversationId: 'private-chat',
      current: () => true, flight, confirmationNonce, chatRequestRef, checkpointKey, setThinking() {},
      setMessages: update => messages.splice(0, messages.length, ...update(messages)),
      window: {localStorage: {getItem: key => values.get(key) ?? null, removeItem: key => values.delete(key)}},
      api: async (url, init) => {calls.push({url, body: JSON.parse(init.body)}); return {ok: true};},
    });
    vm.runInContext(compile(`globalThis.run = ${callback};`), context);
    assert.equal(await context.run('chat.stop', {job_ids: []}), true);
    assert.equal(chatRequestRef.current.attempt, request);
    assert.equal(values.get(checkpointKey), saved);
    assert.deepEqual(calls[0].body.job_ids, []);
    assert.equal(calls[0].body.nonce, 'next-turn');
    assert.equal(flight.current.busy(), false);
    confirmationNonce.current = 'research-turn';
    assert.equal(await context.run('chat.stop', {job_ids: []}), true);
    assert.equal(chatRequestRef.current.attempt, null);
    assert.equal(values.has(checkpointKey), false);
  }
});

test('your local chat limit links to your AI settings without requesting hosted purchase guidance', async () => {
  const {webcrypto} = require('node:crypto');
  const {chatFlight} = helpers('../../lib/chatStop.ts');
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let handler;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'runChatAction') handler = node.initializer.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  for (const domain of ['Invoice scheduling', 'Garden planning']) {
    const messages = [], calls = [];
    const context = {...helpers('../../lib/chatPrefill.ts'), ...require('../../lib/credits.ts'), Date, Promise, crypto: webcrypto,
      ventureId: domain, conversationId: null, current: () => true, flight: {current: chatFlight()}, confirmationNonce: {current: null},
      messagesRef: {current: messages}, setThinking() {}, setMessages: update => messages.splice(0, messages.length, ...update(messages)),
      window: {__SUPYMEM__: {mode: 'local'}, dispatchEvent() {}},
      api: async (_url, init) => {calls.push(JSON.parse(init.body).action); return {ok: false, status: 402};},
    };
    const run = vm.runInNewContext(compile(`(${handler})`), context);
    assert.equal(await run('research.start', {domain}), false);
    assert.deepEqual(calls, ['research.start']);
    assert.match(messages[0].body_md, /Your service account has reached its limit/);
    assert.equal(messages[0].actions[0].href, '/settings/local#providers-title');
    assert.doesNotMatch(messages[0].body_md, /Buy more|credits|\u2014/);
  }
});

test('exhausted usage shows plain recovery and releases the failed confirmation button across unrelated domains', async () => {
  const {webcrypto} = require('node:crypto');
  const {chatFlight} = helpers('../../lib/chatStop.ts');
  const {conversationResponseMatches} = helpers('../../lib/chatConversations.ts');
  const {failedSince, settleClick} = helpers('../../lib/chatAnswered.ts');
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  const callbacks = {};
  function find(node) {
    if (ts.isVariableDeclaration(node) && ['runChatAction', 'confirmChatAction'].includes(node.name.getText(tree))) {
      callbacks[node.name.getText(tree)] = node.initializer.arguments[0].getText(tree);
    }
    ts.forEachChild(node, find);
  }
  find(tree);
  for (const [ventureId, action, audience] of [['ven_freight', 'pagebuilder.build', 'freight billing teams'],
    ['ven_cooking', 'research', 'home cooks sharing recipes']]) {
    const messages = [], calls = [], thinking = [], confirmationNonce = {current: null};
    const flight = {current: chatFlight()};
    const recovery = {id: `recovery-${ventureId}`, role: 'assistant', conversation_id: 'cnv_review',
      body_md: 'You have reached your usage limit. Open Settings to get more, then retry.'};
    const context = vm.createContext({...helpers('../../lib/chatPrefill.ts'), Date, Promise, setTimeout, crypto: webcrypto, ventureId,
      conversationId: 'cnv_review', current: () => true, flight, confirmationNonce,
      conversationResponseMatches, failedSince, messagesRef: {current: messages}, ...helpers('../../lib/chatMerge.ts'),
      setThinking: value => thinking.push(value),
      setMessages: update => messages.splice(0, messages.length, ...update(messages)),
      window: {dispatchEvent() {}}, CustomEvent: class {}, ...require('../../lib/chatConfirmError.ts'),
      startRequestedToolBuild: async message => message,
      api: async (_url, init) => {
        const body = JSON.parse(init.body);
        calls.push({body, signal: init.signal});
        return body.action === 'blocker.explain' ? {ok: true, status: 200, data: recovery} : {ok: false, status: 402};
      },
    });
    vm.runInContext(compile(`const runChatAction = ${callbacks.runChatAction}; globalThis.confirm = ${callbacks.confirmChatAction};`), context);
    const result = await context.confirm(action, {audience, confirmation_message_id: `quote-${ventureId}`});
    assert.deepEqual(calls.map(call => call.body.action), [action, 'blocker.explain']);
    assert.deepEqual(calls[1].body.payload, {code: 'insufficient_credits'});
    assert.notEqual(calls[0].signal, calls[1].signal);
    assert.notEqual(calls[0].body.idempotency_key, calls[1].body.idempotency_key);
    assert.deepEqual(JSON.parse(JSON.stringify(messages)), [{...recovery, client_request_id: calls[1].body.idempotency_key}]);
    assert.doesNotMatch(messages[0].body_md, /credits?/i);
    assert.equal(result, false);
    assert.equal(settleClick(new Set([action]), action, result).has(action), false);
    assert.equal(thinking.at(-1), false);
    assert.equal(flight.current.busy(), false);
    assert.equal(confirmationNonce.current, null);
  }
});

test('chat rename rereads the venture and refreshes scoped header and sidebar names across unrelated domains', async () => {
  function extract(file, predicate) {
    const tree = ts.createSourceFile(file, read(file), ts.ScriptTarget.Latest, true);
    let source;
    function find(node) {
      if (predicate(node, tree)) source = node.getText(tree);
      ts.forEachChild(node, find);
    }
    find(tree);
    assert.ok(source, file);
    return source;
  }
  const refresh = extract('useVentureChat.ts', (node, tree) => ts.isIfStatement(node) && node.expression.getText(tree) === 'grounded.kind === "venture_renamed"');
  const renamed = extract('../office/WorkspaceSwitcher.tsx', node => ts.isFunctionDeclaration(node) && node.name?.text === 'handleRenamed');
  const home = extract('../../lib/companyHome.ts', node => ts.isVariableDeclaration(node) && node.name.getText() === 'renamed');
  for (const [ventureId, name] of [['ven_dispatch', 'Relay Desk'], ['ven_trails', 'Moss Journal']]) {
    for (const mode of ['ok', 'failed', 'wrong-venture', 'retired']) {
      const events = [], calls = [], titles = [], resolved = [];
      let retries = 0, stopped = 0;
      const context = vm.createContext({ventureId, currentId: ventureId, grounded: {kind: 'venture_renamed'},
        current: () => mode !== 'retired', active: () => true,
        api: async path => {calls.push(path); return {ok: mode !== 'failed', data: {id: mode === 'wrong-venture' ? 'ven_elsewhere' : ventureId, name}};},
        CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options.detail;}},
        window: {dispatchEvent: event => events.push(event)}, recovery: {stop: () => stopped++},
        setResolved: value => resolved.push(value), onRenamed: value => titles.push(value), retry: () => retries++});
      vm.runInContext(compile(refresh), context);
      for (let turn = 0; turn < 8; turn++) await Promise.resolve();
      assert.deepEqual(calls, [`/api/ventures/${ventureId}`]);
      assert.equal(events.length, mode === 'ok' ? 1 : 0);
      if (mode !== 'ok') continue;
      assert.equal(events[0].type, 'supymem:venture-renamed');
      vm.runInContext(compile(renamed + `\nglobalThis.homeRename = (${home.slice(home.indexOf('=') + 1)});`), context);
      context.handleRenamed(events[0]);
      context.homeRename(events[0]);
      assert.equal(resolved[0].name, name);
      assert.deepEqual(titles, [name]);
      assert.equal(stopped, 1);
      assert.equal(retries, 1);
      const other = {detail: {ventureId: 'ven_elsewhere', name: 'Unrelated'}};
      context.handleRenamed(other);
      context.homeRename(other);
      assert.deepEqual(titles, [name]);
      assert.equal(retries, 1);
    }
  }
});

test('the first ready question replaces the pending anchor and stops polling across domains', async () => {
  const {ideaPollState} = helpers('../../lib/ideaChatPoll.ts'), {mergeChat} = helpers('../../lib/chatMerge.ts');
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let poll;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'startIdeaPoll') poll = node.initializer.getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(poll);
  for (const domain of ['Freight scheduling', 'Family garden planning']) {
    const pending = {id: domain, role: 'assistant', kind: 'entry_pending'};
    const ready = {...pending, kind: 'idea_clarification_ready', body_md: 'Who needs this first?',
      actions: [{action: 'chat.reply', payload: {question_id: domain}}]};
    const timers = [], shown = [pending];
    const context = vm.createContext({Date, Promise, ideaPollState, mergeChat,
      polledId: {current: ''}, ideaPollGeneration: {current: 0}, current: () => true,
      load: async () => [ready], useCallback: callback => callback,
      setMessages: update => shown.splice(0, shown.length, ...update(shown)),
      window: {setTimeout: callback => timers.push(callback)}});
    vm.runInContext(compile(`globalThis.start = ${poll};`), context);
    context.start(domain, false);
    timers.shift()();
    for (let turn = 0; turn < 8; turn++) await Promise.resolve();
    assert.equal(timers.length, 0);
    assert.equal(shown.some(message => message.id.startsWith('local-idea-wait-')), false);
  }
});

test('failed clarification stops the real poll and removes stale pending notes across unrelated ideas', async () => {
  const {ideaPollState} = helpers('../../lib/ideaChatPoll.ts'), {mergeChat} = helpers('../../lib/chatMerge.ts');
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  let poll;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'startIdeaPoll') poll = node.initializer.getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree);
  for (const idea of ['Reconcile freight invoices', 'Compartir recetas de familia']) {
    const pending = {id: 'pending', role: 'assistant', kind: 'idea_clarification_pending', body_md: idea};
    const failure = {id: 'failed', role: 'assistant', kind: 'idea_failed', body_md: 'Clarification did not finish.'};
    const saved = [pending, failure], timers = [], shown = [pending];
    const context = vm.createContext({Date, Promise, ideaPollState, mergeChat,
      polledId: {current: ''}, ideaPollGeneration: {current: 0}, current: () => true,
      load: async () => saved, useCallback: callback => callback,
      setMessages: update => shown.splice(0, shown.length, ...update(shown)),
      window: {setTimeout: callback => timers.push(callback)}});
    vm.runInContext(compile(`globalThis.start = ${poll};`), context);
    context.start('pending', false);
    timers.shift()();
    for (let turn = 0; turn < 8; turn++) await Promise.resolve();
    assert.equal(timers.length, 0);
    assert.equal(shown.some(message => message.id.startsWith('local-idea-wait-')), false);
    const note = {id: 'local-idea-wait-123', role: 'system', body_md: 'Still preparing'};
    assert.equal(mergeChat([pending, note], saved, true).some(message => message.id === note.id), false);
    assert.equal(mergeChat([pending, failure], [{...pending, id: 'retry'}, note], true).some(message => message.id === note.id), true);
  }
});

test('suggestion turns expose retry errors instead of accepting empty replies or swallowing send failures', async () => {
  const {webcrypto} = require('node:crypto');
  const tree = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  const callbacks = {};
  function find(node) {
    if (ts.isVariableDeclaration(node) && ['sendChat', 'runChatAction', 'showCheckpoint'].includes(node.name.getText(tree))) {
      callbacks[node.name.getText(tree)] = node.initializer.arguments[0].getText(tree);
    }
    ts.forEachChild(node, find);
  }
  find(tree);
  const digest = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'chatPayloadDigest').getText(tree);
  const {conversationResponseMatches} = helpers('../../lib/chatConversations.ts');
  for (const [ventureId, body] of [['ven_inventory', 'Interview purchasing teams about disputed invoices'],
    ['ven_travel', 'Interview parents about accessible holidays']]) {
    for (const outcome of [null, {}, {id: 'empty', body_md: ' '}, {id: 'stamp', body_md: '<!--employee:Charlie:Manager:manager-->'},
      {id: 'echo', role: 'user', body_md: body}, 'before-admission', {id: 'valid', role: 'assistant', body_md: 'Your request is ready for review.'}]) {
      const messages = [], values = new Map(), calls = [], timers = [];
      let thinking = false, controller;
      const flight = {current: {busy: () => false, begin: () => controller = new AbortController(),
        owns: value => controller === value, finish() {}}};
      const context = vm.createContext({Date, Promise, AbortController, TextEncoder, crypto: webcrypto,
        ventureId, conversationId: null, checkpointScope: ventureId, checkpointKey: `checkpoint:${ventureId}`, flight,
        current: () => true, setThinking: value => {thinking = value;},
        setMessages: update => messages.splice(0, messages.length, ...update(messages)),
        messagesRef: {current: messages}, chatRequestRef: {current: {attempt: null}},
        shouldAppendMacTasksHint: () => false, bareMacTaskConfirmation: () => false, browserTaskRequest: () => false,
        reviewableDeviceRequest: () => {if (outcome === 'before-admission') throw new Error('local preparation failed'); return false;},
        resolveCheckpoint: async () => null, conversationResponseMatches, ...helpers('../../lib/chatMerge.ts'),
        parseEmployeeStamp: text => ({text: text.replace(/^<!--employee:[^>]*-->/, '')}),
        withLocalMacTaskObservation: async message => message, startRequestedToolBuild: async message => message, navigateOnResponse: false,
        api: async (_url, init) => {calls.push(JSON.parse(init.body)); return {ok: true, data: outcome};},
        window: {setTimeout: callback => timers.push(callback), localStorage: {
          getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)}},
      });
      vm.runInContext(compile(`${digest}\nglobalThis.sendChat = ${callbacks.sendChat}; globalThis.showCheckpoint = ${callbacks.showCheckpoint}; globalThis.run = ${callbacks.runChatAction};`), context);
      const result = await context.run('chat.reply', {text: body});
      assert.equal(thinking, false);
      if (outcome?.id === 'valid') {
        assert.equal(result, true); assert.equal(messages.at(-1).body_md, outcome.body_md);
        assert.equal(values.size, 0);
      } else {
        assert.equal(result, false);
        const error = messages.find(message => message.role === 'system');
        assert.ok(error?.body_md.trim());
        const retry = error.actions.find(action => ['chat.reply', 'chat.startFresh'].includes(action.action));
        assert.equal(retry?.payload?.text, body);
        if (calls.length) assert.ok(values.size > 0);
      }
    }
  }
});
