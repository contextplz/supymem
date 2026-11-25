const assert = require('node:assert/strict');
const test = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const requireRouteDependency = name => require(`../../lib/${name}`);

test('icon-only composer controls expose keyboard-operable names', () => {
  const source = name => readFileSync(`${__dirname}/${name}.tsx`, 'utf8');
  for (const domain of ['Factory schedules', 'Hiking routes']) {
    assert.match(source('ChatDock'), /<summary[^>]*aria-label="Attach files or folder"[^>]*>\+<\/summary>/, domain);
    assert.match(source('ChatDock'), /<button[^>]*aria-label="Stop Charlie"/, domain);
    assert.match(source('ChatDock'), /<button[^>]*aria-label=\{submitLabel\}/, domain);
    assert.match(source('AttachFileButton'), /<button[^>]*aria-label="Attach files"/, domain);
    assert.match(source('DictateButton'), /<button[^>]*aria-label=\{listening \? "Stop dictation" : "Dictate"\}/, domain);
    assert.match(source('VoiceMic'), /<button\s+type="button"[^>]*aria-label=\{label\}/, domain);
  }
});

test('only the latest coding attempt keeps a live status button across domains', () => {
  const policy = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/chatActionPolicy.ts', 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText, {exports: policy});
  for (const domain of ['Supplier invoice checks', 'Weekend trail guides']) {
    const status = {label: 'Show status here', action: 'status'};
    const first = {id: 'first', role: 'assistant', kind: 'build_tool_started', body_md: domain,
      actions: [{...status, payload: {order_id: 'first-order', task_id: 'task'}}]};
    const latest = {...first, id: 'latest', actions: [{...status, payload: {order_id: 'latest-order', task_id: 'task'}}]};
    const unrelated = {id: 'research', role: 'assistant', kind: 'research_ready', body_md: domain, actions: [status]};
    const messages = [first, unrelated, latest];
    assert.equal(policy.liveActions(first, messages, 0, false, [status]).length, 0);
    assert.equal(policy.liveActions(latest, messages, 2, false, [status]).length, 1);
    assert.equal(policy.liveActions(unrelated, messages, 1, false, [status]).length, 1);
    const failed = {...latest, kind: 'mac_task_stopped'};
    assert.equal(policy.liveActions(first, [first, failed], 0, false, [status]).length, 0);
    const legacy = {...first, kind: 'text', actions: [{label: 'Watch the build', href: '/v/current/builds'}]};
    assert.equal(policy.liveActions(legacy, [legacy, failed], 0, false, [status]).length, 0);
    const otherTask = {...latest, actions: [{...status, payload: {order_id: 'other-order', task_id: 'other-task'}}]};
    assert.equal(policy.liveActions(first, [first, otherTask], 0, false, [status]).length, 1);
  }
});

