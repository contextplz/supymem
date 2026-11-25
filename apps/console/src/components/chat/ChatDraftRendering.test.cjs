const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

test('your recovered chat notices never show a raw system attribution across domains', () => {
  const source = readFileSync(__dirname + '/ChatDock.tsx', 'utf8');
  const tree = ts.createSourceFile('ChatDock.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const speaker = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'speakerName');
  const context = {};
  vm.runInNewContext(ts.transpileModule(speaker.getText(tree), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
  for (const domain of ['warehouse teams', 'weekend walkers']) {
    assert.equal(context.speakerName('system'), 'Your update', domain);
    assert.equal(context.speakerName('user'), 'You');
    assert.equal(context.speakerName('assistant', {name: 'Charlie'}), 'Charlie');
  }
});

const dock = readFileSync(__dirname + '/ChatDock.tsx', 'utf8');
const markdown = ts.transpileModule(readFileSync(__dirname + '/../../lib/chatMarkdown.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const context = { exports: {} };
vm.runInNewContext(markdown, context);
const mergeContext = { exports: {} };
vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/../../lib/chatMerge.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, mergeContext);

test('one draft result keeps three separate raw copy blocks and one work link', () => {
  const drafts = [1, 2, 3].map(number => `Draft ${number}\nSubject: Trail Notebook ${number}\n\nBody ${number}`);
  const body = 'Here are the drafts for your review. Nothing was sent.\n\n'
    + drafts.map(draft => '```text\n' + draft + '\n```').join('\n\n');
  const result = { id: 'msg_drafts', role: 'assistant', body_md: body,
    actions: [{ label: 'Open work', href: '/v/ven_trail_notebook/tasks?task=one' }] };
  const messages = mergeContext.exports.mergeChat([result], [result], true);
  assert.equal(messages.length, 1);
  const blocks = context.exports.splitFences(messages[0].body_md).filter(segment => segment.code);
  assert.deepEqual(Array.from(blocks, block => block.text), drafts);
  assert.equal(blocks.length, 3);
  assert.equal(messages[0].actions.filter(action => action.label === 'Open work').length, 1);
  assert.match(dock, /splitFences\(shownText\)\.map/);
  assert.match(dock, /<pre>\{seg.text\}<\/pre>/);
  assert.match(dock, /aria-label="Copy message block"/);
});

for (const state of ['ready', 'rejected', 'unavailable']) {
  test(`copy button preserves the exact draft and offers selection when ${state}`, async () => {
    const body = dock.match(/onClick=\{async \(event\) => \{([\s\S]*?)\n\s+\}\}>Copy/)[1];
    const copied = [], button = { textContent: 'Copy' }, draft = 'Draft 1\nSubject: Trail Notebook\n\n<raw> — body';
    const navigator = state === 'unavailable' ? {} : { clipboard: { async writeText(text) {
      if (state === 'rejected') throw new Error('Denied');
      copied.push(text);
    } } };
    await vm.runInNewContext(`(async (event) => {${body}})(event)`, {
      event: { currentTarget: button }, navigator, seg: { text: draft }, setTimeout() {},
    });
    assert.equal(button.textContent, state === 'ready' ? 'Copied' : 'Select text to copy');
    assert.deepEqual(copied, state === 'ready' ? [draft] : []);
  });
}
