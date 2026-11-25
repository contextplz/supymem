const assert = require('node:assert/strict'), test = require('node:test');
const fs = require('node:fs'), vm = require('node:vm'), ts = require('typescript');

test('your local composer never offers voice controls across unrelated domains', () => {
  const tree = ts.createSourceFile('ChatDock.tsx', fs.readFileSync(__dirname + '/ChatDock.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let condition;
  function visit(node) {
    if (ts.isConditionalExpression(node) && node.whenTrue.getText(tree).startsWith('<VoiceMic')) condition = node.condition.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(condition);
  for (const ventureId of ['ven_procurement', 'ven_home_cooking']) for (const local of [true, false]) {
    assert.equal(Boolean(vm.runInNewContext(condition, {featureVisible: () => !local, ventureId, onVoiceSend: () => {}})), !local);
    assert.equal(Boolean(vm.runInNewContext(condition, {featureVisible: () => !local, ventureId, onVoiceSend: undefined})), false);
  }
});

test('draft-only asks open real chat UI without a venture and respect venture scope and listener cleanup across domains', () => {
  const source = fs.readFileSync(__dirname + '/ChatDock.tsx', 'utf8');
  assert.doesNotMatch(source, /ReferralCard|REFERRALS_EVENT/);
  const tree = ts.createSourceFile('ChatDock.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect;
  function find(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect' && node.arguments[0].getText(tree).includes('askWithoutVenture')) {
      effect = node.arguments[0].getText(tree);
    }
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(effect);
  for (const [venture, audience] of [['ven_procurement', 'Hospital purchasing teams'], ['ven_dogs', 'Dog walking groups']]) {
    for (const ventureId of [undefined, venture]) {
      for (const visible of [false, true]) {
        const target = new EventTarget(), drafts = [];
        const context = vm.createContext({ventureId, visible, PREFILL_ACTION: 'chat.prefill',
          window: target, applyChatPrefill: text => drafts.push(text)});
        const cleanup = vm.runInContext(ts.transpileModule(`(${effect})()`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
        const text = `Review the saved context for ${audience}.`;
        target.dispatchEvent(new CustomEvent('supymem:ask', {detail: {action: 'chat.prefill', payload: {text}}}));
        target.dispatchEvent(new CustomEvent('supymem:ask', {detail: {action: 'chat.prefill', payload: {text: 12}}}));
        assert.deepEqual(drafts, !ventureId && visible ? [text] : []);
        cleanup(); drafts.length = 0;
        target.dispatchEvent(new CustomEvent('supymem:ask', {detail: {action: 'chat.prefill', payload: {text}}}));
        assert.equal(drafts.length, 0);
      }
    }
  }
});