test('Show status here expands local activity and reads your saved result across domains', async () => {
  const source = ts.createSourceFile('ChatDock.tsx', readFileSync(__dirname + '/ChatDock.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let action;
  function find(node) {if (ts.isFunctionDeclaration(node) && node.name?.text === 'trackAction') action = node.getText(source); ts.forEachChild(node, find);}
  find(source); assert.ok(action);
  for (const workKey of ['Supplier invoices', 'Reading club']) {
    const shown = [], checked = [], context = {local: true, workKey, setBuildStatusScope: scope => shown.push(scope),
      api() {throw new Error('No new work should start');}, onAction: async (action, payload) => {checked.push({action, payload}); return true;},
      disabled: false, scope: {current: workKey}, actionFlights: {current: new Set()}, actionQueue: {current: null},
      setPendingActions() {}, setClicked() {}, setActionErrors() {}};
    vm.runInNewContext(ts.transpileModule(`${action}; exports.run = trackAction;`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText,
      Object.assign(context, {exports: {}}));
    await context.exports.run('status-card', 'status', {});
    assert.deepEqual(shown, [workKey]);
    assert.equal(checked.length, 1);
    assert.equal(checked[0].action, 'status');
  }
});

test('only your latest landing build keeps its preview actions across unrelated domains', () => {
  const policy = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/chatActionPolicy.ts', 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText, {exports: policy});
  for (const domain of ['Warehouse planning', 'Family gardening']) {
    const action = {action: 'site.preview', label: 'Show preview'};
    const older = {id: 'older', role: 'assistant', kind: 'site_preview', text: domain, actions: [action]};
    const latest = {...older, id: 'latest'};
    assert.equal(policy.liveActions(older, [older, latest], 0, false, [action]).length, 0);
    assert.equal(policy.liveActions(latest, [older, latest], 1, false, [action]).length, 1);
    const built = {...latest, actions: [{...action, payload: {job_id: 'saved-build'}}]};
    assert.equal(policy.liveActions(built, [older, built], 1, false, built.actions, true).length, 0, 'The rendered preview card already owns its actions');
    assert.equal(policy.liveActions(older, [older, {id: 'other', role: 'assistant', text: domain}], 0, false, [action]).length, 1);
    const linked = {id: 'linked', role: 'assistant', text: domain, actions: [{label: 'Open preview', href: '/v/saved/site'}]};
    assert.equal(policy.liveActions(older, [older, linked], 0, false, [action]).length, 0);
    assert.equal(policy.latestPageMessage([older, linked]), linked);
    const tree = ts.createSourceFile('ChatDock.tsx', readFileSync(__dirname + '/ChatDock.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    let card;
    function find(node) {
      if (ts.isJsxExpression(node) && node.expression?.getText(tree).includes('BuildPageCard')) card = node.expression;
      ts.forEachChild(node, find);
    }
    find(tree); assert.ok(card);
    for (const m of [older, latest]) {
      const context = {exports: {}, ventureId: domain, m: {...m}, latestPage: latest, previewJob: {job_id: 'saved-build'}, agents: {jobs: []}, BuildPageCard: 'build-card', React, pageResultKind: () => null};
      vm.runInNewContext(compile(`const showInlinePage = ${expressions.showInlinePage}; exports.render = () => (${card.getText(tree)});`), context);
      assert.equal(context.exports.render()?.type, m.id === latest.id ? 'build-card' : undefined);
    }
  }
});

test('failed work keeps retry primary and alternative and explanation inside More across domains', () => {
  const policy = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/chatActionPolicy.ts', 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText, {exports: policy});
  for (const domain of ['Warehouse planning', 'Family gardening']) {
    const actions = [{action: 'gtm.discovery.retry', label: 'Retry', retry: true, style: 'primary', payload: {domain}},
      {action: 'recovery.alternative', label: 'Another approach'}, {action: 'blocker.explain', label: 'Explain'}];
    const plan = policy.planChatActions(actions, false, false, true);
    assert.deepEqual(Array.from(plan.visible, action => action.label), ['Retry']);
    assert.deepEqual(Array.from(plan.overflow, action => action.label), ['Another approach', 'Explain']);
    const consent = {label: 'Allow sending', confirmation: {action: 'outreach.approve'}};
    assert.equal(policy.planChatActions([...actions, consent], false, false, true).confirmations.length, 1);
  }
});

test('desktop send and microphone targets stay at least 32px with visible keyboard focus', () => {
  for (const domain of ['Warehouse planning', 'Family gardening']) {
    const css = readFileSync(__dirname + '/ChatDock.module.css', 'utf8');
    const tool = css.match(/\.homePanel \.composerTools button:not\([^}]+\}/)[0];
    const send = css.match(/\.homePanel \.composerActions button\[data-usage-control="chat_send"\][^}]+\}/)[0];
    for (const rule of [tool, send]) {
      assert.match(rule, /min-width: max\(32px,/, domain);
      assert.match(rule, /min-height: max\(32px,/, domain);
    }
    assert.match(css, /\.homePanel \.composerTools button:focus-visible \{ outline: 2px solid/);
    assert.match(readFileSync(__dirname + '/VoiceMic.module.css', 'utf8'), /\.mic:focus-visible \{ outline: 2px solid/);
  }
});

test('other venture links navigate without using this venture actions', () => {
  const routeSource = readFileSync(__dirname + '/../../lib/chatRouteAction.ts', 'utf8');
  const routeModule = {exports: {}};
  vm.runInNewContext(ts.transpileModule(routeSource, {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  }}).outputText, {module: routeModule, exports: routeModule.exports, URL, require: requireRouteDependency});
  for (const [current, other] of [['ven_factories', 'ven_holidays'], ['ven_families', 'ven_freight']]) {
    for (const suffix of ['', '?chat=1', '/idea', '/site?build=1', '/research']) {
      const link = {label: 'Open your venture', href: `/v/${other}${suffix}`};
      assert.deepEqual(JSON.parse(JSON.stringify(routeModule.exports.inChatActions([link], true, current))), [link]);
      assert.deepEqual(JSON.parse(JSON.stringify(routeModule.exports.inChatActions([link], true, undefined))), [link]);
    }
    assert.equal(routeModule.exports.inChatActions([{label: 'Next', href: `/v/${current}`}], true, current)[0].action, 'next_moves');
  }
  assert.ok(routeCalls.length > 0);
  for (const call of routeCalls) assert.equal(call.arguments[2]?.getText(tree), 'ventureId || undefined');
});

const source = readFileSync(__dirname + '/ChatDock.tsx', 'utf8');
const tree = ts.createSourceFile('ChatDock.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const expressions = {};
const routeCalls = [];
let keyFunction, confirmationCallback, actionBlock;
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(tree) === 'inChatActions') routeCalls.push(node);
  if (ts.isVariableDeclaration(node) && ['linkActions', 'actionPlan', 'renderAction', 'questionExtras', 'otherActions', 'visibleMessages', 'showInlinePage'].includes(node.name.getText(tree))) {
    expressions[node.name.getText(tree)] = node.initializer.getText(tree);
  }
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'actionKey') keyFunction = node.getText(tree);
  if (ts.isCallExpression(node) && node.expression.getText(tree) === 'confirmations.map') confirmationCallback = node.arguments[0].getText(tree);
  if (ts.isJsxExpression(node) && node.expression?.getText(tree).includes('m.next_moves?.body_md')) expressions.nextMovesText = node.expression.getText(tree);
  if (ts.isJsxElement(node) && node.openingElement.getText(tree) === '<div className={styles.actions}>' && node.getText(tree).includes('actionPlan.visible.map')) actionBlock = node.getText(tree);
  ts.forEachChild(node, visit);
}
visit(tree);
const compile = text => ts.transpileModule(text, {compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
}}).outputText;
const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)];
const text = node => !node || typeof node !== 'object' ? String(node ?? '') : [node.props?.children].flat(Infinity).map(text).join('');
const React = {createElement: (type, props, ...children) => ({type, props: {...props, children}})};

