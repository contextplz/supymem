const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

test('the existing Market map entry opens one venture-scoped experiments view without touching chat', () => {
  for (const ventureId of ['ven_wholesale', 'ven_balcony']) {
    const exports = {}, jsx = (type, props) => ({ type, props });
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/page.tsx`, 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText, { exports, require(name) {
      if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx };
      if (name === 'next/navigation') return { useParams: () => ({ ventureId }) };
      return { AppShell: 'shell', ExperimentsView: 'experiments-view' };
    } });
    const tree = exports.default(), view = tree.props.children.props.children.props.children;
    assert.equal(tree.props.title, 'Market map'); assert.equal(tree.props.ventureId, ventureId);
    assert.equal(view.type, 'experiments-view'); assert.equal(view.props.ventureId, ventureId);
    assert.match(readFileSync(`${__dirname}/../../../../components/layout/SideNav.tsx`, 'utf8'), /market-map.*Market map/);
  }
});
