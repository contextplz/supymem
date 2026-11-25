const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const policy = require('./chatActionPolicy.ts');
const jsx = (type, props) => typeof type === 'function' ? type(props) : { type, props: props || {} };
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : !tree || typeof tree !== 'object' ? [] : [tree, ...nodes(tree.props?.children)];
const text = tree => Array.isArray(tree) ? tree.map(text).join('') : tree && typeof tree === 'object' ? text(tree.props?.children) : typeof tree === 'boolean' ? '' : String(tree ?? '');
function harness() {
  const slots = [], exports = {}; let cursor = 0, subject = 'owner_stock', epoch = 1;
  const react = { useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial;
    return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }]; },
    useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; } };
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/CosBrief.tsx`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, { exports, require: id => id === 'react' ? react : id === 'react/jsx-runtime' ? { jsx, jsxs: jsx }
    : id.includes('client') ? { verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch }
    : id.includes('chatActionPolicy') ? policy : id.includes('ErrorNotice') ? { ErrorNotice: 'error' }
    : id.includes('ChatLink') ? { default: 'link' } : { default: new Proxy({}, { get: (_, key) => key }) } });
  return { render(props) { cursor = 0; return exports.CosBrief(props); }, valid: exports.validCosBrief };
}
const payload = (ventureId, label) => ({ week_of: '2026-10-26', sections: [
  { key: 'replies', title: 'Replies and calls booked', items: [{ id: 'r1', label, href: `/v/${ventureId}/leads` },
    { id: 'r2', label: 'Foreign view', href: '/v/ven_other/leads' }, { id: 'r3', label: 'Escape', href: `/v/${ventureId}//evil.example` }] },
  { key: 'bounces', title: 'Bounces and opt-outs', items: [] },
  { key: 'experiments', title: 'Experiments', items: [{ id: 'e1', label: 'Still running: weekly reorders', href: `/v/${ventureId}/experiments` }] }],
  next_move: { label: 'Find customers', action: 'gtm.find', payload: { prepare: true } },
  alternatives: [{ label: 'Draft emails', action: 'outreach.draft' }, { label: 'Build your landing page', action: 'pagebuilder.build' }, { label: 'Extra', action: 'x' }] });

test('weekly brief renders sections, own-venture links and three moves for unrelated founders', async () => {
  for (const [ventureId, label] of [['ven_stock', 'Call booked with Priya'], ['ven_garden', 'Reply from a seed swapper']]) {
    const runtime = harness(), calls = [], fallback = { type: 'plain' };
    const message = { id: ventureId, role: 'assistant', kind: 'cos_brief', citations: [{ cos_brief: payload(ventureId, label) }] };
    const props = { ventureId, message, children: fallback, onAction: async (...args) => { calls.push(args); return true; } };
    assert.equal(runtime.render({ ...props, message: { ...message, kind: 'inbox_briefing' } }), fallback);
    assert.equal(runtime.render({ ...props, message: { ...message, role: 'founder' } }), fallback);
    assert.equal(runtime.render({ ...props, message: { ...message, citations: [{ cos_brief: { sections: 'x' } }] } }), fallback);
    const tree = runtime.render(props);
    assert.match(text(tree), /Replies and calls booked \(3\)/); assert.match(text(tree), /Bounces and opt-outs \(0\)Nothing new\./);
    const links = nodes(tree).filter(node => node.type === 'link');
    assert.deepEqual(links.map(node => node.props.href), [`/v/${ventureId}/leads`, `/v/${ventureId}/experiments`]);
    assert.ok(text(tree).includes(label) && text(tree).includes('Foreign view'));
    const buttons = nodes(tree).filter(node => node.type === 'button');
    assert.deepEqual(buttons.map(text), ['Find customers', 'Draft emails', 'Build your landing page']);
    await buttons[0].props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['gtm.find', { prepare: true }]]);
    assert.equal(nodes(runtime.render({ ...props, disabled: true })).filter(node => node.type === 'button').every(node => node.props.disabled), true);
  }
});

test('empty week says nothing new and failures surface an error', async () => {
  const runtime = harness();
  const empty = { week_of: '2026-10-26', sections: [{ key: 'replies', title: 'Replies', items: [] }], next_move: null, alternatives: [] };
  const props = { ventureId: 'ven_garden', children: null, onAction: async () => false,
    message: { id: 'm', role: 'assistant', kind: 'cos_brief', citations: [{ cos_brief: empty }] } };
  assert.match(text(runtime.render(props)), /^Your weekly briefNothing new this week\.Replies \(0\)Nothing new\.Your next moveNothing new\./);
  const full = { ...props, message: { ...props.message, citations: [{ cos_brief: payload('ven_garden', 'Reply') }] } };
  await nodes(runtime.render(full)).find(node => node.type === 'button').props.onClick(); await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes(runtime.render(full)).find(node => node.type === 'error').props.error, /could not be completed/);
  assert.equal(runtime.valid({ ...empty, next_move: { label: 1 } }), false);
  assert.match(readFileSync(`${__dirname}/InboxBriefing.tsx`, 'utf8'), /message\.kind === "cos_brief"\) return <CosBrief/);
});