test('local build history renders saved status, preview, export, publish and draft controls', async () => {
  const policy = await import('./chatActionPolicy.ts');
  const routeModule = {exports: {}, require: requireRouteDependency};
  vm.runInNewContext(compile(readFileSync(__dirname + '/../../lib/chatRouteAction.ts', 'utf8')), routeModule);
  const tray = ts.createSourceFile('AgentsTray.tsx', readFileSync(__dirname + '/AgentsTray.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const collapse = tray.statements.filter(node => ['codingTaskKey', 'latestCodingAttempts', 'collapseAgentStatuses'].some(name =>
    node.name?.text === name || node.declarationList?.declarations[0]?.name?.text === name));
  for (const [ventureId, audience] of [['ven_dispatch', 'freight dispatchers'], ['ven_picnic', 'families planning picnics']]) {
    const prefix = `/api/ventures/${ventureId}/pagebuilder/jobs/new_page`, job_id = 'new_page';
    const open = {label: 'Open preview', href: `${prefix}/preview`, style: 'primary'};
    const archive = {label: 'Export', href: `${prefix}/source`};
    const start = {id: 'requested', role: 'assistant', kind: 'text', body_md: `Your private page request for ${audience} is saved.`,
      actions: [{label: 'Show status here', action: 'status', task_id: 'landing_page_build', payload: {job_id, venture_id: ventureId}}]};
    const preview = {id: 'ready', role: 'assistant', kind: 'site_preview', body_md: 'Your private preview is ready.',
      actions: [open, archive, {label: 'Publish it', action: 'site.publish', style: 'secondary'},
        {label: 'Review your landing page', action: 'site.preview', payload: {job_id}, style: 'secondary'}]};
    const next = {id: 'next', role: 'assistant', kind: 'next_moves', body_md: 'You can review your draft.',
      actions: [{label: 'Review your draft', action: 'gtm.status', style: 'primary'},
        {label: 'Set up publishing and signups', action: 'site.publish_setup'}, {label: 'Find customers', action: 'gtm.find'}, open, archive]};
    const messages = [start, preview, next];
    const context = vm.createContext({React, exports: {}, ...policy, ...routeModule.exports, local: true, home: true, ventureId, messages,
      jobs: [{id: job_id, kind: 'pagebuilder.build', venture_id: ventureId, task_id: 'landing_page_build', status: 'running'}],
      parseEmployeeStamp: text => ({text}), visibleAction: () => true, firstVisibleIndex: -1, visibleCount: 24,
      options: [], questionSubmit: false, questionExtras: null, styles: new Proxy({}, {get: (_, key) => key}),
      cx: (...classes) => classes.filter(Boolean).join(' '), ChatAnchor: 'a', ErrorNotice: 'error',
      clicked: new Set(), pendingActions: new Set(), actionErrors: {}, disabled: false, onAction() {},
      chatRouteAction: () => [], externalLinkProps: () => ({}), isMacReviewHref: () => false,
      pageResultKind: href => href?.endsWith('/preview') ? 'preview' : null});
    vm.runInContext(compile(collapse.map(node => node.getText(tray)).join('\n') + keyFunction + `
      const allMessages = collapseAgentStatuses(messages, false, jobs, ventureId);
      const latestPage = latestPageMessage(messages);
      const visibleMessages = ${expressions.visibleMessages};
      exports.render = () => visibleMessages.map((m, visibleIndex) => {
        const previewJob = m.actions?.find(action => action.action === 'site.preview')?.payload;
        const showInlinePage = ${expressions.showInlinePage};
        const linkActions = ${expressions.linkActions};
        const otherActions = ${expressions.otherActions};
        const actionPlan = ${expressions.actionPlan};
        const confirmations = actionPlan.confirmations;
        const renderAction = ${expressions.renderAction};
        return {body: m.body_md, controls: (${actionBlock})};
      });
    `), context);
    const rendered = context.exports.render();
    assert.deepEqual(Array.from(rendered, row => row.body), messages.map(message => message.body_md));
    for (const [index, message] of messages.entries()) {
      const controls = nodes(rendered[index].controls).filter(node => node.type === 'button' || node.type === 'a');
      assert.deepEqual(controls.map(text).sort(), message.actions.map(action => action.label).sort(), audience);
    }
  }
});

