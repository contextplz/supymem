const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const { readFileSync } = require('node:fs'), ts = require('typescript');
function page(ventureId, api) {
    const state = [], effects = [], refs = [], prompts = []; let cursor = 0, ref = 0;
    const jsx = (type, props) => ({ type, props }), exports = {};
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/page.tsx`, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX }
    }).outputText, { exports, require(id) {
      if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
      if (id === 'react') return { useState(initial) { const index = cursor++; if (!(index in state)) state[index] = initial;
        return [state[index], next => {state[index] = typeof next === 'function' ? next(state[index]) : next;}]; },
        useEffect(effect) { effects.push(effect); }, useRef(initial) {return refs[ref++] ||= {current: initial};} };
      if (id === 'next/navigation') return { useParams: () => ({ ventureId }) };
      return { ChatAnchor: 'a', AppShell: 'shell', ErrorNotice: 'error', ensureSession: async () => {}, api,
        apiFailure: () => 'Choose Try again.', shortTitle: name => name, prefillChat: text => prompts.push(text) };
    }});
    const nodes = node => Array.isArray(node) ? node.flatMap(nodes) : node?.props ? [node, ...nodes(node.props.children)] : [];
    exports.default(); effects.splice(0).forEach(effect => effect());
    return {prompts, render() { cursor = ref = 0; return nodes(exports.default()); }};
}

test('empty experiments let you design your first experiment', async () => {
  for (const ventureId of ['ven_freight', 'ven_hiking']) {
    const view = page(ventureId, async () => ({ok: true, data: {}}));
    await new Promise(setImmediate);
    const button = view.render().find(node => node.props.children === 'Design your first experiment');
    assert.ok(button);
    button.props.onClick();
    assert.match(view.prompts[0], /^Design an experiment/);
  }
});

test('experiment reports are bound to the card and leave observations blank', async () => {
  for (const hypothesis of ['Dental labs want a faster pickup', 'Kite makers want a wind forecast']) {
    const items = [
      { id: 'exp_open', hypothesis, state: 'running', how_to_run: [] },
      { id: 'exp_missing', hypothesis, state: 'concluded', needs_evidence: true, how_to_run: [] },
      { id: 'exp_system', hypothesis, state: 'running', runner: 'system', how_to_run: [] },
    ];
    const view = page('ven_trial', async () => ({ok: true, data: {items}}));
    await new Promise(setImmediate);
    const buttons = view.render().filter(node => node.props['data-usage-control'] === 'experiment_record');
    assert.deepEqual(Array.from(buttons, button => button.props.children), ['Record result', 'Add evidence']);
    for (const button of buttons) button.props.onClick();
    assert.deepEqual(view.prompts, ['Test results: exp_open\nWhat happened: ', 'Test results: exp_missing\nWhat happened: ']);
  }
});

test('failed experiment reads can be retried, without losing or repeating a saved start', async () => {
  for (const [ventureId, hypothesis] of [['ven_freight', 'Depots will pay for forecasts'], ['ven_hiking', 'Families want shorter trails']]) {
    const experiment = {id: 'saved', hypothesis, state: 'running', how_to_run: []};
    let reads = 0, starts = 0;
    const view = page(ventureId, async (path, init) => {
      if (init?.method === 'POST') {
        assert.equal(JSON.parse(init.body).direction, 'rev_current');
        starts++;
        return {ok: true, data: experiment};
      }
      if (!path.endsWith('/experiments')) return {ok: true, data: {name: hypothesis}};
      reads++;
      if (reads === 1 || reads === 3) throw new Error('Offline');
      return {ok: true, data: reads === 2 ? {suggested: [{hypothesis, action: 'Interview people', direction: 'rev_current'}]} : {items: [experiment]}};
    });
    const button = label => view.render().find(node => node.type === 'button' && node.props.children === label);
    await new Promise(setImmediate);
    assert.ok(button('Try again'));
    button('Try again').props.onClick();
    await new Promise(setImmediate);
    button('Start this test').props.onClick();
    await new Promise(setImmediate);
    assert.match(JSON.stringify(view.render()), /started|running/i);
    assert.equal(button('Start this test'), undefined);
    button('Try again').props.onClick();
    await new Promise(setImmediate);
    assert.equal(starts, 1);
    assert.equal(reads, 4);
  }
});
