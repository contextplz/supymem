const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

function nodes(tree) {
  if (!tree) return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return typeof tree === 'object' ? [tree, ...nodes(tree.props?.children)] : [];
}

function harness(skipped, local = false, ventureId = 'venture-a') {
  let cursor = 0;
  const state = [], effects = [], calls = [], nextInputs = [];
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in state)) state[index] = initial;
      return [state[index], value => { state[index] = value; }];
    },
    useRef: current => ({ current }),
    useEffect: callback => effects.push(callback),
  };
  const jsx = (type, props) => ({ type, props }), exports = {};
  const output = ts.transpileModule(readFileSync(join(__dirname, 'page.tsx'), 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  vm.runInNewContext(output, { exports, window: { location: { hash: '' } }, require(id) {
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id === 'next/navigation') return { useParams: () => ({ ventureId }) };
    if (id === '@/components/layout/NextStepStrip') return { useNextStep: () => ({ journey_overrides: { skipped } }) };
    if (id === '@/hooks/useDurableJob') return { useDurableJob: () => null };
    if (id === '@/lib/detailLevel') return { useDetailLevel: () => ['simple'] };
    if (id === '@/lib/durableJobs') return { restoreActiveJob: () => ({ id: null, error: '' }) };
    if (id === '@/lib/executionNextAction') return { executionNextAction: input => { nextInputs.push(input); return { label: 'Estimate plan', href: '#plan-create' }; } };
    if (id === '@/lib/planObjective') return { objectivePageCopy: () => ({ heading: 'Execution', subtitle: 'Plan' }) };
    if (id === '@/lib/planReview') return { groupPlanFindings: () => [] };
    if (id === '@/lib/client') return { ensureSession: async () => {}, api: async (path, init) => {
      calls.push({ path, init });
      return { ok: true, data: path.endsWith('/resources') ? { inventory: { complete: false, assets: [] }, execution_budget: { status: local ? 'local' : 'ready' } }
        : path.endsWith('/estimate-generation') ? { sufficient_now: true } : { items: [] } };
    } };
    if (id === '@/lib/planningFailure') return { planningFailureExplanation: () => null };
    if (id.endsWith('.css')) return { default: {} };
    return {};
  } });
  function render() { cursor = 0; return exports.default(); }
  return { calls, nextInputs, async loaded() {
    render(); effects.splice(0).forEach(effect => effect());
    await new Promise(resolve => setImmediate(resolve));
    return render();
  } };
}

test('local plans and skipped journeys allow incomplete resources; strict hosted journeys retain their gate across domains', async () => {
  for (const ventureId of ['ven_inventory', 'ven_gardening']) for (const local of [false, true]) for (const skipped of [{}, { research: {} }]) {
    const app = harness(skipped, local, ventureId), tree = await app.loaded();
    const button = nodes(tree).find(node => node.props?.['data-usage-control'] === 'plan_estimate');
    const canGenerate = local || Object.keys(skipped).length > 0;
    assert.equal(button.props.disabled, !canGenerate);
    assert.equal(app.nextInputs.at(-1).resourcesComplete, canGenerate);
    await button.props.onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(app.calls.some(call => call.path.endsWith('/estimate-generation') && call.init.method === 'POST'), canGenerate);
    if (canGenerate) assert.match(JSON.stringify(tree), /Missing research and resources are labelled as gaps/);
  }
});

test('your requested estimate continues without a second approval in unrelated domains', async () => {
  const source = ts.createSourceFile('page.tsx', readFileSync(join(__dirname, 'page.tsx'), 'utf8'), ts.ScriptTarget.Latest, true);
  let method;
  function visit(node) { if (ts.isFunctionDeclaration(node) && node.name?.text === 'estimate') method = node.getText(source); ts.forEachChild(node, visit); }
  visit(source);
  for (const domain of ['Factory procurement', 'Family gardening']) {
    const calls = [], context = {blocked: false, resourceDirty: false, resources: {inventory: {complete: true}}, planningWithAssumptions: false, ventureId: domain, active: {current: domain}, plan: null, request: '', setBusy() {}, setQuote() {}, setMessage() {}, setQuoteBody() {}, api: async () => ({ok: true, data: {sufficient_now: true}}), apiFailure: String};
    context.generate = async (...args) => calls.push(args);
    vm.runInNewContext(ts.transpileModule(method + ';globalThis.runRequested = estimate;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    await context.runRequested(false);
    assert.equal(calls.length, 1);
    assert.ok(calls[0][0]);
  }
});
