const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(file, imports = require) {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS },
  }).outputText, { exports, require: imports });
  return exports;
}
const helpers = load(path.join(__dirname, '../../lib/productBuildCaps.ts'));
function fixture(props = {}) {
  let state, prepared = [], dirty = [];
  const { ProductBuildCapsEditor } = load(path.join(__dirname, 'ProductBuildCapsEditor.tsx'), name =>
    name === 'react' ? { useState(initial) { state ??= initial; return [state, next => { state = next; }]; } }
      : name === '@/lib/productBuildCaps' ? helpers
      : name === '@/components/primitives/ErrorNotice' ? { ErrorNotice: () => null } : require(name));
  const saved = { max_iterations: 2, max_wall_seconds: 1440, per_call_seconds: 720, max_turns: 20 };
  const args = { caps: saved, provider: 'claude', phase: 'plan', disabled: false, freshReview: false,
    onDirtyChange: value => dirty.push(value), onPrepare: async value => prepared.push(value), ...props };
  const render = () => ProductBuildCapsEditor(args);
  function all(node, type) {
    if (!node || typeof node !== 'object') return [];
    return [...(node.type === type ? [node] : []), ...[node.props?.children].flat(Infinity).flatMap(child => all(child, type))];
  }
  return { render, all, prepared, dirty, saved };
}

test('actual input handlers clear acknowledgment before preparing explicit narrow limits', () => {
  const view = fixture();
  for (const [control, value] of [['tries', '1'], ['total', '300'], ['call', '300']]) {
    const input = view.all(view.render(), 'input').find(node => node.props['data-usage-control'] === `product_build_limit_${control}`);
    input.props.onChange({ target: { value } });
    assert.equal(view.dirty.at(-1), true);
  }
  view.render().props.onSubmit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(JSON.stringify(view.prepared)), [{ max_iterations: 1, max_wall_seconds: 300,
    per_call_seconds: 300, max_turns: 20 }]);
  assert.deepEqual(view.saved, { max_iterations: 2, max_wall_seconds: 1440, per_call_seconds: 720, max_turns: 20 });
});

test('invalid, unchanged and busy drafts cannot submit a replacement', () => {
  for (const props of [{}, { disabled: true, freshReview: true }]) {
    const view = fixture(props);
    view.render().props.onSubmit({ preventDefault() {} });
    assert.equal(view.prepared.length, 0);
  }
  const view = fixture();
  view.all(view.render(), 'input').find(node => node.props['data-usage-control'] === 'product_build_limit_total')
    .props.onChange({ target: { value: '300' } });
  view.render().props.onSubmit({ preventDefault() {} });
  assert.equal(view.prepared.length, 0);
  assert.equal(view.all(view.render(), 'button').find(node => node.props.type === 'submit').props.disabled, true);
});

test('closed-plan coding review includes all selected caps and hides Codex turns', () => {
  const view = fixture({ provider: 'codex', phase: 'implement', freshReview: true,
    caps: { max_iterations: 6, max_wall_seconds: 1200, per_call_seconds: 300, max_turns: null } });
  assert.equal(view.all(view.render(), 'input').length, 3);
  view.render().props.onSubmit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(JSON.stringify(view.prepared)), [{ max_iterations: 6, max_wall_seconds: 1200,
    per_call_seconds: 300, max_turns: null }]);
});

test('reset restores saved draft without transferring an approval', () => {
  const view = fixture();
  view.all(view.render(), 'input')[0].props.onChange({ target: { value: '1' } });
  view.all(view.render(), 'button').find(node => node.props['data-usage-control'] === 'product_build_limits_reset').props.onClick();
  assert.equal(view.dirty.at(-1), false);
  view.render().props.onSubmit({ preventDefault() {} });
  assert.equal(view.prepared.length, 0);
});
