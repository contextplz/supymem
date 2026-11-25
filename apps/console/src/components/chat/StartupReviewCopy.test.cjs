const assert = require('node:assert/strict'), test = require('node:test');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript');
const source = fs.readFileSync(path.join(__dirname, '../../lib/validationCopy.ts'), 'utf8');
const context = {exports: {}};
vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText, context);

test('review copy suggests promising next moves without the retired assessment or risk announcements', () => {
  const {GSTACK_ABOUT, founderReviewText} = context.exports;
  for (const audience of ['Hospital billing teams', 'Backyard birdwatchers']) {
    const copy = founderReviewText(`GStack is Garry Tan's open-source founder toolkit for ${audience}`);
    assert.equal(copy, GSTACK_ABOUT);
    assert.match(copy, /promising directions/);
    assert.match(copy, /try next/);
    assert.match(copy, /haven't checked.*customers/);
    assert.doesNotMatch(copy, /AI assessment|not verified market research|biggest risk|\u2014/);
  }
});
