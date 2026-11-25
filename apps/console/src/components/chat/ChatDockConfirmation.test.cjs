const assert = require('node:assert/strict'), test = require('node:test');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript');
const read = file => fs.readFileSync(path.resolve(__dirname, file), 'utf8');
const compile = source => ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;

test('missing builds and apps name an in-chat recovery instead of a hidden screen across domains', () => {
  const context = {exports: {}, require: () => require('../../lib/credits.ts')};
  vm.runInNewContext(compile(read('../../lib/chatConfirmError.ts')), context);
  const {confirmFailureMessage} = context.exports;
  for (const domain of ['Factory maintenance', 'Family meal planning']) for (const code of ['build', 'app']) {
    const reason = confirmFailureMessage(404, {detail: {code}});
    assert.match(reason, /show your saved status here/, domain);
    assert.doesNotMatch(reason, /Open Builds|Report a problem|include code/);
  }
});
const helpers = {exports: {}};
vm.runInNewContext(compile(read('../../lib/chatAnswered.ts')), helpers);
const {confirmationStarted, confirmationState, settleClick} = helpers.exports;
const tree = ts.createSourceFile('ChatDock.tsx', read('ChatDock.tsx'), ts.ScriptTarget.Latest, true);
let trackAction, label, confirmationRenderer;
let keyFunction;
function find(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'trackAction') trackAction = node.getText(tree);
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'actionKey') keyFunction = node.getText(tree);
  if (ts.isCallExpression(node) && node.expression.getText(tree) === 'confirmations.map') confirmationRenderer = node.arguments[0].getText(tree);
  if (ts.isJsxExpression(node) && node.expression?.getText(tree).includes('"Not started"')) label = node.expression.getText(tree);
  ts.forEachChild(node, find);
}
find(tree);
const policy = {exports: {}};
vm.runInNewContext(compile(read('chatActionPolicy.ts')), policy);
const actionKey = vm.runInNewContext(compile(keyFunction + '\nactionKey;'), policy.exports);
const confirmErrors = {exports: {}, require: () => ({})};
vm.runInNewContext(compile(read('../../lib/chatConfirmError.ts')), confirmErrors);
const {ChatActionPendingError} = confirmErrors.exports;

