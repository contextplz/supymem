const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('a confirmed personal build uses the connected tool and opens its venture receipt', async () => {
  const page = readFileSync(join(__dirname, 'page.tsx'), 'utf8');
  const action = page.slice(page.indexOf('  async function preparePersonalBuild()'), page.indexOf('  async function retryValidation()'));
  const code = ts.transpileModule(`export ${action.trim()}`, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText;
  for (const ventureId of ['ven_invoice', 'ven_recipe_book']) {
    const calls = [], notices = [], paths = [], exports = {};
    const pendingAction = {current: false};
    let result = {ok: true, data: {id: 'task_saved', venture_id: ventureId}};
    vm.runInNewContext(code, {exports, ventureId, pendingAction, activeVenture: {current: ventureId},
      local: true, busy: false, syncError: '', session: {state: 'confirmed', project_intent: 'personal'},
      verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => 1, ensureSession: async () => {},
      setBusy() {}, setNotice: text => notices.push(text), apiErrorCode: value => value.data?.detail?.code,
      apiFailure: () => 'Try again', activeMacTaskReviewPath: value => value.data?.detail?.review_path,
      api: async (path, request) => {calls.push([path, JSON.parse(request.body)]); return result;},
      window: {location: {assign: path => paths.push(path)}},
    });
    await Promise.all([exports.preparePersonalBuild(), exports.preparePersonalBuild()]);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], '/api/coding/dispatch');
    assert.deepEqual(Object.keys(calls[0][1]).sort(), ['kind', 'prompt', 'venture_id']);
    assert.equal(calls[0][1].kind, 'product');
    assert.equal(calls[0][1].venture_id, ventureId);
    assert.match(calls[0][1].prompt, /personal project.*own use.*private.*no market research.*publishing/i);
    assert.doesNotMatch(calls[0][1].prompt, /codex|claude|cursor|opencode/i);
    assert.deepEqual(paths, [`/v/${ventureId}/device-work-orders/task_saved`]);
    result = {ok: false, status: 409, data: {detail: {code: 'build_tool_needs_setup'}}};
    await exports.preparePersonalBuild();
    assert.match(notices.at(-1), /Coding tools/);
    assert.match(page, /href="\/settings\/local#coding-tools"/);
    result = {ok: true, data: {id: 'other_task', venture_id: 'other_venture'}};
    await exports.preparePersonalBuild();
    assert.match(notices.at(-1), /Open Builds and refresh/);
    assert.equal(paths.length, 1);
  }
});

test('saved-work recovery tells you the next action without credit or paid-attempt copy', () => {
  const page = readFileSync(join(__dirname, 'page.tsx'), 'utf8');
  const recovery = page.match(/const displayNotice = ([^;]+);/)[1];
  for (const audience of ['Shipping agents', 'People learning pottery']) {
    const notice = `Your idea for ${audience} is saved.`;
    const context = {session: {credits_continue_job_id: 'saved_job'}, notice};
    assert.equal(vm.runInNewContext(recovery, context), 'You can continue from your saved work. Select Continue.');
    context.session = {};
    assert.equal(vm.runInNewContext(recovery, context), notice);
  }
  assert.doesNotMatch(page, /Credits are available now|malformed paid attempt|new paid clarification/);
});