test('your local requested work offers status without replaying uncertain work or losing consent', async () => {
  const policy = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/chatActionPolicy.ts', 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText, {exports: policy});
  const {founderAction} = policy;
  for (const domain of ['Invoice reconciliation', 'Garden advice']) {
    for (const state of ['starting', 'started']) {
      const saved = {label: 'Approve', confirmation: {action: 'research.start', interpretation: domain, resulting_state: {domain}, state}};
      const result = founderAction(saved, 'saved', state, true);
      assert.equal(result.confirmation, undefined);
      assert.equal(result.action, 'status');
      assert.equal(result.label, 'Check status');
      assert.equal(saved.confirmation.state, state);
    }
    for (const state of [undefined, 'not_started', 'denied']) {
      const saved = {label: 'Approve', confirmation: {action: 'research.start', resulting_state: {domain}, state}};
      const result = founderAction(saved, 'saved', state, true);
      assert.equal(result.confirmation, undefined);
      assert.equal(result.action, 'research.start');
      assert.deepEqual(JSON.parse(JSON.stringify(result.payload)), {domain, confirmation_message_id: 'saved'});
    }
    for (const action of ['outreach.send', 'autopilot.send_cap', 'experiment.spend_cap']) {
      const saved = {label: 'Allow', confirmation: {action, resulting_state: {domain, send_max: 1}}};
      const result = founderAction(saved, 'saved', undefined, true);
      assert.equal(result.action, action);
      assert.deepEqual(result.confirmation.resulting_state, saved.confirmation.resulting_state);
    }
  }
});

test('saved work notices give you one next step without exposing internal states', async () => {
  const {founderAction} = await import('./chatActionPolicy.ts');
  for (const domain of ['factory inspection planning', 'family music lessons']) {
    const saved = {label: 'Run it for me', style: 'primary', confirmation: {
      action: 'experiment.run', resulting_state: {domain, experiment_id: domain},
    }};
    for (const [state, expected] of [
      ['started', "You already started this. Ask me how it's going."],
      ['denied', 'You chose not to start this. Ask me what to do next.'],
    ]) {
      const action = founderAction(saved, 'saved-message', state);
      assert.equal(action.disabled_reason, expected);
      assert.equal(action.action, 'experiment.run');
      assert.deepEqual(action.payload, {...saved.confirmation.resulting_state, confirmation_message_id: 'saved-message'});
      assert.equal(action.confirmation, undefined);
      assert.doesNotMatch(action.disabled_reason, /action|saved status|declined|credits|—/i);
    }
    assert.equal(founderAction(saved, 'saved-message', 'starting').action, 'status');
    assert.equal(saved.confirmation.resulting_state.domain, domain);
  }
});

test('your local recommended review remains the blue next step across unrelated domains', async () => {
  const {chatActionIdentity, isQuestionReply, planChatActions, founderAction} = await import('./chatActionPolicy.ts');
  const routeModule = {exports: {}, require: requireRouteDependency};
  vm.runInNewContext(compile(readFileSync(__dirname + '/../../lib/chatRouteAction.ts', 'utf8')), routeModule);
  for (const audience of ['Commercial greenhouse managers', 'Recreational canoeists']) {
    for (const local of [false, true]) {
      const review = {label: 'Review your answers here', action: 'startup.review', payload: {}, style: 'primary'};
      const find = {label: 'Find customers', action: 'gtm.find', payload: {prepare: true}, style: 'secondary'};
      const message = {id: audience, role: 'assistant', kind: 'next_moves',
        body_md: `You can review your saved answers about ${audience}. Choose ${review.label} to take your next step.`,
        actions: [review, find]};
      const calls = [];
      const context = vm.createContext({React, local, m: message, messages: [message], ventureId: 'venture', home: true,
        inChatActions: routeModule.exports.inChatActions, isChatNavigationAction: routeModule.exports.isChatNavigationAction,
        chatRouteAction: () => [], chatActionIdentity, isQuestionReply, planChatActions, founderAction,
        questionSubmit: false, questionExtras: null, styles: new Proxy({}, {get: (_, key) => key}),
        cx: (...classes) => classes.filter(Boolean).join(' '), ChatAnchor: 'a', ErrorNotice: 'error',
        disabled: false, onAction() {}, pendingActions: new Set(), clicked: new Set(), actionErrors: {},
        trackAction: (...args) => calls.push(args)});
      vm.runInContext(compile(keyFunction + `
        const linkActions = ${expressions.linkActions};
        const otherActions = linkActions;
        const actionPlan = ${expressions.actionPlan};
        const confirmations = actionPlan.confirmations;
        const renderAction = ${expressions.renderAction};
        const render = () => (${actionBlock});
      `), context);
      const buttons = nodes(vm.runInContext('render()', context)).filter(node => node.type === 'button');
      const primary = buttons.filter(button => button.props.className.includes('actionPrimary'));
      assert.equal(primary.length, 1);
      assert.equal(text(primary[0]), local ? review.label : find.label);
      primary[0].props.onClick();
      assert.equal(calls[0][1], local ? review.action : find.action);
      assert.equal(message.actions[0], review);
      for (const action of ['gtm.approve', 'idea.confirm']) {
        const consent = {label: 'Approve your saved work', action, style: 'secondary'};
        assert.equal(planChatActions([review, find, consent], false, false, local).primary, consent);
      }
      assert.equal(planChatActions([review, find], false, true, local).primary, undefined);
    }
  }
});

