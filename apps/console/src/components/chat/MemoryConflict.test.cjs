const assert = require('node:assert/strict'), test = require('node:test');
const { readFileSync } = require('node:fs'), path = require('node:path'), Module = require('node:module');
const ts = require('typescript');
const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)];
const label = node => !node || typeof node !== 'object' ? String(node ?? '') : [node.props?.children].flat(Infinity).map(label).join('');
const domains = [['logistics teams', 'accounting firms'], ['new parents', 'amateur gardeners']];
function harness(values, submit = async () => {}) {
  let cursor = 0;
  const states = [];
  const react = {
    useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = value; }]; },
    useRef(initial) { const index = cursor++; if (!(index in states)) states[index] = { current: initial }; return states[index]; },
  };
  const jsx = (type, props) => ({ type, props });
  const filename = path.join(__dirname, 'MemoryConflict.tsx'), loaded = new Module(filename, module);
  loaded.filename = filename; loaded.paths = module.paths;
  loaded.require = id => id === 'react' ? react : id === 'react/jsx-runtime' ? { jsx, jsxs: jsx }
    : id === '@/components/primitives/ErrorNotice' ? { ErrorNotice: 'error-notice' } : require(id);
  loaded._compile(ts.transpileModule(readFileSync(filename, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, filename);
  const card = { id: 'a'.repeat(64), venture_id: 'ven_scope', subject: 'idea.audience', question: 'Which is right?', choices: values.map((value, index) => ({
    value, source: index ? 'you' : 'Your website', ref: index ? 'chat:msg_source' : 'idea_revision:idv_source',
    timestamp: '2026-10-02T00:00:00+00:00', untrusted: !index,
  })) };
  return { api: loaded.exports, card, render(props = {}) { cursor = 0;
    return loaded.exports.MemoryConflict({ card, messageId: 'msg_card', onSubmit: submit, ...props }); } };
}

test('each sourced value and both-true option submit exact quoted founder corrections across domains', async () => {
  for (const values of domains) for (const choice of [0, 1, 2]) {
    const calls = [], view = harness(values, async text => calls.push(text));
    let tree = view.render();
    assert.equal(nodes(tree).filter(node => node.type === 'fieldset').length, 1);
    assert.equal(nodes(tree).filter(node => node.type === 'input').length, 3);
    assert.match(label(tree), /Your website.*you.*Both are true/);
    assert.equal(nodes(tree).filter(node => node.type === 'time').length, 2);
    assert.equal(nodes(tree).find(node => node.type === 'button').props.disabled, true);
    nodes(tree).filter(node => node.type === 'input')[choice].props.onChange();
    tree = view.render(); await tree.props.onSubmit({ preventDefault() {} });
    const value = choice < 2 ? values[choice] : `Both are true:\n${values.join('\n')}`;
    assert.deepEqual(calls, [`Memory correction msg_card: ${choice}\n${value}`]);
    assert.equal(nodes(view.render()).find(node => node.type === 'button').props.disabled, true);
  }
});

test('invalid conflict payloads are not actionable and external markup remains text', () => {
  for (const values of domains) {
    const view = harness(values);
    assert.ok(view.api.memoryConflict(view.card));
    for (const invalid of [null, {}, { ...view.card, id: 'invalid' }, { ...view.card, venture_id: '' }, { ...view.card, choices: [] },
      { ...view.card, choices: [view.card.choices[0], { ...view.card.choices[1], timestamp: 'not a date' }] }]) {
      assert.equal(view.api.memoryConflict(invalid), null);
    }
    view.card.choices[0].value = '<img src=x onerror="publish()">';
    const tree = view.render();
    assert.match(label(tree), /<img src=x/);
    assert.equal(nodes(tree).some(node => node.props?.dangerouslySetInnerHTML), false);
  }
});

test('a pending choice cannot double-submit and a failure permits an explicit retry', async () => {
  for (const values of domains) {
    let reject, calls = 0;
    const view = harness(values, async () => { calls++; await new Promise((_, fail) => { reject = fail; }); });
    nodes(view.render()).find(node => node.type === 'input').props.onChange();
    const tree = view.render(), pending = tree.props.onSubmit({ preventDefault() {} });
    await tree.props.onSubmit({ preventDefault() {} });
    assert.equal(calls, 1);
    reject(new Error('failed')); await pending;
    const notice = nodes(view.render()).find(node => node.type === 'error-notice');
    assert.match(notice.props.error, /Your choice was not saved/);
    assert.equal(notice.props.source, 'memory-conflict');
    assert.equal(nodes(view.render()).find(node => node.type === 'button').props.disabled, false);
  }
});

test('disabled cards never submit and existing renderer only handles supplied conflict metadata', async () => {
  for (const values of domains) {
    let calls = 0;
    const view = harness(values, async () => calls++);
    nodes(view.render()).find(node => node.type === 'input').props.onChange();
    await view.render({ disabled: true }).props.onSubmit({ preventDefault() {} });
    assert.equal(calls, 0);
  }
  const source = readFileSync(path.join(__dirname, 'ChatDock.tsx'), 'utf8');
  assert.match(source, /resulting_state\?\.memory_conflict \? memoryConflict/);
  assert.match(source, /<MemoryConflict[^]*onAction\("chat\.reply", \{ text \}\)/);
  assert.match(source, /scope\.current !== conflictScope/);
  assert.match(source, /conflict\.venture_id !== ventureId/);
});
