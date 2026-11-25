const assert = require('node:assert/strict'), test = require('node:test');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript');
const source = fs.readFileSync(path.join(__dirname, 'ChatDock.tsx'), 'utf8');
const tree = ts.createSourceFile('ChatDock.tsx', source, ts.ScriptTarget.Latest, true);
const compile = text => ts.transpileModule(text, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
let keyFunction, disabled, click, linkActions, nextMovesBody;
function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'actionKey') keyFunction = node.getText(tree);
  if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'linkActions') linkActions = node.initializer.getText(tree);
  if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(tree) === 'ChatMessageText') {
    const text = node.attributes.properties.find(attribute => attribute.name?.getText(tree) === 'text');
    if (text?.initializer?.expression?.getText(tree) === 'm.next_moves.body_md') nextMovesBody = text.initializer.expression.getText(tree);
  }
  if (ts.isJsxOpeningElement(node) && node.tagName.getText(tree) === 'button' && node.attributes.properties.some(attribute =>
    attribute.name?.getText(tree) === 'data-usage-control' && attribute.initializer?.text === 'chat_followup_action') && node.getText(tree).includes('actionKey(m, a)')) {
    disabled = node.attributes.properties.find(attribute => attribute.name?.getText(tree) === 'disabled').initializer.expression.getText(tree);
    click = node.attributes.properties.find(attribute => attribute.name?.getText(tree) === 'onClick').initializer.expression.getText(tree);
  }
  ts.forEachChild(node, visit);
}
visit(tree);
const policy = {exports: {}};
vm.runInNewContext(compile(fs.readFileSync(path.join(__dirname, 'chatActionPolicy.ts'), 'utf8')), policy);

test('legacy requested cards become ordinary actions without losing exact inputs or replay state across domains', () => {
  for (const [ventureId, idea] of [['ven_orders', 'Purchasing teams'], ['ven_picnics', 'Family picnics']]) {
    const payload = { venture_id: ventureId, audience: idea, approval_digest: 'saved_review', ceiling_micro: 27000 };
    const saved = { label: 'Allow once', style: 'primary', confirmation: { action: 'gtm.send', resulting_state: payload } };
    const action = policy.exports.founderAction(saved, 'saved_message');
    assert.equal(action.confirmation, undefined);
    assert.equal(action.label, 'Start');
    assert.deepEqual(JSON.parse(JSON.stringify(action.payload)), { ...payload, confirmation_message_id: 'saved_message' });
    const plan = policy.exports.planChatActions([action]);
    assert.equal(plan.confirmations.length, 0);
    assert.equal(plan.visible[0], action);
    assert.ok(policy.exports.founderAction(saved, 'saved_message', 'started').disabled_reason);
    assert.ok(policy.exports.founderAction(saved, 'saved_message', 'denied').disabled_reason);
    assert.equal(policy.exports.founderAction(saved, 'saved_message', 'starting').action, 'status');
    const quote = policy.exports.founderAction({ ...saved, confirmation: { ...saved.confirmation, action: 'gtm.diagnosis.start' } }, 'saved_quote');
    assert.equal(quote.payload.quote_message_id, 'saved_quote');
    assert.equal(saved.confirmation.resulting_state, payload);
  }
});

