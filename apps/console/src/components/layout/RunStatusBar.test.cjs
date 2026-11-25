const assert = require('node:assert/strict'), test = require('node:test');
const fs = require('node:fs'), vm = require('node:vm'), ts = require('typescript');
const {runStatusPresentation} = require('../../lib/runStatusPresentation.ts');

test('the live status line presents saved Markdown as plain text', () => {
  const tree = ts.createSourceFile('RunStatusBar.tsx', fs.readFileSync(`${__dirname}/RunStatusBar.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  let statusLine;
  function find(node) {
    if (!statusLine && ts.isJsxExpression(node) && node.getText(tree).includes('Reconnecting to saved work')) statusLine = node.expression.getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree);
  assert.ok(statusLine);
  const formatter = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'shortActivity');
  const shortActivity = vm.runInNewContext(`(${formatter.getText(tree)})`);
  for (const detail of ['Dispatch teams reconcile delivery records', 'Families plan meals around allergies']) {
    const presentation = runStatusPresentation({state: 'working', activity: `- **Problem:** ${detail}. Idea: other saved context.`}, 0);
    const scope = {offline: false, presentation, shortActivity};
    assert.equal(vm.runInNewContext(statusLine, scope), `Problem: ${detail}.`);
    const longStatus = vm.runInNewContext(statusLine, {...scope, presentation: {activity: `${detail} `.repeat(6).trim()}});
    assert.ok(longStatus.length <= 120 && longStatus.endsWith('…'));
    assert.ok(`${detail} `.repeat(6).startsWith(`${longStatus.slice(0, -1)} `));
  }
});