test('slow actions show one waiting message and protect repeat clicks until their result or a generous wait across domains', async () => {
  const hook = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  const callbacks = {};
  function findHook(node) {
    if (ts.isVariableDeclaration(node) && ['runChatAction', 'confirmChatAction'].includes(node.name.getText(hook))) {
      callbacks[node.name.getText(hook)] = node.initializer.arguments[0].getText(hook);
    }
    ts.forEachChild(node, findHook);
  }
  findHook(hook);
  const waiting = "This is taking longer than usual. I'll post the result here.";
  for (const domain of ['Commercial kitchen maintenance', 'Beginner birdwatching groups']) {
    for (const status of [504, 0, 408]) {
      for (const outcome of ['receipt', 'saved-card', 'expired']) {
        let now = 1000, clicked = new Set(), pendingActions = new Set(), actionErrors = {}, thinking = false;
        const calls = [], timers = new Map(), nonce = `request-${domain}`, key = `button-${domain}`;
        const messages = [{id: 'card', role: 'assistant', body_md: domain, actions: [
          {label: 'Start your work', confirmation: {action: 'research.start', resulting_state: {audience: domain}}},
          {label: 'Start other work', confirmation: {action: 'research.start', resulting_state: {audience: 'other'}}},
        ]}];
        let timerId = 0;
        const timeout = (callback, delay) => {
          if (!delay) { callback(); return ++timerId; }
          timers.set(++timerId, callback); return timerId;
        };
        const context = vm.createContext({Set, Error, Promise, Date: {now: () => now}, AbortController, exports: {},
          crypto: {randomUUID: () => nonce}, ventureId: 'ven_sample', conversationId: null,
          current: () => true, confirmationNonce: {current: null}, messagesRef: {current: messages},
          setThinking: value => {thinking = value;},
          setMessages: update => messages.splice(0, messages.length, ...update(messages)),
          api: async (url, init) => {calls.push({url, body: JSON.parse(init.body), headers: init.headers});
            return {ok: false, status, data: status === 504 ? {error: {code: 'upstream_timeout'}} : null};},
          setTimeout: timeout, window: {setTimeout: timeout, dispatchEvent() {}}, CustomEvent,
          disabled: false, scope: {current: domain}, actionFlights: {current: new Set()}, actionQueue: {current: null},
          setClicked: update => {clicked = update(clicked);},
          setPendingActions: update => {pendingActions = update(pendingActions);},
          setActionErrors: update => {actionErrors = update(actionErrors);},
          chatActionIdentity: policy.exports.chatActionIdentity,
          require: () => ({}),
        });
        for (const file of ['../../lib/chatConfirmError.ts', '../../lib/chatStop.ts', '../../lib/chatAnswered.ts', '../../lib/chatPrefill.ts']) {
          context.exports = {};
          vm.runInContext(compile(read(file)), context);
          Object.assign(context, context.exports);
        }
        assert.equal(context.confirmFailureMessage(status, status === 504 ? {error: {code: 'upstream_timeout'}} : null), waiting);
        context.flight = {current: context.chatFlight()};
        vm.runInContext(compile(`const runChatAction = ${callbacks.runChatAction}; const confirm = ${callbacks.confirmChatAction};
          globalThis.onAction = confirm; ${trackAction}`), context);
        const payload = {audience: domain, confirmation_message_id: 'card'};
        const running = context.trackAction(key, 'research.start', payload);
        const flush = async () => {for (let turn = 0; turn < 30; turn++) await Promise.resolve();};
        await flush();
        assert.equal(messages.filter(message => message.body_md === waiting).length, 1);
        assert.doesNotMatch(messages.map(message => message.body_md).join('\n') + Object.values(actionErrors).join('\n'), /Could not start|couldn't reach|Nothing was changed/);
        assert.equal(pendingActions.has(key), true);
        assert.equal(clicked.has(key), true);
        assert.equal(thinking, false, 'history polling must resume while the button waits');
        assert.equal(context.flight.current.busy(), true);
        await context.trackAction(key, 'research.start', payload);
        assert.equal(calls.length, 1);
        assert.equal(calls[0].headers['Idempotency-Key'], nonce);
        assert.equal(calls[0].body.idempotency_key, nonce);
        messages.push({id: 'unrelated', role: 'assistant', body_md: 'Your other work finished.', client_request_id: 'other-request'});
        messages[0].actions[1].confirmation.state = 'started';
        let timer = [...timers][0]; timers.delete(timer[0]); now += 5000; timer[1](); await flush();
        assert.equal(pendingActions.has(key), true, 'a sibling result must not settle this action');
        if (outcome === 'receipt') messages.push({id: 'result', role: 'assistant', body_md: `Your result for ${domain} is ready.`, client_request_id: nonce});
        if (outcome === 'saved-card') messages[0].actions[0].confirmation.state = 'started';
        now += outcome === 'expired' ? 900000 : 5000;
        timer = [...timers][0]; timers.delete(timer[0]); timer[1]();
        await running;
        assert.equal(pendingActions.has(key), false);
        assert.equal(clicked.has(key), outcome !== 'expired');
        assert.equal(context.flight.current.busy(), false);
        assert.equal(actionErrors[key], '');
        assert.equal(messages.filter(message => message.body_md === waiting).length, outcome === 'expired' ? 1 : 0);
        assert.equal(calls.length, 1);
      }
    }
  }
});

test('a handled failed Retry releases the button without adding a second error across domains', async () => {
  for (const domain of ['Dental equipment servicing', 'Pottery sessions for retirees']) {
    let clicked = new Set(), pending = new Set(), errors = {};
    const messages = [];
    const context = vm.createContext({Set, Error, settleClick, disabled: false,
      scope: {current: domain}, actionFlights: {current: new Set()}, actionQueue: {current: null},
      setClicked: update => {clicked = update(clicked);},
      setPendingActions: update => {pending = update(pending);},
      setActionErrors: update => {errors = update(errors);},
      onAction: async () => {messages.push('You can add the missing step to your request and try again.'); return false;},
    });
    vm.runInContext(compile(trackAction), context);
    await context.trackAction(domain, 'experiment.start', {text: `Design an experiment for ${domain}`});
    assert.equal(messages.length, 1);
    assert.equal(errors[domain], '');
    assert.equal(clicked.size, 0);
    assert.equal(pending.size, 0);
    assert.equal(context.actionFlights.current.size, 0);
  }
});

test('a replied HTTP failure stays a server failure through response handling, on web and Mac', async () => {
  const client = ts.createSourceFile('client.ts', read('../../lib/client.ts'), ts.ScriptTarget.Latest, true);
  const functions = {};
  function findClient(node) {
    if (ts.isFunctionDeclaration(node) && ['rawApi', 'nativeRequest'].includes(node.name?.text)) functions[node.name.text] = node.getText(client);
    ts.forEachChild(node, findClient);
  }
  findClient(client);
  for (const domain of ['Dental equipment servicing', 'Pottery sessions for retirees']) {
    for (const [native, phase] of [[false, 'diagnostic'], [true, 'diagnostic'], [false, 'body'], [false, 'csrf']]) {
      const status = phase === 'csrf' ? 403 : 500;
      const data = {error: {code: 'experiment_plan_failed', message: `You can revise your request for ${domain}.`}};
      const settlements = [];
      let diagnosticFailure = phase === 'diagnostic';
      const context = vm.createContext({Headers, AbortSignal, Error, SyntaxError, DOMException, JSON,
        sessionReceiptEpoch: 1, sessionSubject: 'founder', csrfToken: 'fixture', unauthorizedRequests: new Map(),
        webRequestCurrent: () => true, readCsrfCookie: () => '', newRequestId: () => 'req_fixture', pageOrigin: () => 'https://studio.example',
        resolveGateway: () => 'https://studio.example', fetch: async () => phase === 'body'
          ? {ok: false, status, text: async () => {throw new Error('Unreadable response body');}}
          : new Response(JSON.stringify(data), {status}),
        isCsrfFailure: () => phase === 'csrf', ensureSession: async () => ({ok: false, status: 0, data: null}),
        claimControlOutcome: () => null, settleControlOutcome: (_control, status) => settlements.push(status), noteReconnecting() {},
        desktopInvokeOrThrow: async () => ({status: 500, data}), waitForChatRequest: async promise => promise,
        isTransientNativeIdentityFence: () => false, apiDiagnosticOperation: () => 'chat_confirm',
        apiDiagnostic: () => 'server_error', apiDiagnosticMessage: () => 'Server failure',
        apiDiagnosticReference: () => null, apiDiagnosticOrderId: () => null, apiDiagnosticDetail: () => null,
        native503Origin: () => null, announceDiagnostic: () => {
          if (diagnosticFailure) {diagnosticFailure = false; throw new Error('Local diagnostic listener failed');}
        },
        transientReadFailure: () => false, isAbort: () => false,
      });
      vm.runInContext(compile(functions.nativeRequest + '\n' + functions.rawApi), context);
      const result = await context.rawApi('/api/ventures/fixture/chat/confirm', {method: 'POST', body: '{}'}, {subject: 'founder'}, native);
      assert.equal(result.status, status);
      assert.equal(result.ok, false);
      assert.deepEqual(result.data, phase === 'body' ? null : data);
      assert.deepEqual(settlements, native ? [500] : []);
    }
  }
});

test('unknown actions show one failure and a working fresh-request control without rerunning the retired action across domains', async () => {
  let system;
  function findSystem(node) {
    if (ts.isIfStatement(node) && node.expression.getText(tree) === 'role === "system" && m.kind !== "confirmation"') system = node.thenStatement.getText(tree);
    ts.forEachChild(node, findSystem);
  }
  findSystem(tree); assert.ok(system);
  const hook = ts.createSourceFile('useVentureChat.ts', read('useVentureChat.ts'), ts.ScriptTarget.Latest, true);
  const callbacks = {};
  function findHook(node) {
    if (ts.isVariableDeclaration(node) && ['runChatAction', 'confirmChatAction'].includes(node.name.getText(hook))) {
      callbacks[node.name.getText(hook)] = node.initializer.arguments[0].getText(hook);
    }
    ts.forEachChild(node, findHook);
  }
  findHook(hook);
  const {webcrypto} = require('node:crypto');
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)];
  for (const [ventureId, request] of [['ven_payroll', 'Review the saved plan for payroll teams'], ['ven_stargazing', 'Review the saved plan for local stargazers']]) {
    for (const envelope of [{error: {code: 'unknown_action'}}, {detail: {error: {code: 'unknown_action'}}}, {detail: 'unknown_action'}]) {
      const messages = [{id: 'founder-request', role: 'founder', body_md: request}], events = [], calls = [];
      let focuses = 0;
      const context = vm.createContext({Date, Promise, setTimeout, AbortController, crypto: webcrypto, exports: {}, ventureId, conversationId: null,
        current: () => true, confirmationNonce: {current: null}, messagesRef: {current: messages}, setThinking() {},
        setMessages: update => messages.splice(0, messages.length, ...update(messages)),
        api: async (url, init) => {calls.push({url, body: JSON.parse(init.body)}); return {ok: false, status: 400, data: envelope};},
        window: {dispatchEvent: event => events.push(event)}, CustomEvent,
        inputRef: {current: {focus: () => focuses++}}, onAction: undefined, disabled: false,
        setActionErrors() {}, setReferralsOpen() {}, reportUserError() {},
        pendingActions: new Set(), actionErrors: {}, actionKey, home: true,
        inChatActions: actions => actions, cx: (...values) => values.join(' '), styles: {}, ChatAnchor: 'a', ErrorNotice: 'error',
        require: () => ({jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})}),
      });
      for (const file of ['../../lib/chatPrefill.ts', '../../lib/chatConfirmError.ts', '../../lib/chatStop.ts', '../../lib/chatAnswered.ts']) {
        context.exports = {};
        vm.runInContext(compile(read(file)), context);
        Object.assign(context, context.exports);
      }
      context.flight = {current: context.chatFlight()};
      vm.runInContext(compile(`const runChatAction = ${callbacks.runChatAction}; globalThis.confirm = ${callbacks.confirmChatAction};\n${trackAction}`), context);
      assert.equal(await context.confirm('retired.action', {audience: request}), false);
      const failures = messages.filter(message => message.id.startsWith('local-err-'));
      assert.equal(failures.length, 1);
      context.m = failures[0]; context.parsed = {text: failures[0].body_md};
      assert.match(context.m.body_md, /prepare a fresh request/i);
      const view = vm.runInContext(ts.transpileModule(`(() => ${system})()`, {compilerOptions: {
        module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
      }}).outputText, context);
      const button = nodes(view).find(node => node.type === 'button');
      assert.equal(button.props.children, 'Prepare a fresh request');
      assert.equal(button.props.disabled, false);
      button.props.onClick();
      assert.deepEqual(events.map(event => event.type), ['supymem-chat-prefill']);
      assert.equal(events[0].detail.text, request);
      assert.equal(focuses, 1);
      assert.equal(calls.length, 1);
      assert.equal(calls[0].body.action, 'retired.action');
      assert.deepEqual(Array.from(context.confirmFailureActions({detail: 'task'}, messages)), []);
      assert.ok(context.confirmFailureActions(envelope, [])[0].payload.text.trim());
    }
  }
});

