const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

function compile(file, mocks = {}) {
  const code = ts.transpileModule(readFileSync(join(__dirname, file), 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const exports = {}, jsx = (type, props) => typeof type === 'function' ? type(props) : ({ type, props });
  vm.runInNewContext(code, { exports, require(id) {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };

    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id.endsWith('.css')) return { default: {} };
    if (id in mocks) return mocks[id];
    throw new Error(id);
  } });
  return exports;
}
function nodes(tree) {
  if (!tree) return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return typeof tree === 'object' ? [tree, ...nodes(tree.props?.children)] : [];
}
const { ResearchProgress } = compile('ResearchProgress.tsx');
const saved = { state: 'leased', percent: 37, step_label: 'Reading customer complaints', started_at: null };

test('completed receipts render a thin, accessible determinate progress bar', () => {
  const rendered = nodes(ResearchProgress({ progress: saved }));
  const bar = rendered.find(node => node.type === 'progress');
  assert.equal(bar.props.role, 'progressbar');
  assert.equal(bar.props['aria-valuenow'], 37);
  assert.equal(bar.props['aria-label'], saved.step_label);
  assert.equal(bar.props.value, 37);
  assert.match(JSON.stringify(rendered), /37%/);
});
test('queued work is indeterminate; unknown or invalid progress only shows the step', () => {
  const queued = nodes(ResearchProgress({ progress: { ...saved, state: 'queued', percent: 0 } }));
  const bar = queued.find(node => node.type === 'progress');
  assert.equal(bar.props.value, undefined);
  assert.equal(bar.props['aria-valuenow'], undefined);
  assert.doesNotMatch(JSON.stringify(queued), /0%/);
  for (const percent of [null, NaN, -1, 101]) {
    const unknown = nodes(ResearchProgress({ progress: { ...saved, percent } }));
    assert.equal(unknown.some(node => node.type === 'progress'), false);
    assert.ok(JSON.stringify(unknown).includes(saved.step_label));
  }
  assert.equal(ResearchProgress({}), null);
});
test('the top status banner renders the same durable research progress', () => {
  const snapshot = { venture: { id: 'venture', name: 'Harbor' }, needs_you: [],
    run_status: { state: 'working', stage: 'Research', activity: 'Checking competitors', research_progress: saved } };
  const { RunStatusBar } = compile('../layout/RunStatusBar.tsx', {
    react: { useEffect() {}, useState: initial => [typeof initial === 'function' ? initial() : initial, () => {}] },
    'next/navigation': { usePathname: () => '/v/venture/research' },
    '@/lib/client': { verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => 1 },
    '@/lib/apiCache': { cacheGet: () => ({ data: snapshot }) },
    '@/lib/nextStep': { dedupeStatusCta: () => null },
    '@/lib/runStatusPresentation': { isPreparingFirstQuestion: () => false,
      runStatusPresentation: () => ({ state: 'Running', activity: 'Checking competitors' }), statusBarAction: () => null },
    '@/lib/taskApi': {},
    '@/lib/gtmProgress': compile('../../lib/gtmProgress.ts'),
    '@/components/desktop/EmergencyStopButton': { EmergencyStopButton: () => null },
    '@/components/primitives/ResearchProgress': { ResearchProgress },
  });
  const rendered = nodes(RunStatusBar({ ventureId: 'venture' }));
  assert.equal(rendered.find(node => node.type === 'progress').props.value, 37);
  assert.equal(rendered.find(node => node.props?.['aria-label'] === 'Run status').props['data-research-progress'], true);
});
