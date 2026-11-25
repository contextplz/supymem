const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const {readFileSync} = require('node:fs'), ts = require('typescript');
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : tree?.props ? [tree, ...nodes(tree.props.children)] : [];
test('26: all Forget buttons become unavailable while your first memory deletion is pending', async () => {
  for (const domain of ['Warehouse logistics', 'Watercolor classes']) {
    const slots = [], effects = [], requests = [], exports = {}; let cursor = 0, finish;
    const jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/VentureMemoryPanel.tsx`, 'utf8'), {compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    }}).outputText, {exports, require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {useState(initial) {const index = cursor++; if (!(index in slots)) slots[index] = initial;
        return [slots[index], value => {slots[index] = typeof value === 'function' ? value(slots[index]) : value;}];},
        useCallback(callback, deps) {const index = cursor++; if (!slots[index]) slots[index] = {callback, deps}; return slots[index].callback;},
        useEffect(callback, deps) {const index = cursor++; if (!slots[index]) {slots[index] = deps; effects.push(callback);}}};
      if (id.endsWith('/client')) return {api: async (path, options) => {requests.push({path, options});
        if (options) return new Promise(resolve => {finish = resolve;});
        return {ok: true, data: {items: ['first', 'second'].map(id => ({id, kind: 'preference', statement: domain}))}};}};
      return {ErrorNotice: 'error'};
    }});
    const render = () => {cursor = 0; const tree = exports.VentureMemoryPanel({ventureId: 'ven_domain'}); effects.splice(0).forEach(effect => effect()); return tree;};
    render(); await new Promise(resolve => setImmediate(resolve));
    const buttons = () => nodes(render()).filter(node => node.props['data-usage-control'] === 'settings_memory_delete');
    buttons()[0].props.onClick(); assert.ok(buttons().every(button => button.props.disabled));
    buttons()[1].props.onClick(); assert.equal(requests.filter(request => request.options).length, 1);
    finish({ok: true}); await new Promise(resolve => setImmediate(resolve));
    assert.equal(buttons().length, 1); assert.equal(buttons()[0].props.disabled, false);
  }
});
