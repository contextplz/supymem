const assert = require('node:assert/strict'), test = require('node:test'), ts = require('typescript');
const { readFileSync } = require('node:fs'), Module = require('node:module'), path = require('node:path');
const jsx = (type, props) => ({ type, props });
const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
const label = node => !node || typeof node !== 'object' ? '' : [node.props?.children].flat(8).map(value => typeof value === 'string' ? value : label(value)).join('');
const saved = { revision: 'a'.repeat(64), question: null, complete: false, priority_text: '', priorities: [], answers: [], next_href: null,
  idea_href: '/v/v1/idea?edit_previous=1', attachments_href: '/new?venture=v1', acknowledgement: null };

function harness(initial, responder = () => saved) {
  const state = [], effects = [], calls = []; let cursor = 0;
  const react = { useState(value) { const index = cursor++; if (!(index in state)) state[index] = value;
    return [state[index], next => { state[index] = typeof next === 'function' ? next(state[index]) : next; }]; },
    useRef(value) { const index = cursor++; if (!(index in state)) state[index] = { current: value }; return state[index]; },
    useEffect(fn) { effects.push(fn); } };
  let subject = 'founder', epoch = 1;
  const mocks = { react, 'react/jsx-runtime': { jsx, jsxs: jsx }, 'next/link': { default: 'a' },
    '@/components/primitives/ErrorNotice': { ErrorNotice: 'error' }, '@/lib/apiFailure': { apiFailure: () => 'Reload and try again.' },
    '@/lib/client': { ensureSession: async () => {}, verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
      api: async (url, init) => { calls.push({ url, body: init.body ? JSON.parse(init.body) : null });
        return { ok: true, data: calls.length === 1 ? initial : await responder(url, init) }; } } };
  global.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent(event) { calls.push({ event: event.type }); } };
  const filename = path.join(__dirname, 'StartupReview.tsx'), loaded = new Module(filename, module);
  loaded.filename = filename; loaded.paths = module.paths;
  loaded.require = id => { if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    return id.endsWith('.css') ? { default: { page: 'page', focus: 'focus', composer: 'composer', answerActions: 'answerActions', editLinks: 'editLinks' } } : id === '@/lib/runStatusPresentation' ? { RUN_STATUS_REFRESH: 'supymem-run-status-refresh' } : mocks[id] ?? require(id); };
  loaded._compile(ts.transpileModule(readFileSync(filename, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText, filename);
  return { calls, effects, switchOwner() { subject = 'other'; epoch++; }, render(props = {}) {
    cursor = 0; effects.length = 0; return loaded.exports.StartupReview({ ventureId: 'v1', ...props }); } };
}
const flush = () => new Promise(resolve => setImmediate(resolve));
test('completed in-chat answers review continues in chat without hidden research links', async () => {
  let continued = 0;
  const view = harness({ ...saved, complete: true, priorities: ['market_insights'], next_href: '/v/v1/research' });
  view.render(); view.effects[0](); await flush();
  const all = nodes(view.render({ onContinue: () => continued++ }));
  assert.equal(all.some(node => node.props?.href === '/v/v1/research'), false);
  all.find(node => node.type === 'button' && label(node) === 'Continue in chat').props.onClick();
  assert.equal(continued, 1);
  assert.equal(view.calls.length, 1);
});
async function load(initial, responder) { const view = harness(initial, responder); view.render(); view.effects[0](); await flush(); return view; }

test('saved making answers have a clear edit label and still edit the right field', async () => {
  for (const value of ['An inspection tracker for facilities teams', 'A weekend itinerary app for families']) {
    const view = await load({ ...saved, answers: [{ field: 'making', value }] });
    const button = nodes(view.render()).find(node => node.type === 'button' && label(node) === 'Edit what you’re making');
    assert.ok(button);
    button.props.onClick(); await flush();
    assert.deepEqual(view.calls[1].body, { revision: saved.revision, field: 'making' });
  }
});

test('conflict is one question with each sourced value and a typed something else', async () => {
  const view = await load({ ...saved, question: { field: 'business_model', text: 'Which pricing should we use?', values: [
    { value: '$12/mo', source: 'Website' }, { value: '$20/mo', source: 'Founder PDF' }] } });
  let tree = view.render();
  assert.equal(nodes(tree).filter(node => node.type === 'h2').length, 1);
  assert.match(label(tree), /\$12\/mo.*Website.*\$20\/mo.*Founder PDF.*Something else/);
  const other = nodes(tree).find(node => node.type === 'input' && node.props.value === 'other');
  other.props.onChange(); tree = view.render();
  nodes(tree).find(node => node.type === 'textarea').props.onChange({ target: { value: '$25/mo' } });
  tree = view.render(); nodes(tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
  assert.equal(view.calls[1].url, '/api/ventures/v1/context/answer');
  assert.deepEqual(view.calls[1].body, { revision: saved.revision, field: 'business_model', value: '$25/mo' });
  assert.equal(nodes(tree).filter(node => node.type === 'button' && label(node) === 'Skip').length, 1);
});

test('priorities accept unsure and multiple free-text priorities without category buttons', async () => {
  const view = await load(saved); let tree = view.render();
  assert.match(label(tree), /What matters most to you right now\?/);
  nodes(tree).find(node => node.type === 'textarea').props.onChange({ target: { value: "I'm not sure" } });
  tree = view.render(); nodes(tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
  assert.equal(view.calls[1].url, '/api/ventures/v1/context/priorities');
  assert.equal(view.calls[1].body.text, "I'm not sure");
  assert.match(label(tree), /first call/);
});

test('outreach stays the primary action when research and fundraising are also saved', async () => {
  const view = await load({ ...saved, complete: true, priorities: ['outreach_leads', 'market_insights', 'fundraising'], next_href: '/gtm?venture=v1', acknowledgement: 'Saved as plan items.' });
  const tree = view.render(), primary = nodes(tree).find(node => node.type === 'a' && node.props.className === 'primary');
  assert.equal(primary.props.href, '/gtm?venture=v1');
  assert.equal(label(primary), 'Book a first call');
  assert(nodes(tree).some(node => node.type === 'div' && node.props.className === 'answerActions' && node.props.children.includes(primary)));
  assert.equal(nodes(tree).find(node => label(node) === 'Research your market').props.href, '/v/v1/research');
  assert.equal(nodes(tree).find(node => label(node) === 'Review saved plan items').props.href, '/v/v1/tasks');
  assert.doesNotMatch(label(tree), /Early|Mid|Advanced|classification/);
});

test('back actions work for idea, attachments, a resolved conflict and saved priorities', async () => {
  const initial = { ...saved, complete: true, priorities: ['outreach_leads'], next_href: '/gtm?venture=v1',
    answers: [{ field: 'business_model', value: '$12/mo' }] };
  const view = await load(initial); let tree = view.render();
  assert(nodes(tree).some(node => node.type === 'a' && node.props.href === initial.idea_href));
  assert(nodes(tree).some(node => node.type === 'a' && node.props.href === initial.attachments_href));
  nodes(tree).find(node => node.type === 'button' && label(node) === 'Edit business model').props.onClick(); await flush();
  assert.equal(view.calls[1].body.field, 'business_model');
  const priorities = await load(initial); tree = priorities.render();
  nodes(tree).find(node => node.type === 'button' && label(node) === 'Edit priorities').props.onClick(); await flush();
  assert.equal(priorities.calls[1].body.field, 'priorities');
  assert.match(label(priorities.render()), /What matters most/);
});

test('an uncertain save keeps a reload action and a duplicate click does not replay', async () => {
  let finish; const view = await load(saved, () => new Promise(resolve => { finish = resolve; }));
  let tree = view.render(); nodes(tree).find(node => node.type === 'textarea').props.onChange({ target: { value: 'Customers' } });
  tree = view.render(); const submit = nodes(tree).find(node => node.type === 'form');
  submit.props.onSubmit({ preventDefault() {} }); submit.props.onSubmit({ preventDefault() {} }); await flush();
  assert.equal(view.calls.length, 2); view.switchOwner(); finish({ ...saved, complete: true }); await flush();
  assert(!label(view.render()).includes('Your next step is ready'));
});

test('review uses idea cards, a full-width answer and refreshes both banners after saving', async () => {
  const view = await load(saved);
  let tree = view.render();
  assert.equal(tree.props.className, 'page');
  assert.equal(nodes(tree).find(node => node.type === 'section').props.className, 'focus');
  assert.equal(nodes(tree).find(node => node.type === 'form').props.className, 'composer');
  const textarea = nodes(tree).find(node => node.type === 'textarea');
  assert.equal(nodes(tree).find(node => node.type === 'label' && node.props.children.includes(textarea)).props.style.display, 'grid');
  textarea.props.onChange({ target: { value: 'Customers' } });
  tree = view.render();
  nodes(tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
  assert(view.calls.some(call => call.event === 'supymem-run-status-refresh'));
});

test('problem prefill is editable and saved problem/progress share the edit-link style', async () => {
  const view = await load({ ...saved, question: { field: 'problem', text: 'What problem does it solve?',
    prefill: 'Help first-time US parents track baby sleep', values: [] },
    answers: [{ field: 'problem', value: 'Lost sleep' }, { field: 'progress', value: 'Prototype' }] });
  const tree = view.render();
  assert.equal(nodes(tree).find(node => node.type === 'textarea').props.value, 'Help first-time US parents track baby sleep');
  const nav = nodes(tree).find(node => node.props?.['aria-label'] === 'Go back');
  assert.equal(nav.props.className, 'answerActions editLinks');
  for (const text of ['Edit idea', 'Edit attachments', 'Edit problem', 'Edit progress']) {
    assert(nodes(nav).some(node => label(node) === text));
  }
  const css = readFileSync(path.join(__dirname, '../../app/v/[ventureId]/idea/idea.module.css'), 'utf8');
  assert.match(css, /\.editLinks a, \.editLinks button \{[^}]*color: var\(--accent\);[^}]*font: inherit/);
});

test('every review question and priorities can skip without inventing an answer', async () => {
  for (const field of ['problem', 'progress', 'business_model', null]) {
    const view = await load({ ...saved, question: field ? { field, text: 'Question', values: [] } : null });
    nodes(view.render()).find(node => node.type === 'button' && label(node) === 'Skip').props.onClick();
    await flush();
    assert.equal(view.calls[1].url, `/api/ventures/v1/context/${field ? 'skip' : 'priorities'}`);
    assert.deepEqual(view.calls[1].body, { revision: saved.revision, ...(field ? { field } : { text: '' }) });
  }
});
