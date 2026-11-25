const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const { readFileSync } = require('node:fs'), ts = require('typescript');
const nodes = node => Array.isArray(node) ? node.flatMap(nodes) : node?.props ? [node, ...Object.values(node.props).flatMap(nodes)] : [];
const text = node => Array.isArray(node) ? node.map(text).join('') : node?.props ? text(node.props.children) : typeof node === 'string' ? node : '';
function renderJourney(icp, stage = 0, extra = {}, local = false, calls = []) {
  const value = { venture_id: 'ven_first', state: 'idle', discovery_started: stage !== 0, icp, prospects: [], drafts: [], batches: [],
    results: { sent: 0, replied: 0, replies: [] }, gmail: { connected: false }, daily_cap: 5, estimate: { basis: 'Review your estimate before starting.' }, ...extra };
  const state = [value]; state[9] = stage;
  let cursor = 0;
  const jsx = (type, props) => ({ type, props }), exports = {};
  const knownComponents = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/../chat/KnownAnswer.tsx`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
  }).outputText, { exports: knownComponents, require: () => ({ jsx, jsxs: jsx }) });
  const passthrough = value => value || '';
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/GtmJourney.tsx`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX }
  }).outputText, { exports, URL, window: {}, require(id) {
    if (id === 'react') return { useState(initial) { const index = cursor++; if (!(index in state)) state[index] = initial; return [state[index], next => { state[index] = next; }]; }, useRef: current => ({ current }), useCallback: fn => fn, useEffect() {} };
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id.endsWith('/gtmDiagnosisIntent')) return { gtmRequestFence: () => ({ capture: () => () => true }) };
    if (id.endsWith('/localMode')) return { isLocalConsole: () => local };
    if (id.endsWith('/gtmJourneyApi')) return {journeySendReason: code => code, journeyClient: {
      askSending: async owner => { calls.push(['askSending', owner]); return value; },
      send: async (_owner, _batch, draftIds, _cap, selfOnly) => { calls.push(['send', draftIds, selfOnly]); return value; },
    }, journeyReviewRequested: () => false, journeyInitialStage: () => stage};
    if (id.endsWith('/ideaAnswer')) return { editableIdeaAnswer: passthrough };
    if (id.endsWith('/researchBrief')) return { plainText: passthrough };
    if (id.endsWith('/reachableLeads')) return { guessedEmailWarning: () => '' };
    if (id.endsWith('/ErrorNotice')) return { useReportedError() {}, ErrorNotice: 'error' };
    if (id.endsWith('/KnownAnswer')) return knownComponents;
    return { default: 'link', OneCard: 'card', NextStepCard: 'next-step' };
  }});
  return () => { cursor = 0; return exports.GtmJourney({ owner: { actorId: 'usr_first', workspaceId: 'wsp_first', ventureId: 'ven_first' } }); };
}

test('reachable cards show the sourced reason rather than a generic pitch', () => {
  for (const [who, mode, domain, why] of [
    ['Independent bike repair shops', 'b2b', 'bikeshop.example', 'Repairs cargo bikes and e-bikes'],
    ['People gardening on balconies', 'consumer', 'gardening.example', 'Grows tomatoes in balcony containers'],
  ]) {
    const source_url = `https://${domain}/about`;
    const lead = { id: 'lead', name: who, evidence_label: 'fit_only', source_url, why, routes: [],
      value_exchange: 'Ask whether they would share their experience.' };
    const tree = renderJourney({ who, mode }, 1, { reachable_leads: [lead] }, true)();
    const card = nodes(tree).find(node => node.type?.name === 'ReachCard');
    const content = card.type(card.props);
    assert.ok(text(content).includes(`Why: ${why}`));
    const source = nodes(content).find(node => node.props.url === source_url);
    assert.equal(text(source), 'Public source');
  }
});

