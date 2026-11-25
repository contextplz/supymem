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

function harness(queue) {
  let cursor = 0;
  const state = [], effects = [], routes = [], requests = [];
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in state)) state[index] = initial;
      return [state[index], value => { state[index] = value; }];
    },
    useCallback: callback => callback,
    useEffect: callback => effects.push(callback),
  };
  const jsx = (type, props) => ({ type, props });
  const exports = {};
  const output = ts.transpileModule(readFileSync(join(__dirname, 'page.tsx'), 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  vm.runInNewContext(output, { exports, console, window: { setInterval: () => 1, clearInterval() {} }, require(id) {
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id === 'next/navigation') return { useParams: () => ({ ventureId: 'venture-a' }), useRouter: () => ({ push: href => routes.push(href) }) };
    if (id === '@/lib/client') return {
      ensureSession: async () => {},
      apiSwr: async (path, callback) => callback({ ok: true, data: path.endsWith('/tasks') ? queue : { name: 'Bakery' } }),
      api: async path => { requests.push(path); return { ok: true, data: queue }; },
    };
    if (id === '@/lib/format') return { displayName: name => name };
    if (id === '@/lib/taskWorkflow') return { buildPlanTaskId: () => '', planTaskId: () => '' };
    if (id === '@/lib/taskApi') return { estimatedDeadline: () => 'being checked' };
    if (id.endsWith('.css')) return { default: {} };
    return new Proxy({}, { get: (_target, name) => String(name) });
  } });
  function render() { cursor = 0; return exports.default(); }
  return { routes, requests, async loaded() {
    render(); effects.splice(0).forEach(effect => effect());
    await new Promise(resolve => setImmediate(resolve));
    return render();
  } };
}

test('empty Work offers exactly one usable server-selected action, without NO DATA or Replan', async () => {
  for (const action of [{ label: 'Make a plan', href: '/v/venture-a/plan' }, { label: 'Find customers', href: '/gtm?venture=venture-a' }]) {
    const app = harness({ items: [], running: [], replan_available: true, empty_action: action });
    const tree = await app.loaded();
    assert.equal(tree.props.headerRight, null);
    const buttons = nodes(tree).filter(node => node.type === 'button');
    assert.equal(buttons.length, 1);
    assert.equal(buttons[0].props.children, action.label);
    buttons[0].props.onClick();
    assert.deepEqual(app.routes, [action.href]);
    assert.deepEqual(app.requests, []);
    assert.doesNotMatch(JSON.stringify(tree), /ProvenanceChip|NO DATA|zero work completed|plan is locked/);
  }
});

test('unfinished prerequisites hide Replan even when saved work exists', async () => {
  const app = harness({ items: [{ id: 'task-a', title: 'Check the offer' }], running: [], replan_available: false });
  assert.equal((await app.loaded()).props.headerRight, null);
});

test('running work is not presented as an empty queue', async () => {
  const app = harness({ items: [], running: [{ id: 'task-a', title: 'Check the offer' }], replan_available: true,
    empty_action: { label: 'Make a plan', href: '/v/venture-a/plan' } });
  const tree = await app.loaded();
  assert.equal(tree.props.headerRight.props.children, 'Replan');
  assert.ok(!nodes(tree).some(node => node.props?.children === 'No work in the queue yet'));
});
