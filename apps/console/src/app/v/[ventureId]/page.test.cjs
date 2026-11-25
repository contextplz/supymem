const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const { BETA_PATH, betaStageIndex } = require('../../../lib/betaPath.ts');

test('company loading finishes after session verification and exposes a retry on read failures across domains', async () => {
  const source = readFileSync(join(__dirname, 'page.tsx'), 'utf8');
  const tree = ts.createSourceFile('page.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let load;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'load') load = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  const code = ts.transpileModule(`${load}\nexports.load = load;`, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText;
  for (const name of ['Factory inventory alerts', 'Neighborhood dog walking']) {
    for (const fails of [false, true]) {
      let epoch = 0, snapshot, busy = false, errors = [];
      const context = {exports: {}, ventureId: 'venture', current: {current: 'venture'}, generation: {current: 0},
        verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => epoch,
        ensureSession: async () => {epoch++;}, setBusy: value => {busy = value;},
        setSnapshot: value => {snapshot = value;}, setErrors: value => {errors = value;},
        cacheGet: () => null, cachePut() {}, prefetchApi() {}, URLSearchParams,
        apiFailure: () => 'Open your chat and choose Refresh to retry.',
        api: async path => {
          if (path === '/api/ventures/venture') {
            if (fails) throw new Error('read failed');
            return {ok: true, data: {name, workspace_id: 'workspace'}};
          }
          return {ok: true, data: {items: [], tiles: {}}};
        }};
      vm.runInNewContext(code, context);
      await context.exports.load();
      assert.equal(busy, false);
      if (fails) assert.match(errors.join(' '), /Refresh|retry/i);
      else assert.equal(snapshot.company.name, name);
    }
  }
});

test('startup context is a prerequisite without resetting your observed phase', () => {
  const page = readFileSync(join(__dirname, 'page.tsx'), 'utf8');
  const stageExpression = page.match(/  const stage =[^;]+;/)[0];
  const phaseExpression = page.match(/  const at =[^;]+;/)[0];
  const card = page.slice(page.indexOf('{snapshot && nextStep && focusedPath && <OneCard'), page.indexOf('\n        details={<>'));
  const code = ts.transpileModule(`export default function Context() {
    ${stageExpression} ${phaseExpression}
    return <section>${card} />}</section>;
  }`, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;
  for (const rawIdea of ['Shift scheduling for factory supervisors', 'Community trails for recreational cyclists']) {
    for (const [founderStage, published, expectedPhase] of [
      ['idea', false, 0], ['building', false, 1], ['page_live', true, 4], ['outreach', false, 4], ['traction', false, 4],
    ]) {
      assert.equal(betaStageIndex('venture.context', published, founderStage), expectedPhase);
      const exports = {};
      const jsx = (type, props) => ({ type, props });
      const serverStep = { founder_stage: { stage: founderStage } };
      vm.runInNewContext(code, { exports, require: () => ({ jsx, jsxs: jsx }), BETA_PATH, betaStageIndex,
        snapshot: { company: { raw_idea: rawIdea }, websitePublished: published }, serverStep,
        key: 'venture.context', idea: { state: 'confirmed' }, target: null, gtm: false, focusedPath: true,
        nextStep: { title: 'Review your startup context', detail: 'Review your saved answers.' }, cta: null, OneCard: 'card',
      });
      const body = exports.default().props.children.props.body;
      assert.ok(body.startsWith(`Your current phase: ${BETA_PATH[expectedPhase].label}.`));
      assert.ok(body.includes('Complete your startup context to continue.'));
      assert.equal(betaStageIndex('venture.context', false, 'idea'), 0, 'A corrected phase must not retain earlier progress');
    }
  }
});