test('primary clarification precedes open questions and research using real navigation classification across domains', () => {
  const routes = {exports: {}, require: id => require(path.resolve(__dirname, '../../lib', id))};
  vm.runInNewContext(compile(fs.readFileSync(path.join(__dirname, '../../lib/chatRouteAction.ts'), 'utf8')), routes);
  for (const [ventureId, idea] of [['ven_factories', 'factory maintenance teams'], ['ven_gardens', 'home gardeners']]) {
    const clarification = {label: 'Continue clarifying', action: 'idea.review', payload: {}, style: 'primary'};
    const questions = {label: 'Show the open questions', action: 'chat.reply', payload: {text: `Show the open questions for ${idea}`}, style: 'secondary'};
    const research = {label: 'Review saved research', action: 'research', style: 'primary'};
    const context = vm.createContext({m: {role: 'assistant', body_md: idea, actions: [questions, research], next_moves: {actions: [clarification]}},
      ventureId, home: true, visibleIndex: 0, visibleMessages: [{}], inChatActions: routes.exports.inChatActions,
      isChatNavigationAction: routes.exports.isChatNavigationAction, chatRouteAction: routes.exports.chatRouteAction,
      isQuestionReply: policy.exports.isQuestionReply});
    const actions = vm.runInContext(compile('const actions = ' + linkActions + '; actions;'), context);
    const plan = policy.exports.planChatActions(actions);
    assert.deepEqual(Array.from(plan.visible, action => action.action), ['idea.review', 'chat.reply', 'research']);
    assert.equal(plan.primary, clarification);
    assert.equal(plan.visible[1], questions);
    assert.equal(plan.visible[2].navigation, true);
    assert.equal(plan.visible[2].style, 'primary');
    assert.equal(plan.overflow.length, 0);
    const next = {label: 'See your next action', action: 'whats_next', style: 'primary', state_hash: idea, reply_choice: true};
    const later = {...clarification, style: 'secondary', reply_choice: true};
    context.m = {role: 'assistant', body_md: idea, actions: [next, later], next_moves: {actions: [next, later, research]}};
    const nextPlan = policy.exports.planChatActions(vm.runInContext(compile('(' + linkActions + ');'), context));
    assert.deepEqual(Array.from(nextPlan.visible, action => action.action), ['whats_next', 'idea.review', 'research']);
    assert.equal(nextPlan.primary, next);
    assert.equal(nextPlan.visible[1], later);
    assert.equal(nextPlan.visible[0].navigation, undefined);
    const offeredResearch = {...research, style: 'secondary', reply_choice: true, state_hash: idea};
    context.m = {role: 'assistant', body_md: idea, actions: [clarification, offeredResearch], next_moves: {actions: [clarification, offeredResearch,
      {label: 'Pressure-test your idea', action: 'ceo_review', style: 'secondary'},
      {label: 'Find customers', action: 'gtm.find', style: 'secondary'}]}};
    const eitherPlan = policy.exports.planChatActions(vm.runInContext(compile('(' + linkActions + ');'), context));
    assert.deepEqual(Array.from(eitherPlan.visible, action => action.action), ['idea.review', 'research', 'ceo_review']);
    assert.equal(eitherPlan.visible[1], offeredResearch);
    assert.equal(eitherPlan.primary, clarification);
    assert.deepEqual(Array.from(eitherPlan.overflow, action => action.action), ['gtm.find']);
    const ordinaryResearch = {...research, style: 'secondary', state_hash: idea};
    const ordinaryClarification = {...clarification, style: 'secondary', state_hash: idea};
    const pressure = {label: 'Pressure-test your idea', action: 'ceo_review', style: 'primary', state_hash: idea};
    context.m = {role: 'assistant', body_md: 'Your progress is saved.', actions: [], next_moves: {actions: [pressure, ordinaryResearch, ordinaryClarification]}};
    const ordinaryPlan = policy.exports.planChatActions(vm.runInContext(compile('(' + linkActions + ');'), context));
    assert.deepEqual(Array.from(ordinaryPlan.visible, action => action.action), ['ceo_review', 'research', 'idea.review']);
    assert.equal(ordinaryPlan.primary, pressure);
    assert.equal(ordinaryPlan.visible[1].navigation, true);
    assert.equal(ordinaryPlan.visible[2].navigation, true);
    for (const href of [`/v/${ventureId}/idea`, `https://app.supymem.com/v/${ventureId}/idea`]) {
      context.m = {role: 'assistant', body_md: idea, actions: [questions, {label: 'Review saved idea', href, style: 'primary'}]};
      const linked = vm.runInContext(compile('(' + linkActions + ');'), context);
      const linkedPlan = policy.exports.planChatActions(linked);
      assert.deepEqual(Array.from(linkedPlan.visible, action => action.action), ['chat.reply', 'idea.review']);
      assert.equal(linkedPlan.primary, questions);
      assert.equal(linkedPlan.visible[1].navigation, true);
      assert.equal(linkedPlan.visible[1].style, 'primary');
    }
  }
});

test('primary next moves precede secondary views without changing their styles across domains', () => {
  for (const idea of ['Invoice reconciliation for purchasing teams', 'Meal planning for busy households']) {
    const secondary = {label: 'Review saved details', action: 'idea.review', style: 'secondary'};
    const primary = {label: 'Find customers', action: 'gtm.find', style: 'primary'};
    const context = vm.createContext({m: {body_md: idea, actions: [secondary], next_moves: {actions: [primary]}},
      ventureId: 'venture', home: true, inChatActions: actions => actions,
      isChatNavigationAction: () => false, chatRouteAction: () => [{label: 'Show status', action: 'status'}],
      isQuestionReply: policy.exports.isQuestionReply});
    const actions = vm.runInContext(compile('const actions = ' + linkActions + '; actions;'), context);
    const plan = policy.exports.planChatActions(actions);
    assert.deepEqual(Array.from(plan.visible, action => action.action), ['gtm.find', 'idea.review', 'status']);
    assert.equal(plan.primary, primary);
    assert.equal(plan.visible[0].style, 'primary');
    assert.equal(plan.visible[1].style, 'secondary');
    assert.equal(plan.visible[2].style, undefined);
  }
});
const mergeModule = {exports: {}};
vm.runInNewContext(compile(fs.readFileSync(path.join(__dirname, '../../lib/chatMerge.ts'), 'utf8')), mergeModule);

