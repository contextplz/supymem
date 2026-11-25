const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const { readFileSync } = require('node:fs'), ts = require('typescript');
test('an empty customer list explains its purpose and gives one start button', () => {
  const jsx = (type, props) => ({ type, props }), exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/DailyQueue.tsx`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
  }).outputText, { exports, require(id) {
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id === './useOutreachAction') return { useOutreachAction: () => ({ pending: false }), ActionFeedback: 'feedback' };
    return { default: {}, ChatAnchor: 'a' };
  }});
  const nodes = node => Array.isArray(node) ? node.flatMap(nodes) : node?.props ? [node, ...nodes(node.props.children)] : [];
  for (const scenario of [
    { audience: 'Finance teams at manufacturers', remaining: { email: 20 } },
    { audience: 'Parents planning accessible holidays', remaining: { dm: 10 } },
  ]) {
    const tree = exports.DailyQueue({ items: [], ready: true, remaining: scenario.remaining, onPick() { throw new Error(`No person to pick for ${scenario.audience}`); } });
    const links = nodes(tree).filter(node => node.type === 'a');
    assert.equal(links.length, 1, scenario.audience); assert.equal(links[0].props.href, '/gtm'); assert.equal(links[0].props.children, 'Find my first customers');
    assert.match(JSON.stringify(tree), /real people and where you found them/);
    assert.doesNotMatch(JSON.stringify(tree), /\u2014/);
  }
});
