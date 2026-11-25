const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

const domains = [
  ['ven_freight', 'You could reduce missed deliveries for freight operators'],
  ['ven_cooking', 'You could help home cooks plan their weekly meals'],
];
const transpile = source => ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
function nodes(node) {
  return Array.isArray(node) ? node.flatMap(nodes) : node?.props ? [node, ...nodes(node.props.children)] : [];
}
function text(node) {
  return Array.isArray(node) ? node.map(text).join('') : node?.props ? text(node.props.children)
    : node == null || typeof node === 'boolean' ? '' : String(node);
}
function harness() {
  const exports = {}, policy = { exports: {} }, copy = { exports: {} }, prompts = [], calls = [];
  vm.runInNewContext(transpile(readFileSync(`${__dirname}/chatActionPolicy.ts`, 'utf8')), { exports: policy.exports });
  vm.runInNewContext(transpile(readFileSync(`${__dirname}/../../lib/loopProgress.ts`, 'utf8')), { exports: copy.exports });
  const jsx = (type, props) => typeof type === 'function' ? type(props) : { type, props: props || {} };
  vm.runInNewContext(transpile(readFileSync(`${__dirname}/ExperimentFeed.tsx`, 'utf8')), {
    exports, Date, require(name) {
      if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx };
      if (name === './chatActionPolicy') return policy.exports;
      if (name === '@/lib/loopProgress') return copy.exports;
      if (name === '@/lib/chatPrefill') return { prefillChat: prompt => prompts.push(prompt) };
      if (name.endsWith('.css')) return { default: new Proxy({}, { get: (_, key) => key }) };
      throw new Error(name);
    },
  });
  return { prompts, calls, render: props => exports.ExperimentFeed({ ...props,
    onAction: (name, payload) => calls.push([name, payload]) }) };
}
function spec(ventureId, statement, index = 0, extra = {}) {
  return { id: `${ventureId}_test_${index}`, venture_id: ventureId, title: `Your test ${index + 1}`,
    hypothesis: statement, scan: index % 2 ? 'redteam' : 'demand', runner: 'scan', state: 'proposed',
    max_duration_days: 7, founder_minutes: 10, ceiling_micro: 50000, approval: 'once',
    metric: 'positive_responses', min_sample: 20,
    success_threshold: { op: '>=', value: 3, min_events: 20 },
    failure_threshold: { op: '<', value: 1, min_events: 20 }, ...extra };
}
function data(ventureId, statement) {
  return { venture_id: ventureId, experiments: [{ id: `${ventureId}_test_0`, venture_id: ventureId,
    hypothesis: statement, state: 'running', started_at: '2026-10-27T12:00:00Z',
    spec: spec(ventureId, statement), result: { outcome: 'inconclusive', measured: { events: 7 } } }],
    proposed_experiments: [0, 1, 2, 3].map(index => spec(ventureId, statement, index)) };
}

for (const [ventureId, statement, metric] of [
  ['ven_packaging', 'You could help packaging factory workers reduce handover delays', 'workers_interviewed'],
  ['ven_running', 'You could help recreational runners plan rest between workouts', 'runners_interviewed'],
]) {
  test(`your customer roles stay unchanged in scoreboards, ideas and criteria: ${ventureId}`, () => {
    const view = harness(), value = data(ventureId, statement);
    value.experiments[0].spec.metric = metric;
    value.proposed_experiments[0].title = statement;
    const tree = view.render({ ventureId, data: value });
    assert.ok(nodes(tree).filter(node => node.type === 'h3').some(node => text(node) === statement));
    assert.ok(nodes(tree).some(node => node.type === 'button' && node.props['aria-label'] === `Run: ${statement}`));
    assert.ok(text(tree).includes(`Works if: ${metric.replaceAll('_', ' ')} is at least 3`));
    assert.deepEqual(view.calls, []);
  });
}

test('flag-gated data stays absent for legacy, empty, malformed, and other-venture reads in two domains', () => {
  for (const [ventureId, statement] of domains) {
    const view = harness();
    for (const value of [undefined, null, false, true, {}, { venture_id: ventureId },
      { venture_id: ventureId, experiments: {}, proposed_experiments: {} }, data('ven_other', statement)]) {
      assert.equal(view.render({ ventureId, data: value }), null);
    }
    assert.deepEqual(view.calls, []); assert.deepEqual(view.prompts, []);
  }
});