test('memory conflict cards and standing approval controls coexist across unrelated domains', async () => {
  const code = ts.transpileModule(`const render = ${confirmationRenderer}; render;`, {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText;
  const nodes = node => Array.isArray(node) ? node.flatMap(nodes) : !node || typeof node !== 'object' ? [] : [node, ...nodes(node.props?.children)];
  for (const domain of ['Warehouse stock reconciliation', 'Family meal planning']) {
    const calls = [], scope = {current: domain}, m = {id: `message_${domain}`};
    const render = vm.runInNewContext(code, {exports: {}, require: () => ({jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})}),
      memoryConflict: value => value, MemoryConflict: 'memory-conflict', ApprovalPolicyControls: 'approval-controls', ErrorNotice: 'error-notice',
      scope, m, messages: [m], ventureId: domain, disabled: false, historyReady: true, composer: {pending: false}, clicked: new Set(),
      onAction: async (...args) => { calls.push(args); }, actionKey, confirmationState: () => undefined,
      senderReviews: {}, deniedPolicies: new Set(), pendingActions: new Set(), actionErrors: {}, actionPlan: {}, styles: {}, cx: () => '',
    });
    const conflict = {venture_id: domain, subject: 'idea.audience'};
    const memory = render({confirmation: {action: 'memory.resolve', resulting_state: {memory_conflict: conflict}}});
    assert.equal(memory.type, 'memory-conflict');
    assert.equal(memory.props.card, conflict);
    assert.equal(memory.props.disabled, false);
    await memory.props.onSubmit(domain);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], 'chat.reply');
    assert.equal(calls[0][1].text, domain);
    const approval = {label: 'Do it', confirmation: {action: 'research.start', resulting_state: {},
      approval_policy: {eligible: true, venture_id: domain, workspace_id: `workspace_${domain}`}}};
    const controls = nodes(render(approval)).find(node => node.type === 'approval-controls');
    assert.equal(controls.props.policy, approval.confirmation.approval_policy);
    assert.equal(controls.props.disabled, false);
    assert.equal(controls.props.current(), true);
    assert.equal(nodes(render(approval)).find(node => node.type === 'button').props.children, 'Approve once');
    scope.current = 'different_session';
    assert.equal(controls.props.current(), false);
    await assert.rejects(memory.props.onSubmit(domain), /Your session changed/);
    assert.equal(calls.length, 1);
  }
});

