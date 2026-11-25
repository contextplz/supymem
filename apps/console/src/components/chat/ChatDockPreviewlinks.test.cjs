const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

test('Show status here fetches your venture result instead of only opening an empty tray across domains', async () => {
  const source = ts.createSourceFile('ChatDock.tsx', fs.readFileSync(__dirname + '/ChatDock.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let branch;
  function visit(node) {
    if (ts.isIfStatement(node) && node.expression.getText(source) === 'action === "status" && local && !unavailable') branch = node.getText(source);
    ts.forEachChild(node, visit);
  }
  visit(source);
  for (const domain of ['Supplier bookkeeping', 'Weekend dance classes']) {
    let opened;
    const context = {action: 'status', local: true, unavailable: undefined, payload: {}, workKey: domain,
      setBuildStatusScope: value => {opened = value;}};
    const continued = await vm.runInNewContext(ts.transpileModule(`(async () => {${branch}; return true;})()`, {
      compilerOptions: {target: ts.ScriptTarget.ES2022},
    }).outputText, context);
    assert.equal(opened, domain);
    assert.equal(continued, true, 'Your status action must reach the saved venture status request');
  }
});

test('your completed coding task is from Charlie across domains', () => {
  const source = ts.createSourceFile('ChatDock.tsx', fs.readFileSync(__dirname + '/ChatDock.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const speaker = source.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'speakerName');
  const context = {exports: {}};
  vm.runInNewContext(ts.transpileModule(speaker.getText(source) + '; exports.speakerName = speakerName;', {
    compilerOptions: {module: ts.ModuleKind.CommonJS},
  }).outputText, context);
  for (const domain of ['Fleet maintenance', 'Neighborhood running']) {
    assert.equal(context.exports.speakerName('assistant', null, 'mac_task_finished'), 'Charlie', domain);
    assert.equal(context.exports.speakerName('founder'), 'You');
  }
});

test('expanded task status reserves space instead of covering the conversation across domains', () => {
  for (const domain of ['Team scheduling', 'Home recipes']) {
    const tray = fs.readFileSync(__dirname + '/AgentsTray.module.css', 'utf8');
    const chat = fs.readFileSync(__dirname + '/ChatDock.module.css', 'utf8');
    assert.match(tray.match(/\.tray\s*\{[^}]+\}/)[0], /position:\s*relative/, domain);
    assert.match(chat, /\.header:has\([^}]+height:\s*auto/s, domain);
  }
});

test('the ready status panel delegates private links to the authenticated chat link across domains', () => {
  const filename = __dirname + '/../desktop/ProductBuildReview.tsx';
  const source = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const hrefTags = [];
  function visit(node) {
    if (ts.isJsxOpeningElement(node) && node.attributes.getText(source).match(/href=\{(?:pagePreview|source)/)) hrefTags.push(node.tagName.getText(source));
    ts.forEachChild(node, visit);
  }
  visit(source);
  for (const domain of ['Invoice reminders', 'Cycling routes']) assert.deepEqual(hrefTags, ['ChatAnchor', 'ChatAnchor'], domain);
});