test('question answers are all visible in order without a routine primary on desktop and phone', async () => {
  const {chatActionIdentity, isQuestionReply, planChatActions, founderAction} = await import('./chatActionPolicy.ts');
  const routeModule = {exports: {}, require: requireRouteDependency};
  vm.runInNewContext(compile(readFileSync(__dirname + '/../../lib/chatRouteAction.ts', 'utf8')), routeModule);
  for (const [idea, count] of [['Inspection scheduling for factory operators', 4], ['Music lessons for families', 9]]) {
    for (const [width, kind] of [[1280, 'text'], [390, 'text'], [1280, 'question'], [390, 'question'],
      [1280, 'idea_clarification_ready'], [390, 'idea_clarification_ready'], [1280, 'question_id'], [390, 'question_id']]) {
      const calls = [];
      const choices = Array.from({length: count}, (_, index) => ({label: `Your choice ${index + 1}`,
        action: 'chat.reply', text: `${idea} choice ${index + 1}`, style: index === count - 1 ? 'primary' : 'secondary',
        ...(kind === 'question_id' ? {payload: {text: `${idea} choice ${index + 1}`, question_id: 'saved-question'}} : {})}));
      const message = {id: idea, role: 'assistant', kind: kind === 'question_id' ? 'text' : kind, body_md: 'Which group do you want to reach?',
        citations: kind === 'text' ? [{question: true}] : [], actions: choices,
        next_moves: {body_md: 'Here are your routine next steps.', actions: [{label: 'Design an experiment', action: 'experiment.design', style: 'primary'}]}};
      const context = vm.createContext({React, local: false, m: message, messages: [message], ventureId: 'venture', home: true, homePanel: width < 600,
        window: {innerWidth: width},
        inChatActions: routeModule.exports.inChatActions, isChatNavigationAction: routeModule.exports.isChatNavigationAction,
        chatRouteAction: () => [{label: 'Open workspace', action: 'status', style: 'primary'}], chatActionIdentity, isQuestionReply, planChatActions, founderAction, confirmationState: () => undefined,
        questionSubmit: false, questionExtras: null, styles: new Proxy({}, {get: (_, key) => key}),
        cx: (...classes) => classes.filter(Boolean).join(' '), ChatAnchor: 'a', RecoveryResultForm: 'recovery', ErrorNotice: 'error',
        disabled: false, onAction() {}, pendingActions: new Set(), clicked: new Set(), actionErrors: {},
        trackAction: (...args) => calls.push(args), own: false, ChatMessageText: 'p'});
      vm.runInContext(compile(keyFunction + `
        const linkActions = ${expressions.linkActions};
        const otherActions = linkActions;
        const actionPlan = ${expressions.actionPlan};
        const confirmations = actionPlan.confirmations;
        const renderAction = ${expressions.renderAction};
        const render = () => (${actionBlock});
      `), context);
      const plan = vm.runInContext('actionPlan', context);
      assert.deepEqual(Array.from(plan.visible, choice => choice.label), choices.map(choice => choice.label));
      assert.equal(plan.overflow.length, 0);
      assert.equal(plan.primary, undefined);
      assert.equal(nodes(vm.runInContext(compile(`(${expressions.nextMovesText})`), context)).length, 0);
      const rendered = vm.runInContext('render()', context);
      assert.equal(nodes(rendered).some(node => node.type === 'details'), false);
      const buttons = nodes(rendered).filter(node => node.type === 'button');
      assert.equal(buttons.length, count);
      assert.ok(buttons.every(button => !button.props.className.includes('actionPrimary')));
      buttons.at(-1).props.onClick();
      assert.equal(calls[0][2].text, choices.at(-1).text);
    }
  }
  assert.match(readFileSync(__dirname + '/ChatDock.module.css', 'utf8'), /\.actions\s*\{[^}]*flex-wrap: wrap/);
  assert.match(readFileSync(__dirname + '/ChatDock.module.css', 'utf8'), /\.actionLink,[^{]+\{[^}]*min-height: 28px;[^}]*max-width: 100%;[^}]*white-space: normal;[^}]*overflow-wrap: anywhere/);
});