test('draft review remains primary instead of a navigation overflow action across domains', () => {
  for (const idea of ['Purchasing workflows for warehouses', 'Shared walks for neighborhood families']) {
    const review = {label: 'Review 3 drafts', action: 'gtm.status', payload: {show_saved: true}, style: 'primary'};
    const context = vm.createContext({m: {body_md: idea, actions: [review], next_moves: {actions: [review,
      {label: 'Pressure-test your idea', action: 'ceo_review', style: 'secondary'}]}},
      ventureId: 'venture', home: true, inChatActions: actions => actions,
      isChatNavigationAction: action => action.action === 'gtm.status', chatRouteAction: () => [],
      isQuestionReply: policy.exports.isQuestionReply});
    const actions = vm.runInContext(compile('const actions = ' + linkActions + '; actions;'), context);
    const plan = policy.exports.planChatActions(actions);
    assert.equal(plan.primary, review);
    assert.equal(plan.visible[0].label, 'Review 3 drafts');
    assert.equal(plan.visible.filter(action => action.action === 'gtm.status').length, 1);
    assert.equal(plan.overflow.length, 0);
  }
});

test('next moves render from the saved reply without another message', () => {
  for (const idea of ['Inventory workflows for distributors', 'Weekend hikes for families']) {
    const reply = {id: 'reply', role: 'assistant', kind: 'task_completed', body_md: 'Your work is saved.',
      actions: [{label: 'Review saved work', action: 'work.review'}],
      next_moves: {body_md: idea, actions: [{label: 'Find customers', action: 'gtm.find', state_hash: idea}]}};
    const context = vm.createContext({m: reply, ventureId: 'venture', home: true,
      inChatActions: actions => actions, isChatNavigationAction: () => false, chatRouteAction: () => [], renderLightMd: text => text,
      isQuestionReply: policy.exports.isQuestionReply});
    const actions = vm.runInContext(compile('const actions = ' + linkActions + '; actions;'), context);
    assert.deepEqual(Array.from(actions, action => action.action), ['work.review', 'gtm.find']);
    assert.equal(vm.runInContext(compile('const body = ' + nextMovesBody + '; body;'), context), idea);
    const merged = mergeModule.exports.mergeChat([reply], [reply], true);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].id, reply.id);
  }
});

test('replaced next-moves cards rearm buttons only for a new state in unrelated domains', () => {
  const policy = {exports: {}};
  vm.runInNewContext(compile(fs.readFileSync(path.join(__dirname, 'chatActionPolicy.ts'), 'utf8')), policy);
  for (const idea of ['Inventory workflows for distributors', 'Weekend hikes for families']) {
    const calls = [], clicked = new Set();
    const context = vm.createContext({m: {id: 'saved-status', kind: 'next_moves', body_md: idea},
      a: {label: 'Find customers', action: 'gtm.find', payload: {prepare: true}, state_hash: 'state-one'},
      disabled: false, onAction() {}, clicked, chatActionIdentity: policy.exports.chatActionIdentity, trackAction: (...args) => calls.push(args)});
    vm.runInContext(compile(keyFunction + '\nconst click = ' + click + ';'), context);
    const key = () => vm.runInContext('actionKey(m, a)', context);
    assert.equal(vm.runInContext(disabled, context), false);
    vm.runInContext('click()', context);
    assert.equal(calls[0][0], key());
    assert.equal(JSON.stringify(calls[0][2]), JSON.stringify({prepare: true, reply_to_message_id: 'saved-status'}));
    clicked.add(key());
    assert.equal(vm.runInContext(disabled, context), true);
    context.a = {...context.a, state_hash: 'state-two'};
    assert.equal(vm.runInContext(disabled, context), false);
    assert.notEqual(key(), calls[0][0]);
    const original = {...context.m, actions: [{...context.a, state_hash: 'state-one'}]};
    const updated = {...context.m, actions: [context.a]};
    const receipt = {id: 'task-receipt', role: 'assistant', body_md: 'Your work finished.'};
    const merged = mergeModule.exports.mergeChat([original, receipt], [receipt, updated], true);
    assert.deepEqual(Array.from(merged, message => message.id), ['saved-status', 'task-receipt']);
    assert.equal(merged.filter(message => message.kind === 'next_moves').length, 1);
    const unchanged = mergeModule.exports.mergeChat([updated, receipt], [updated, receipt], true);
    assert.deepEqual(Array.from(unchanged, message => message.id), ['saved-status', 'task-receipt']);
    delete context.a.state_hash;
    assert.equal(key(), `saved-status:${policy.exports.chatActionIdentity(context.a)}`);
    clicked.add(key());
    assert.equal(vm.runInContext(disabled, context), true);
  }
});
