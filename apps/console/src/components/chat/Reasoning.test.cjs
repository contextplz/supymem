const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function source(file) { return fs.readFileSync(path.join(__dirname, file), 'utf8'); }
function compile(code) {
  return ts.transpileModule(code, {fileName: 'fixture.tsx', compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
  }}).outputText;
}
function declaration(file, name) {
  const tree = ts.createSourceFile(file, source(file), ts.ScriptTarget.Latest, true);
  let found;
  function find(node) {
    if ((ts.isFunctionDeclaration(node) || ts.isVariableDeclaration(node)) && node.name?.getText(tree) === name) found = node;
    ts.forEachChild(node, find);
  }
  find(tree);
  assert.ok(found, name);
  return ts.isFunctionDeclaration(found) ? found.getText(tree).replace(/^export /, '') : found.initializer.getText(tree);
}

test('deep progress isolates unrelated founders and never resurrects a completed task', () => {
  const progress = vm.runInNewContext(compile(declaration('useVentureChat.ts', 'deepAnswerProgress') + '\ndeepAnswerProgress;'));
  for (const [venture, domain] of [['business-founder', 'industrial valve procurement'], ['consumer-founder', 'family meal planning']]) {
    const message = (task, state, spend = 20_000, cap = 5_000_000, owner = venture) => ({
      id: task + state, kind: state === 'thinking' ? 'deep_answer_pending' : 'deep_answer', body_md: domain,
      actions: [{action: 'status', payload: {reasoning: {task_id: task, venture_id: owner, state, spend_micro: spend, cap_micro: cap}}}],
    });
    assert.equal(progress([{id: 'ordinary', role: 'assistant', body_md: domain}], venture), undefined);
    assert.deepEqual(JSON.parse(JSON.stringify(progress([message('same-nonce', 'thinking')], venture))), {spendMicro: 20_000, capMicro: 5_000_000});
    assert.equal(progress([message('same-nonce', 'thinking', 20_000, 5_000_000, 'foreign-founder')], venture), undefined);
    assert.equal(progress([message('same-nonce', 'thinking'), message('same-nonce', 'complete')], venture), undefined);
    assert.equal(progress([message('same-nonce', 'thinking'), message('same-nonce', 'partial')], venture), undefined);
    assert.ok(progress([message('running', 'thinking'), message('other', 'complete')], venture));
    for (const bad of [-1, NaN, Infinity, 0.1, 5_000_001]) assert.equal(progress([message('bad', 'thinking', bad)], venture), undefined);
    assert.equal(progress([message('bad', 'thinking', 0, 10_000_001)], venture), undefined);
  }
});

test('deep thinking keeps costs private without changing ordinary typing across domains', () => {
  const expression = declaration('ChatDock.tsx', 'streamingBody');
  const render = vm.runInNewContext(compile(`(function(streaming) { return ${expression}; });`), {
    React: {createElement: (type, props, ...children) => ({type, props, children})},
    styles: {}, cx: () => '', waitingLonger: false,
  });
  for (const domain of ['industrial valve procurement', 'family meal planning']) {
    const element = render({messageId: domain, text: '', spendMicro: 40_000, capMicro: 5_000_000});
    assert.equal(element.props['aria-hidden'], true);
    assert.equal(element.children.length, 3);
    assert.doesNotMatch(JSON.stringify(element), /spend|credit|\$0\.04|\$5\.00/i);
    const ordinary = render({messageId: domain, text: ''});
    assert.equal(ordinary.props['aria-hidden'], true);
    assert.equal(ordinary.children.length, 3);
  }
});