test('merged actions keep one primary, three visible controls, distinct payloads and accessible overflow across domains', async () => {
  const {chatActionIdentity, isQuestionReply, planChatActions, founderAction} = await import('./chatActionPolicy.ts');
  const routeModule = {exports: {}, require: requireRouteDependency};
  vm.runInNewContext(compile(readFileSync(__dirname + '/../../lib/chatRouteAction.ts', 'utf8')), routeModule);
  for (const [ventureId, idea] of [['ven_safety', 'Inspection scheduling for factory operators'], ['ven_music', 'Shared practice sessions for amateur musicians']]) {
    const calls = [];
    const confirmation = {label: 'Approve', confirmation: {action: 'research.start', interpretation: 'Review before starting',
      consequences: ['Your saved plan will be used.'], resulting_state: {proposal_id: ventureId, idea}}};
    const recommended = {label: 'Draft', action: 'outreach.draft', payload: {review_only: true, context: {idea, ventureId}}, style: 'primary'};
    const second = {label: 'Draft', name: 'outreach.draft', payload: {review_only: true, context: {idea, ventureId}, contact_ids: ['person_two']}, style: 'primary'};
    const navigation = {label: 'Review', href: `/v/${ventureId}/research`, style: 'primary'};
    const message = {id: 'message', role: 'assistant', body_md: idea, actions: [navigation, recommended, confirmation, second,
      {label: 'Keep going', action: 'gtm.find', payload: {prepare: true}, style: 'primary'}],
      next_moves: {actions: [{...recommended, label: 'Same draft', payload: {context: {ventureId, idea}, review_only: true}},
        {label: 'Build', action: 'pagebuilder.build', payload: {prepare: true}, style: 'primary'},
        {label: 'Do not start', action: 'gtm.automate', payload: {prepare: true}, disabled_reason: 'Connect your account first.'}]}};
    const context = vm.createContext({React, local: false, m: message, messages: [message], ventureId, home: true, homePanel: false, hideComposer: false,
      inChatActions: routeModule.exports.inChatActions, isChatNavigationAction: routeModule.exports.isChatNavigationAction,
      chatRouteAction: () => [], chatActionIdentity, isQuestionReply, planChatActions, founderAction,
      questionSubmit: false, options: [], settled: true, selected: [], composer: {draft: ''}, blocked: false,
      styles: new Proxy({}, {get: (_, key) => key}), cx: (...classes) => classes.filter(Boolean).join(' '),
      ChatAnchor: 'a', RecoveryResultForm: 'recovery', ErrorNotice: 'error', disabled: false, onAction() {}, onSend: text => calls.push(['send', text]),
      pendingActions: new Set(), clicked: new Set(), actionErrors: {}, externalLinkProps: () => ({}), isMacReviewHref: () => false,
      recommendedChoices: options => options, onQuestionBack: () => calls.push(['back']),
      trackAction: (...args) => calls.push(args), submitChoices: (...args) => calls.push(args), answerFor: () => 'An answer', submitLabel: 'Send',
      shownText: idea, dedupeParagraphs: value => value, confirmationState: () => undefined});
    vm.runInContext(compile(keyFunction + `
      const linkActions = ${expressions.linkActions};
      const otherActions = linkActions;
      let actionPlan = ${expressions.actionPlan};
      const renderAction = ${expressions.renderAction};
      let confirmations = actionPlan.confirmations;
      let questionExtras = null;
      const render = () => (${actionBlock});
      const renderConfirmation = ${confirmationCallback};
    `), context);
    const plan = vm.runInContext('actionPlan', context);
    assert.equal(plan.confirmations.length, 0);
    assert.equal(plan.visible.length, 3);
    assert.deepEqual(Array.from(plan.visible, action => action.label), ['Draft', 'Draft', 'Keep going']);
    assert.equal(plan.overflow.length, 4);
    assert.equal(plan.overflow.at(-1).action, 'research');
    assert.equal(plan.primary.action, 'outreach.draft');
    assert.equal(new Set([...plan.confirmations, ...plan.visible, ...plan.overflow].map(chatActionIdentity)).size, 7);
    assert.equal(message.actions[0], navigation);
    context.renamed = {...recommended, label: 'Review your draft'};
    assert.equal(vm.runInContext('actionKey(m, renamed)', context), vm.runInContext('actionKey(m, m.actions[1])', context));
    const rendered = vm.runInContext('render()', context);
    const disclosure = nodes(rendered).find(node => node.type === 'details');
    assert.equal(disclosure.props.onClick, undefined);
    const hidden = new Set(nodes(disclosure));
    const visibleButtons = nodes(rendered).filter(node => node.type === 'button' && !hidden.has(node));
    assert.equal(visibleButtons.length + plan.confirmations.length, 3);
    assert.equal(disclosure.props.open, undefined);
    assert.equal(text(nodes(disclosure).find(node => node.type === 'summary')), 'More actions');
    const approve = nodes(rendered).find(node => node.type === 'button' && text(node) === 'Start');
    assert.equal(approve.props.className, 'actionLink actionGhost');
    approve.props.onClick();
    assert.equal(calls[0][1], 'research.start');
    assert.equal(calls[0][2].confirmation_message_id, message.id);
    assert.equal(calls[0][2].proposal_id, ventureId);
    visibleButtons[0].props.onClick();
    visibleButtons[1].props.onClick();
    assert.notEqual(calls[1][0], calls[2][0]);
    assert.equal(JSON.stringify(calls[1][2]), JSON.stringify({...recommended.payload, reply_to_message_id: message.id}));
    assert.equal(JSON.stringify(calls[2][2]), JSON.stringify({...second.payload, reply_to_message_id: message.id}));
    context.clicked.add(calls[1][0]);
    const afterClick = nodes(vm.runInContext('render()', context));
    assert.equal(afterClick.find(node => node.type === 'button' && text(node) === 'Draft').props.disabled, true);
    assert.equal(afterClick.filter(node => node.type === 'button' && text(node) === 'Draft')[1].props.disabled, false);
    const blockedAction = nodes(disclosure).find(node => node.type === 'button' && text(node) === 'Do not start');
    assert.equal(blockedAction.props.disabled, true);
    assert.equal(blockedAction.props.title, 'Connect your account first.');
    const overflowButton = nodes(disclosure).find(node => node.type === 'button' && text(node) === 'Build');
    overflowButton.props.onClick();
    assert.equal(calls.at(-1)[1], 'pagebuilder.build');
    assert.equal(calls.at(-1)[2].prepare, true);
    context.actionErrors[calls.at(-1)[0]] = 'Your action could not start. Try again.';
    const failedOverflow = nodes(vm.runInContext('render()', context)).find(node => node.type === 'details');
    assert.ok(nodes(failedOverflow).some(node => node.type === 'error' && node.props.error === 'Your action could not start. Try again.'));
    const failedRender = vm.runInContext('render()', context);
    const hiddenErrors = new Set(nodes(nodes(failedRender).find(node => node.type === 'details')));
    assert.ok(nodes(failedRender).some(node => !hiddenErrors.has(node) && node.type === 'error'
      && node.props.error === 'Your action could not start. Try again.'));
    nodes(failedRender).find(node => node.type === 'button' && text(node) === 'Ask for your next step').props.onClick();
    assert.deepEqual(calls.at(-1), ['send', 'What should I do next?']);
    context.actionErrors = {};
    let focused = 0;
    const menu = {open: true, contains: target => target === 'inside', querySelector: () => ({focus: () => focused++})};
    disclosure.props.onKeyDown({key: 'Escape', currentTarget: menu, preventDefault() {}});
    assert.equal(menu.open, false);
    assert.equal(focused, 1);
    menu.open = true;
    disclosure.props.onBlur({currentTarget: menu, relatedTarget: 'inside'});
    assert.equal(menu.open, true);
    disclosure.props.onBlur({currentTarget: menu, relatedTarget: null});
    assert.equal(menu.open, true);
    disclosure.props.onBlur({currentTarget: menu, relatedTarget: 'outside'});
    assert.equal(menu.open, false);
    context.questionSubmit = true;
    context.options = [{label: 'An answer'}];
    context.settled = false;
    vm.runInContext(compile(`actionPlan = ${expressions.actionPlan}; confirmations = actionPlan.confirmations; questionExtras = ${expressions.questionExtras};`), context);
    const withQuestion = nodes(vm.runInContext('render()', context));
    assert.equal(withQuestion.find(node => node.type === 'button').props['data-usage-control'], 'chat_options_submit');
    assert.equal(vm.runInContext('actionPlan.visible.length', context), 2);
    assert.ok(withQuestion.some(node => node.props['data-usage-control'] === 'chat_use_recommended'));
    assert.ok(withQuestion.some(node => node.props['data-usage-control'] === 'onboarding_back'));
    const manyApprovals = planChatActions([...Array.from({length: 4}, (_, index) => ({...confirmation,
      confirmation: {...confirmation.confirmation, resulting_state: {proposal_id: `${ventureId}_${index}`}}})), recommended]);
    assert.equal(manyApprovals.confirmations.length, 4);
    assert.equal(manyApprovals.visible.length, 0);
    assert.equal(manyApprovals.primary, manyApprovals.confirmations[0]);
    assert.equal(manyApprovals.overflow[0], recommended);
    const withoutApproval = planChatActions([navigation, recommended, second]);
    assert.equal(withoutApproval.primary, recommended);
    assert.equal(withoutApproval.visible.at(-1), navigation);
    for (const actionName of ['gtm.approve', 'idea.confirm', 'idea.proposal.approve']) {
      const approval = {label: 'Approve your saved work', action: actionName, payload: {ventureId}, style: 'secondary'};
      const guarded = planChatActions([navigation, recommended, second, approval]);
      assert.equal(guarded.visible[0], approval);
      assert.equal(guarded.primary, approval);
      assert.equal(guarded.visible.length, 3);
      assert.equal(guarded.overflow.includes(approval), false);
    }
    const plainApproval = {label: 'Approve saved work', action: 'gtm.approve', payload: {proposal_id: ventureId}};
    const confirmedApproval = {...plainApproval, confirmation: {action: 'gtm.approve', resulting_state: plainApproval.payload,
      consequences: ['You approve only this saved work.']}};
    const duplicateApproval = planChatActions([plainApproval, recommended, confirmedApproval]);
    assert.equal(duplicateApproval.confirmations[0], confirmedApproval);
    assert.equal(duplicateApproval.visible.includes(plainApproval), false);
    context.m = {...message, actions: [{label: 'Saved research', action: 'research', style: 'primary'}, recommended], next_moves: {actions: []}};
    const convertedNavigation = vm.runInContext(compile(`const reordered = ${expressions.linkActions}; reordered;`), context);
    assert.deepEqual(Array.from(planChatActions(convertedNavigation).visible, action => action.label), ['Draft', 'Saved research']);
    assert.notEqual(chatActionIdentity({label: 'Order', action: 'order', payload: {ids: ['first', 'second']}}),
      chatActionIdentity({label: 'Order', action: 'order', payload: {ids: ['second', 'first']}}));
  }
  const css = readFileSync(__dirname + '/ChatDock.module.css', 'utf8');
  assert.match(css, /\.actionLink,\s*\.actionForm button\s*\{[^}]*min-height: 28px;[^}]*padding: 0 var\(--s-3\);[^}]*border-radius: var\(--r-full\)/);
  assert.doesNotMatch(css, /\.actions button:global\(\.ghost\)/);
  assert.match(css, /\.actionMenu\s*\{[^}]*max-height:[^}]*overflow: auto/);
  assert.match(source, /className=\{cx\(styles.option, picked\(action\) && styles.optionPicked\)\}/);
});