test('undispatchable chips show an actionable error and release the chip across domains', async () => {
  for (const idea of ['Stock reconciliation for purchasing teams', 'Meal planning for busy households']) {
    for (const outcome of ['missing handler', 'missing action', 'disabled action', 'throw']) {
      let clicked = new Set(), pendingActions = new Set(), actionErrors = {};
      const key = `${idea}:${outcome}`;
      const context = vm.createContext({Set, Error, settleClick, ChatActionPendingError, disabled: false,
        scope: {current: idea}, actionFlights: {current: new Set()}, actionQueue: {current: null}, reportUserError() {},
        setClicked: update => {clicked = update(clicked);},
        setPendingActions: update => {pendingActions = update(pendingActions);},
        setActionErrors: update => {actionErrors = update(actionErrors);},
        onAction: outcome === 'missing handler' ? undefined : async () => {
          if (outcome === 'throw') throw new Error('You could not open this action. Ask for your next step.');
          return false;
        },
      });
      vm.runInContext(compile(trackAction), context);
      await context.trackAction(key, outcome === 'missing action' ? '' : 'unknown.action', {},
        outcome === 'disabled action' ? 'You cannot use this action. Ask for your next step.' : undefined);
      assert.ok(actionErrors[key], `${outcome} must have visible error copy`);
      assert.equal(clicked.has(key), false);
      assert.equal(pendingActions.size, 0);
    }
  }
});