test('startup handoff says it confirms your idea across unrelated audiences', () => {
  const page = readFileSync(join(__dirname, 'page.tsx'), 'utf8');
  const button = page.match(/<button[^\n]+data-usage-control=\{personalProject \? "idea_confirm_personal" : "idea_continue_research"\}[^\n]+<\/button>/)?.[0];
  assert.ok(button);
  const code = ts.transpileModule(`export default function Confirm() { return ${button}; }`, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  for (const customer of ['commercial electricians', 'home gardeners']) {
    const calls = [], exports = {};
    const jsx = (type, props) => ({ type, props });
    vm.runInNewContext(code, { exports, require: () => ({ jsx, jsxs: jsx }),
      session: { next_href: '/v/venture/context', current_revision: { structured: { customer } } },
      ventureId: 'venture', personalProject: false, busy: false, jobId: null, syncError: null,
      continueToResearch: value => calls.push(value),
    });
    const rendered = exports.default();
    assert.equal(rendered.props.children, 'Confirm your idea');
    assert.equal(rendered.props.disabled, false);
    rendered.props.onClick();
    assert.deepEqual(calls, [false]);
  }
});

test('saved clarification answers show progress within Idea without completing a milestone', () => {
  const page = readFileSync(join(__dirname, 'page.tsx'), 'utf8');
  const progress = page.slice(page.indexOf('{session && currentStep === 0 &&'), page.indexOf('\n\n        {idea && <section'));
  assert.ok(progress.length > 0);
  const code = ts.transpileModule(`export default function Progress() { return <section>${progress}</section>; }`, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const { previousIdeaAnswers } = require('../../../../lib/ideaAnswer.ts');
  const { ideaJourneyProgress } = require('../../../../lib/companyJourney.ts');
  for (const audience of ['distribution center managers', 'weekend birdwatchers']) {
    for (const count of [0, 1, 3]) {
      const turns = Array.from({ length: count }, (unused, index) => [
        { id: `question-${index}`, role: 'agent', kind: 'question', text: 'Who needs this?' },
        { id: `answer-${index}`, role: 'founder', kind: 'answer', text: audience },
      ]).flat();
      const session = { state: 'open', turns };
      const previousAnswers = previousIdeaAnswers(turns);
      const currentStep = ideaJourneyProgress(session.state, false);
      assert.equal(currentStep, 0);
      for (const lastQuestion of [null, { id: 'next-question' }]) {
        const exports = {};
        const jsx = (type, props) => ({ type, props });
        vm.runInNewContext(code, { exports, require: () => ({ jsx, jsxs: jsx }),
          session, previousAnswers, currentStep, lastQuestion, styles: {},
        });
        const tree = exports.default();
        const text = value => Array.isArray(value) ? value.map(text).join('') : value && typeof value === 'object'
          ? text(value.props?.children) : typeof value === 'string' || typeof value === 'number' ? String(value) : '';
        if (count || lastQuestion) {
          assert.ok(text(tree).includes(`${count} ${count === 1 ? 'answer saved' : 'answers saved'} in your idea phase.`));
          assert.equal(tree.props.children.props.role, 'status');
        } else assert.equal(text(tree), '');
      }
    }
  }
});

test('idea card shows findings, opening and runnable next steps across unrelated audiences', () => {
  const page = readFileSync(join(__dirname, 'page.tsx'), 'utf8');
  const card = page.slice(page.indexOf('<h2>Your idea review</h2>'), page.indexOf('{session.case === "explore" && <p>You can keep this opening'));
  assert.ok(card.length > 0);
  const code = ts.transpileModule(`export default function Review() { return <section>${card}</section>; }`, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  for (const customer of ['commercial electricians', 'home gardeners']) {
    const exports = {};
    const jsx = (type, props) => ({ type, props });
    const testIdea = { hypothesis: `${customer} would try a focused first version.`, action: 'Prepare a consent-safe interview outline.', success_measure: 'Record actual commitments.' };
    const opening = `Your best opening: start with ${customer}, positioned around one useful result. Use a focused page to reach them.`;
    vm.runInNewContext(code, { exports, require: () => ({ jsx, jsxs: jsx }),
      reviewValidations: Array.from({ length: 3 }, (unused, index) => ({ id: `review-${index}`, report: { summary: 'The idea is an empty container.', biggest_risk: 'No identified customer', gaps: ['No description'],
        forcing_questions: [{ question: 'Problem: got no clue help me out' }], proposed_tests: [testIdea],
        presentation: { findings: [{ assessment: `You described a workflow for ${customer}.` }], opening, options: [] } } })),
      styles: {}, startedTests: {}, suggestedExperiment: null, personalProject: false, ventureId: 'venture', Link: 'a', startTest() {},
    });
    const tree = exports.default();
    const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === 'object' ? [value, ...nodes(value.props?.children)] : [];
    const rendered = nodes(tree);
    const headings = rendered.filter(node => node.type === 'h3').map(node => node.props.children);
    assert.deepEqual(headings, ['What we found', 'Your opening', "What I'd do next"]);
    const text = JSON.stringify(tree);
    assert.ok(text.includes(customer) && text.includes(opening) && text.includes(testIdea.action));
    assert.equal(rendered.filter(node => node.type === 'p' && node.props.children === `You described a workflow for ${customer}.`).length, 1);
    assert.ok(rendered.some(node => node.type === 'button' && node.props.children === 'Start this test'));
    assert.ok(!/Biggest risk|Gaps:|Strengths:|empty container|no clue|No description|No identified|This is an AI assessment/.test(text));
  }
});
