const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const {renderToStaticMarkup} = require('react-dom/server');

test('one experiment receipt never starts sibling cards in the same message', async () => {
  const {confirmationStarted} = await import('../../lib/chatAnswered.ts');
  for (const domain of ['manufacturing compliance teams', 'family recipe sharing']) {
    const confirmations = ['first', 'second'].map((experiment_id, index) => ({
      action: 'experiment.run', resulting_state: {experiment_id, fingerprint: experiment_id, domain},
      ...(index === 0 ? {state: 'started'} : {}),
    }));
    const messages = [{id: 'experiments', role: 'assistant', body_md: domain,
      actions: confirmations.map(confirmation => ({label: 'Run it for me', confirmation}))},
      {id: 'receipt', role: 'assistant', confirmed_action: {
        message_id: 'experiments', action: 'experiment.run', state: 'started',
      }}];
    assert.equal(confirmationStarted(messages, 'experiments', 'experiment.run', confirmations[0]), true);
    assert.equal(confirmationStarted(messages, 'experiments', 'experiment.run', confirmations[1]), false);
    assert.equal(confirmationStarted(messages, 'experiments', 'experiment.run'), false);
  }
});

test('denied confirmation state is shared by the dock and never starts sibling work', async () => {
  const {confirmationState, confirmationStarted} = await import('../../lib/chatAnswered.ts');
  const contract = readFileSync(__dirname + '/../../lib/api-types.ts', 'utf8');
  assert.match(contract.match(/export type ChatConfirmation = \{[^}]+\}/)[0], /state\?:[^;]+"denied"/);
  for (const domain of ['fleet maintenance scheduling', 'home gardening reminders']) {
    const denied = {action: 'experiment.run', state: 'denied', resulting_state: {domain, experiment_id: 'denied'}};
    const sibling = {action: 'experiment.run', state: 'started', resulting_state: {domain, experiment_id: 'started'}};
    const messages = [{id: domain, role: 'assistant', actions: [denied, sibling].map(confirmation => ({confirmation}))},
      {id: 'receipt', confirmed_action: {message_id: domain, action: 'experiment.run', state: 'started'}}];
    assert.equal(confirmationState(messages, domain, 'experiment.run', denied), 'denied');
    assert.equal(confirmationStarted(messages, domain, 'experiment.run', denied), false);
    assert.equal(confirmationStarted(messages, domain, 'experiment.run', sibling), true);
  }
});

test('saved state and later receipts disable only their exact card across unrelated domains', async () => {
  const {confirmationStarted, confirmationState} = await import('../../lib/chatAnswered.ts');
  const {chatActionIdentity} = await import('./chatActionPolicy.ts');
  const source = readFileSync(__dirname + '/ChatDock.tsx', 'utf8');
  const tree = ts.createSourceFile('ChatDock.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let callback, keyFunction;
  const visit = node => {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'confirmations.map') callback = node.arguments[0];
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'actionKey') keyFunction = node.getText(tree);
    ts.forEachChild(node, visit);
  };
  visit(tree);
  const actionKey = vm.runInNewContext(ts.transpileModule(keyFunction + '\nactionKey;', {
    compilerOptions: {target: ts.ScriptTarget.ES2022},
  }).outputText, {chatActionIdentity});
  for (const domain of ['manufacturing compliance teams', 'family recipe sharing']) {
    const card = {id: 'card_one', role: 'assistant', body_md: domain,
      actions: [{label: 'Do it', confirmation: {action: 'pagebuilder.build', resulting_state: {domain}}}]};
    for (const evidence of ['receipt', 'state', 'pending', 'unrelated']) {
      const messages = [structuredClone(card)];
      if (evidence === 'state') messages[0].actions[0].confirmation.state = 'started';
      if (evidence === 'pending') messages[0].actions[0].confirmation.state = 'starting';
      if (evidence === 'receipt' || evidence === 'unrelated') messages.push({id: 'reply', role: 'assistant',
        confirmed_action: {message_id: evidence === 'receipt' ? card.id : 'other_card', action: 'pagebuilder.build'}});
      const expected = evidence !== 'unrelated';
      assert.equal(confirmationStarted(messages, card.id, 'pagebuilder.build'), expected && evidence !== 'pending');
      const element = vm.runInNewContext(ts.transpileModule(`(${callback.getText(tree)})(a)`, {
        compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
      }).outputText, {React: require('react'), a: messages[0].actions[0], m: messages[0], messages,
        confirmationState, actionKey, actionPlan: {primary: messages[0].actions[0]}, cx: (...classes) => classes.filter(Boolean).join(' '), styles: {}, disabled: false, onAction() {}, clicked: new Set(), pendingActions: new Set(),
        actionErrors: {}, ErrorNotice: () => null, shownText: domain, dedupeParagraphs: text => text,
        trackAction: () => assert.fail('Completed card cannot dispatch')});
      const html = renderToStaticMarkup(element);
      assert.equal(html.includes('disabled=""'), expected);
      assert.equal(html.includes('Started'), expected && evidence !== 'pending');
      assert.equal(html.includes('Check status'), evidence === 'pending');
    }
  }
});