test('a second button tapped while one runs waits its turn instead of failing', async () => {
  const started = [], errors = {};
  let finishFirst;
  const context = vm.createContext({Set, Error, settleClick, disabled: false,
    scope: {current: 'conversation'}, actionFlights: {current: new Set()}, actionQueue: {current: null},
    setClicked: () => {}, setPendingActions: () => {}, setActionErrors: update => { Object.assign(errors, update(errors)); },
    onAction: action => { started.push(action); return action === 'first' ? new Promise(resolve => { finishFirst = resolve; }) : Promise.resolve(true); },
  });
  vm.runInContext(compile(trackAction), context);
  const first = context.trackAction('a', 'first', {});
  const second = context.trackAction('b', 'second', {});
  assert.deepEqual(started, ['first']);
  finishFirst(true);
  await Promise.all([first, second]);
  assert.deepEqual(started, ['first', 'second']);
  assert.ok(!errors.a && !errors.b);
});

test('confirmation labels require a real start, including refused responses and reloaded cards in unrelated domains', async () => {
  for (const [ventureId, idea] of [['ven_inventory', 'Reconcile warehouse inventory'], ['ven_travel', 'Plan accessible family trips']]) {
    for (const outcome of ['refused', 'failed', 'started']) {
      let clicked = new Set(), pendingActions = new Set(), actionErrors = {};
      const message = {id: `quote_${ventureId}`, role: 'assistant', body_md: idea,
        actions: [{label: 'Do it', confirmation: {action: 'research.start'}}]};
      const messages = [message], key = actionKey(message, message.actions[0]);
      const context = vm.createContext({Set, Error, settleClick, ChatActionPendingError, disabled: false,
        scope: {current: ventureId}, actionFlights: {current: new Set()}, actionQueue: {current: null},
        setClicked: update => {clicked = update(clicked);},
        setPendingActions: update => {pendingActions = update(pendingActions);},
        setActionErrors: update => {actionErrors = update(actionErrors);},
        onAction: async action => {
          assert.equal(pendingActions.has(key), true);
          if (outcome === 'failed') return false;
          messages.push({id: `reply_${ventureId}`, role: 'assistant', body_md: idea,
            confirmed_action: {message_id: message.id, action, state: outcome === 'started' ? 'started' : 'not_started'}});
        },
      });
      vm.runInContext(compile(trackAction), context);
      await context.trackAction(key, 'research.start', {confirmation_message_id: message.id});
      assert.equal(pendingActions.size, 0);
      if (outcome !== 'failed') assert.equal(actionErrors[key], '');
      const render = (conf, selected = clicked, thread = messages) => {
        const started = confirmationStarted(thread, message.id, 'research.start');
        return vm.runInNewContext(label, {pendingActions, clicked: selected, m: message, a: message.actions[0], conf, started, denied: conf.state === 'denied',
          actionKey, confirmationStatus: confirmationState(thread, message.id, 'research.start')});
      };
      assert.equal(render(message.actions[0].confirmation), outcome === 'started' ? 'Started' : outcome === 'refused' ? 'Not started' : 'Do it');
      assert.equal(render({...message.actions[0].confirmation, state: 'denied'}), 'Denied');
      assert.equal(confirmationStarted(messages, message.id, 'research.start'), outcome === 'started');
      if (outcome === 'refused') {
        const persisted = {...message, actions: [{...message.actions[0], confirmation: {action: 'research.start', state: 'not_started'}}]};
        assert.equal(render(persisted.actions[0].confirmation, new Set(), [persisted]), 'Not started');
        for (const kind of ['needs_you', 'confirmation']) {
          const legacy = {...message, actions: [{...message.actions[0], confirmation: {action: 'research.start', state: 'started'}}]};
          const refused = {id: 'legacy-refusal', role: 'assistant', kind,
            confirmed_action: {message_id: message.id, action: 'research.start'}};
          assert.equal(render(legacy.actions[0].confirmation, new Set(), [legacy, refused]), 'Not started');
        }
        assert.equal(render(persisted.actions[0].confirmation, new Set(), [...messages, {
          id: 'retry', role: 'assistant', confirmed_action: {message_id: message.id, action: 'research.start', state: 'started'}}]), 'Started');
      }
    }
  }
});