test('Mac overflow survives null blur and every visible chip reaches its handler across domains', async () => {
  const {chatActionIdentity, isQuestionReply, planChatActions, founderAction} = await import('./chatActionPolicy.ts');
  const routeModule = {exports: {}, require: requireRouteDependency};
  vm.runInNewContext(compile(readFileSync(__dirname + '/../../lib/chatRouteAction.ts', 'utf8')), routeModule);
  for (const idea of ['Factory inspection scheduling', 'Weekend music lessons for families']) {
    const calls = [];
    const actions = [
      {label: 'Review your idea', action: 'ceo_review', style: 'primary'},
      {label: 'Draft emails', action: 'outreach.draft', payload: {review_only: true}},
      {label: 'Build your page', action: 'pagebuilder.build', payload: {prepare: true}},
      {label: 'Show customers and drafts', action: 'gtm.status'},
    ];
    const context = vm.createContext({React, m: {id: idea, role: 'assistant', body_md: idea, actions},
      ventureId: 'venture', home: true, inChatActions: routeModule.exports.inChatActions,
      isChatNavigationAction: routeModule.exports.isChatNavigationAction, chatRouteAction: () => [],
      chatActionIdentity, isQuestionReply, planChatActions, confirmations: [], questionSubmit: false, questionExtras: null,
      styles: new Proxy({}, {get: (_, key) => key}), cx: (...classes) => classes.filter(Boolean).join(' '),
      ChatAnchor: 'a', RecoveryResultForm: 'recovery', ErrorNotice: 'error', disabled: false, onAction() {},
      pendingActions: new Set(), clicked: new Set(), actionErrors: {}, trackAction: (...args) => calls.push(args)});
    vm.runInContext(compile(keyFunction + `
      const linkActions = ${expressions.linkActions};
      const actionPlan = planChatActions(linkActions);
      const renderAction = ${expressions.renderAction};
      const render = () => (${actionBlock});
    `), context);
    const rendered = vm.runInContext('render()', context);
    const disclosure = nodes(rendered).find(node => node.type === 'details');
    const menu = {open: true, contains: () => false};
    disclosure.props.onBlur({currentTarget: menu, relatedTarget: null});
    assert.equal(menu.open, true);
    for (const button of nodes(rendered).filter(node => node.type === 'button')) {
      assert.equal(button.props.disabled, false);
      button.props.onClick();
    }
    assert.deepEqual(calls.map(call => call[1]).sort(), actions.map(action => action.action).sort());
    assert.equal(calls.find(call => call[1] === 'gtm.status')[2].reply_to_message_id, idea);
  }
});