test('each scoreboard uses recorded samples, both thresholds, review date, and honest outcomes in two domains', () => {
  for (const [ventureId, statement] of domains) {
    const view = harness(), value = data(ventureId, statement);
    value.proposed_experiments = [];
    for (const [outcome, label] of [['supported', 'Worked'], ['refuted', "Didn't work"], ['inconclusive', 'Too early to tell']]) {
      value.experiments[0].result.outcome = outcome;
      const tree = view.render({ ventureId, data: value });
      assert.match(text(tree), /7 of 20 collected/);
      assert.match(text(tree), /Works if: positive responses is at least 3, after 20 collected/);
      assert.match(text(tree), /Doesn't work if: positive responses is below 1, after 20 collected/);
      assert.equal(nodes(tree).find(node => node.type === 'time').props.dateTime, '2026-11-03T12:00:00.000Z');
      assert.ok(text(tree).includes(label));
      assert.equal(nodes(tree).find(node => node.props['data-outcome']).props['data-outcome'], outcome);
    }
    value.experiments[0].result = null;
    const pending = view.render({ ventureId, data: value });
    assert.match(text(pending), /Testing/); assert.doesNotMatch(text(pending), /0 of 20 collected|Worked|Didn't work/);
    value.experiments[0].result = { outcome: 'inconclusive', measured: { events: 0 } };
    assert.match(text(view.render({ ventureId, data: value })), /0 of 20 collected/);
    value.experiments[0].result.measured.events = -1;
    value.experiments[0].started_at = 'not-a-date';
    value.experiments[0].spec.success_threshold.value = Infinity;
    const malformed = view.render({ ventureId, data: value });
    assert.doesNotMatch(text(malformed), /-1 of|Infinity|Invalid Date|Works if:/);
    assert.equal(nodes(malformed).filter(node => node.type === 'time').length, 0);
    value.experiments[0].started_at = '2026-10-27T12:00:00';
    assert.equal(nodes(view.render({ ventureId, data: value })).filter(node => node.type === 'time').length, 0);
  }
});

test('proposed specs mix sensible and red-pill ideas with bounded actions, time, cost and contact disclosure in two domains', () => {
  for (const [ventureId, statement] of domains) {
    const view = harness(), value = data(ventureId, statement);
    value.experiments = [];
    value.proposed_experiments[2] = spec(ventureId, statement, 2, { runner: 'outreach_split', approval: 'each_send' });
    const tree = view.render({ ventureId, data: value });
    assert.match(text(tree), /Sensible idea/); assert.match(text(tree), /Red-pill idea/);
    assert.match(text(tree), /Time: up to 7 days/); assert.match(text(tree), /Your time: 10 minutes/);
    assert.doesNotMatch(text(tree), /Cost:|usage units/); assert.match(text(tree), /Contacts people/);
    assert.doesNotMatch(text(tree), /hypothesis|evidence level|credits|\u2014/i);
    const buttons = nodes(tree).filter(node => node.type === 'button');
    assert.equal(buttons.filter(node => node.props.className === 'primary sm').length, 1);
    const more = nodes(tree).find(node => node.type === 'details' && text(node.props.children[0]).startsWith('More ideas'));
    assert.ok(more); assert.equal(more.props.open, undefined);
    assert.equal(buttons.length - nodes(more).filter(node => node.type === 'button').length, 3);
    buttons[2].props.onClick();
    assert.equal(view.calls[0][0], 'experiment.prepare');
    assert.deepEqual(JSON.parse(JSON.stringify(view.calls[0][1])), { experiment_id: value.proposed_experiments[2].id });
    assert.ok(!view.calls.some(([name]) => name === 'experiment.run'));
    const disabled = view.render({ ventureId, data: value, disabled: true });
    assert.ok(nodes(disabled).filter(node => node.type === 'button').every(node => node.props.disabled));
    nodes(disabled).find(node => node.type === 'button').props.onClick(); assert.equal(view.calls.length, 1);
    const completed = view.render({ ventureId, data: value, completed: [`experiment.prepare:${value.proposed_experiments[0].id}`] });
    const first = nodes(completed).find(node => node.type === 'button');
    assert.equal(first.props.disabled, true); first.props.onClick(); assert.equal(view.calls.length, 1);
    value.proposed_experiments.push({ ...spec('ven_other', statement), title: 'Not yours' }, null);
    assert.doesNotMatch(text(view.render({ ventureId, data: value })), /Not yours/);
  }
});

test('evidence-backed decisions send the exact requested choice without a confirmation in two domains', () => {
  for (const [ventureId, statement] of domains) {
    const view = harness(), value = { venture_id: ventureId };
    const actions = ['persevere', 'pivot', 'spinoff', 'kill'].map(name => ({ action: `experiment.${name}`,
      label: name, confirmation: { action: `experiment.${name}`, resulting_state: { parent_venture_id: ventureId,
        evidence: [{ hypothesis: statement, outcome: 'refuted', learning: 'You received no replies from 20 people.' }] } } }));
    assert.equal(view.render({ ventureId, data: { ...value, decision_recommendation: { actions: [] } } }), null);
    assert.equal(view.render({ ventureId, data: { ...value, decision_recommendation: { actions: [{ ...actions[0], confirmation: undefined }] } } }), null);
    const tree = view.render({ ventureId, data: { ...value, decision_recommendation: { actions } } });
    const buttons = nodes(tree).filter(node => node.type === 'button');
    assert.deepEqual(buttons.map(text), ['Persevere', 'Pivot', 'Spin off', 'Kill']);
    assert.equal(buttons.filter(node => node.props.className === 'primary sm').length, 1);
    assert.equal(nodes(tree).filter(node => node.type === 'details').length, 1);
    assert.equal(nodes(tree).filter(node => node.type === 'p' && text(node).includes('You received no replies')).length, 4);
    buttons.forEach(node => node.props.onClick());
    assert.deepEqual(view.calls.map(([action, payload]) => [action, payload.text]), actions.map(action => ['chat.reply', `Please ${action.label}.`]));
    assert.deepEqual(view.prompts, []);
    const wrong = { ...actions[0], confirmation: { ...actions[0].confirmation,
      resulting_state: { ...actions[0].confirmation.resulting_state, parent_venture_id: 'ven_other' } } };
    assert.equal(view.render({ ventureId, data: { ...value, decision_recommendation: { actions: [wrong] } } }), null);
    const privateCopy = { ...actions[0], confirmation: { ...actions[0].confirmation, resulting_state: { parent_venture_id: ventureId,
      evidence: [{ hypothesis: statement, learning: 'token=private-value', outcome: 'refuted' }] } } };
    assert.doesNotMatch(text(view.render({ ventureId, data: { ...value, decision_recommendation: { actions: [privateCopy] } } })), /private-value/);
    const mixed = view.render({ ventureId, data: { ...data(ventureId, statement), decision_recommendation: { actions } } });
    assert.equal(nodes(mixed).filter(node => node.type === 'button' && node.props.className === 'primary sm').length, 1);
    const overflow = nodes(mixed).filter(node => node.type === 'details' && /^More (?:ideas|decisions)/.test(text(node.props.children[0])));
    const hiddenButtons = overflow.flatMap(node => nodes(node).filter(child => child.type === 'button'));
    assert.equal(nodes(mixed).filter(node => node.type === 'button' && !hiddenButtons.includes(node)).length, 3);
  }
});

test('scoreboard hides saved spend in two domains', () => {
  for (const [ventureId, statement] of domains) {
    const view = harness(), value = data(ventureId, statement);
    value.proposed_experiments = [];
    assert.doesNotMatch(text(view.render({ ventureId, data: value })), /Spent/);
    value.experiments[0].spend = { spent_micro: 250000, cap_micro: 300000, status: 'running' };
    assert.doesNotMatch(text(view.render({ ventureId, data: value })), /Spent|\$0\.25|\$0\.30/);
    value.experiments[0].spend = { spent_micro: 3000000, cap_micro: null, status: 'running' };
    assert.doesNotMatch(text(view.render({ ventureId, data: value })), /Spent|\$3/);
  }
});