test('local journey failures keep your exact server recovery without overriding limits or uncertainty', async () => {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/../../lib/gtmJourneyApi.ts`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText, { exports, URL, require(moduleId) {
    if (moduleId.endsWith('localMode.ts')) return { isLocalConsole: () => true };
    if (moduleId.endsWith('credits.ts')) return { usageLimitMessage: () => 'Open Settings to continue.' };
    if (moduleId.endsWith('apiFailure.ts')) return { apiFailure: () => 'Generic recovery.' };
    if (moduleId.endsWith('errorCopy.ts')) return { errorCodeCopy: () => undefined };
    return {};
  }});
  for (const audience of ['Clinic appointment coordinators', 'Families tending seedlings']) {
    const owner = { actorId: 'owner', workspaceId: 'workspace', ventureId: audience };
    for (const [code, message] of [
      ['gtm_send_blocked', `Open Settings > Keys and connections > Your AI provider, save your model and API key, then find real ${audience}.`],
      ['gtm_approval_changed', `Open Find customers and review your saved drafts for ${audience}.`],
      ['gtm_outcome_uncertain', 'Open Find customers and review your saved send records before retrying.'],
    ]) {
      const client = exports.createJourneyClient(async () => ({ ok: false, status: 409, data: { detail: { code, message } } }));
      await assert.rejects(client.prepare(owner), error => error.message === message);
    }
    const limited = exports.createJourneyClient(async () => ({ ok: false, status: 402, data: { detail: { message: 'Private limit detail.' } } }));
    await assert.rejects(limited.prepare(owner), /Open Settings to continue/);
    const timedOut = exports.createJourneyClient(async () => ({ ok: false, status: 504, data: { detail: { message: 'Nothing was sent.' } } }));
    await assert.rejects(timedOut.prepare(owner), /may already have completed/);
  }
});

test('journey known context replaces repeated intake with editable assumptions and source choices', () => {
  for (const audience of ['Municipal water auditors', 'Teenage chess learners']) {
    for (const status of ['known', 'confirm', 'conflict']) {
      const render = renderJourney({ who: status === 'conflict' ? '' : audience, problem: 'Your customers lose time scheduling visits.',
        needs_input: status !== 'known', known_checks: { audience: { status, value: audience, source: 'Your saved context' } } });
      let tree = render();
      const card = nodes(tree).find(node => node.type?.name === 'KnownAnswer');
      assert.equal(card.props.check.value, audience);
      const saved = card.type(card.props);
      assert.match(text(saved), new RegExp(audience));
      assert.match(text(saved), /Your saved context/);
      assert.equal(nodes(tree).some(node => node.type === 'textarea' && node.props['aria-label'] === 'Who should we target?'), false);
      if (status !== 'known') card.props.onSelect(audience);
      else card.props.onEdit();
      tree = render();
      assert.ok(text(tree).includes(audience) || nodes(tree).some(node => node.props.value === audience));
      assert.match(nodes(tree).find(node => node.type === 'card').props.body, /Nothing is sent/);
    }
  }
});

test('customer setup asks one missing question and keeps saved website answers across unrelated audiences', () => {
  for (const [audience, problem] of [['Freight dispatch teams', 'Lost delivery schedules'], ['Weekend hikers', 'Finding accessible trails']]) {
    const render = renderJourney({ who: '', problem: '', needs_input: true });
    let tree = render(), fields = nodes(tree).filter(node => node.props['aria-label'] === 'Who should we target?' || node.props['aria-label'] === 'What problem do you solve?');
    assert.equal(fields.length, 1);
    fields[0].props.onChange({ target: { value: audience } });
    tree = render();
    assert.doesNotMatch(text(tree), /Tell us what problem you solve/);
    nodes(tree).find(node => node.type === 'button' && text(node) === 'Continue').props.onClick();
    fields = nodes(render()).filter(node => node.props['aria-label'] === 'Who should we target?' || node.props['aria-label'] === 'What problem do you solve?');
    assert.equal(fields.length, 1); assert.equal(fields[0].props['aria-label'], 'What problem do you solve?');
    fields[0].props.onChange({ target: { value: problem } });
    assert.equal(nodes(render()).find(node => node.props['data-usage-control'] === 'gtm_journey_find_customers').props.disabled, false);
    const saved = renderJourney({ who: audience, problem, needs_input: false, problem_basis: 'From your website.' })();
    const savedFields = nodes(saved).filter(node => ['Who should we target?', 'What problem do you solve?'].includes(node.props['aria-label']));
    assert.equal(savedFields.length, 0);
    assert.equal(nodes(saved).some(node => node.type === 'button' && text(node) === 'Continue'), false);
    assert.match(text(saved), new RegExp(audience)); assert.match(text(saved), new RegExp(problem));
    assert.match(nodes(saved).find(node => node.type === 'card').props.body, /Nothing is sent/);
    assert.doesNotMatch(nodes(saved).find(node => node.type === 'card').props.body, /\b(?:ICP|GTM|enrich|backfill)\b|\u2014/);
  }
});

test('first discovery keeps saved audience and optional context reachable and editable', () => {
  for (const [who, problem, updatedWho, note] of [['Freight dispatch teams', 'Lost delivery schedules', 'Regional dispatch teams', 'Avoid carriers already using this product'],
    ['Weekend hikers', 'Finding accessible trails', 'Families choosing local trails', 'Prioritize routes suitable for children']]) {
    const icp = { who, problem, needs_input: false }, render = renderJourney(icp);
    let tree = render();
    const label = nodes(tree).find(node => node.type === 'label' && text(node) === 'Who should we target?');
    const audience = nodes(tree).find(node => node.type === 'textarea' && node.props.id === label?.props.htmlFor);
    const context = nodes(tree).find(node => node.props['aria-label'] === 'Anything we should know? (existing customers, who to target or avoid)');
    assert.ok(audience); assert.equal(audience.props.value, who); assert.equal(audience.props.disabled, false);
    assert.ok(context); assert.equal(context.props.disabled, false);
    assert.equal(nodes(tree).some(node => node.type === 'details' && !node.props.open && nodes(node).includes(context)), false);
    audience.props.onChange({ target: { value: updatedWho } });
    context.props.onChange({ target: { value: note } });
    tree = render();
    assert.equal(nodes(tree).find(node => node.props['aria-label'] === 'Who should we target?').props.value, updatedWho);
    assert.equal(nodes(tree).find(node => node.props['aria-label'] === 'Anything we should know? (existing customers, who to target or avoid)').props.value, note);
    assert.equal(nodes(tree).find(node => node.props['data-usage-control'] === 'gtm_journey_find_customers').props.disabled, false);
    assert.equal(nodes(tree).some(node => node.props['aria-label'] === 'What problem do you solve?'), false);
    assert.equal(icp.who, who); assert.equal(icp.problem, problem);
  }
});

test('missing Gmail explains optional sending and offers one connection link', () => {
  for (const who of ['Freight dispatch teams', 'Weekend hikers']) {
    const tree = renderJourney({ who, problem: 'Planning work', needs_input: false }, 3)();
    assert.match(text(tree), /Gmail is not connected/);
    assert.match(text(tree), /You can find customers without it/);
    assert.equal(nodes(tree).filter(node => node.type === 'link' && text(node) === 'Connect Gmail').length, 1);
    assert.equal(nodes(tree).find(node => node.type === 'button' && text(node).startsWith('Send ')).props.disabled, true);
  }
});

test('your reviewed batch can send without enabling automatic sending in unrelated domains', () => {
  for (const who of ['Factory procurement', 'Family gardening']) {
    const extra = {allow_sending: false, daily_cap: 0, can_send: false, send_blocked_reason: 'gtm_allow_sending_required',
      gmail: {connected: true}, drafts: [{doc_id: 'draft_saved', review_state: 'approved', channel: 'email', state: 'draft', to: 'reviewed@example.test'}],
      batches: [{approved_count: 1, cap: 5}]};
    const tree = renderJourney({who, problem: 'Planning', needs_input: false}, 3, extra)();
    const send = nodes(tree).find(node => node.type === 'button' && /send 1/i.test(text(node)));
    assert.equal(send.props.disabled, false);
    assert.doesNotMatch(text(tree), /Allow approved sending/);
    assert.equal(extra.allow_sending, false);
    const blocked = renderJourney({who, problem: 'Planning', needs_input: false}, 3, {...extra, send_blocked_reason: 'gtm_prospect_blocked'})();
    assert.equal(nodes(blocked).find(node => node.type === 'button' && /send 1/i.test(text(node))).props.disabled, true);
  }
});

test('local SMTP test requires exactly one reviewed draft addressed to your own mailbox', () => {
  for (const who of ['Factory procurement', 'Family gardening']) {
    const mailbox = 'owner@example.test', calls = [];
    const draft = {doc_id: 'self', batch_id: 'batch', review_state: 'approved', channel: 'email', state: 'draft', to: mailbox};
    const extra = {allow_sending: true, can_send: true, gmail: {connected: true, sender: mailbox}};
    const button = (drafts, local = true) => nodes(renderJourney({who, problem: 'Planning', needs_input: false}, 3,
      {...extra, drafts, batches: [{batch_id: 'batch', approved_count: drafts.length, cap: 5}]}, local, calls)())
      .find(node => node.type === 'button' && text(node) === 'Send a test to yourself');
    assert.equal(button([{...draft, to: 'lead@example.test'}]), undefined);
    assert.equal(button([draft, {...draft, doc_id: 'other'}]), undefined);
    assert.equal(button([draft], false), undefined);
    assert.deepEqual(calls, []);
    const testSend = button([draft]);
    assert.equal(testSend.props.disabled, false);
    testSend.props.onClick();
    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['send', ['self'], true]]);
  }
});

test('F056: local sending and booking setup use your app and do not send while enabling', async () => {
  for (const who of ['Factory procurement', 'Family gardening']) {
    const calls = [], icp = { who, problem: 'Planning', needs_input: false };
    const render = renderJourney(icp, 3, { can_send: false, send_blocked_reason: 'outbound_execution_disabled' }, true, calls);
    const tree = render();
    const enable = nodes(tree).find(node => node.type === 'button' && text(node) === 'Turn on email sending');
    assert.ok(enable);
    await enable.props.onClick();
    assert.equal(calls.length, 1); assert.equal(calls[0][0], 'askSending');
    assert.equal(calls[0][1].ventureId, 'ven_first');
    const setup = nodes(tree).find(node => node.type === 'link' && text(node) === 'Set up Email in Keys and connections');
    assert.equal(setup.props.href, '/settings/local');
    assert.match(text(tree), /You can find customers without it/);
    assert.doesNotMatch(text(tree), /Gmail|Ask Supymem|credit/i);
    assert.equal(nodes(tree).find(node => node.type === 'button' && text(node).startsWith('Send ')).props.disabled, true);
    const booking = renderJourney({ ...icp, mode: 'consumer' }, 2, {}, true)();
    assert.equal(nodes(booking).find(node => node.type === 'link' && /Booking link/.test(text(node))).props.href, '/settings/local');
  }
});