test('an answered question keeps no live reply action, while an open one and later replies stay live', async () => {
  const {liveActions} = await import('./chatActionPolicy.ts');
  for (const [audience, other] of [['Warehouse leads', 'Clinic managers'], ['Parents of toddlers', 'Weekend hikers']]) {
    const choice = {label: audience, action: 'chat.reply', text: audience, payload: {text: audience, question_id: 'q1'}};
    const skip = {label: 'Skip the questions', action: 'chat.reply', text: 'skip', payload: {text: 'skip', question_id: 'q1'}};
    const link = {label: 'Open your plan', href: '/plan'};
    const question = {id: 'q', role: 'assistant', body_md: 'Who first?', actions: [choice, skip, link]};
    const chips = {id: 'c', role: 'assistant', kind: 'text', citations: [{question: true}], body_md: 'Who?', actions: [{label: other, action: 'chat.reply', text: other}]};
    const plain = {id: 'p', role: 'assistant', body_md: 'Done.', actions: [{label: other, action: 'chat.reply', text: other}]};
    const reply = {id: 'f', role: 'founder', body_md: audience};
    const live = (thread, index, here = false) => liveActions(thread[index], thread, index, here, thread[index].actions).map(a => a.label);
    assert.deepEqual(live([question], 0), [audience, 'Skip the questions', 'Open your plan']);
    assert.deepEqual(live([question, reply], 0), ['Open your plan']);
    assert.deepEqual(live([question], 0, true), ['Open your plan']);
    assert.deepEqual(live([question, {...chips, actions: [{...choice, payload: {question_id: 'q2'}}]}], 0), ['Open your plan']);
    assert.deepEqual(live([chips, reply], 0), []);
    assert.deepEqual(live([plain, reply], 0), [other]);
    assert.deepEqual(live([question, reply, chips], 2), [other]);
  }
});

test('ChatDock drops answered reply actions before planning buttons', () => {
  assert.match(source, /const otherActions = liveActions\(m, visibleMessages, visibleIndex, clicked\.has\(`\$\{m\.id\}:answer`\), linkActions, showInlinePage\)\.filter/);
});

test('a reply retry with a question ID stays a button, not an answer choice', async () => {
  const {isQuestionReply, liveActions} = await import('./chatActionPolicy.ts');
  for (const text of ['The warehouse team checks pallets daily', 'I take my dog out before breakfast']) {
    const retry = {label: 'Try again', action: 'chat.reply', payload: {text, question_id: 'q1'}};
    const error = {id: 'error', role: 'assistant', kind: 'error', body_md: 'Check your internet connection.', actions: [retry]};
    assert.equal(isQuestionReply(error), false);
    assert.deepEqual(liveActions(error, [error], 0, false, error.actions), [retry]);
  }
  assert.match(source, /function questionOptionsFor\(message: ChatMessage\) \{\s*if \(message.kind === "error"/);
});
