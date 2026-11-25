const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const dock = readFileSync(path.join(__dirname, 'ChatDock.tsx'), 'utf8');
const jsx = (type, props) => ({ type, props });
const fragment = dock.match(/\{(!own && m\.kind === "exploration_options"[^\n]+) : searchProgress \?/)[1];

test('ChatDock replaces the exploration event body with one card at its existing message position in both domains', () => {
  assert.match(dock, /import \{ ExplorationCard \} from "\.\/ExplorationCard";/);
  assert.equal((dock.match(/<ExplorationCard\b/g) || []).length, 1);
  for (const ventureId of ['ven_stock', 'ven_walk']) {
    const context = { exports: {}, require: () => ({ jsx, jsxs: jsx }), ExplorationCard: 'exploration', ventureId,
      m: { id: `${ventureId}_round`, kind: 'exploration_options' }, own: false, disabled: false, onAction: async () => true };
    const source = ts.transpileModule(`exports.out = ${fragment} : null;`, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText;
    const render = () => vm.runInNewContext(`(() => { ${source} })()`, context);
    render();
    assert.equal(context.exports.out.type, 'exploration'); assert.equal(context.exports.out.props.message, context.m);
    assert.equal(context.exports.out.props.ventureId, ventureId); assert.equal(context.exports.out.props.onAction, context.onAction);
    context.own = true; render(); assert.equal(context.exports.out, null);
    context.own = false; context.m.kind = 'plain'; render(); assert.equal(context.exports.out, null);
  }
});
