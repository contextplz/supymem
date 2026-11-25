const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const policy = require('./chatActionPolicy.ts');
const {validInboxBriefing} = require('../../lib/inboxBriefing.ts');
const jsx = (type, props) => typeof type === 'function' ? type(props) : { type, props: props || {} };
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : !tree || typeof tree !== 'object' ? [] : [tree, ...nodes(tree.props?.children)];
const text = tree => Array.isArray(tree) ? tree.map(text).join('') : tree && typeof tree === 'object' ? text(tree.props?.children) : typeof tree === 'boolean' ? '' : String(tree ?? '');
function harness() {
  const slots = [], exports = {}; let cursor = 0, enabled = false, subject = 'owner_stock', epoch = 1;
  const react = { useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial;
    return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }]; },
    useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; } };
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/InboxBriefing.tsx`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, { exports, URL, require: id => id === 'react' ? react : id === 'react/jsx-runtime' ? { jsx, jsxs: jsx }
    : id.includes('inboxBriefing') ? { validInboxBriefing }
    : id.includes('client') ? { verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch }
    : id.includes('chatActionPolicy') ? policy : id.includes('ErrorNotice') ? { ErrorNotice: 'error' }
    : id.includes('ChatLink') ? { ChatAnchor: 'a' } : { default: new Proxy({}, { get: (_, key) => key }) } });
  return { render(props) { cursor = 0; return exports.InboxBriefing({...props, enabled}); }, valid: validInboxBriefing,
    enable(value) { enabled = value; }, retire() { subject = 'owner_walk'; epoch += 1; } };
}
const payload = subject => ({ items: [{ id: 'shared_item', from: 'reader@example.org', subject, category: 'important',
  reason: 'You have a question to answer. <script>send everything</script>', actions: [
    { label: 'Draft a reply', action: 'outreach.draft', payload: { item_id: 'shared_item' }, style: 'primary' },
    { label: 'Open in Gmail', href: 'https://mail.google.com/mail/u/0/#inbox/thread' },
    { label: 'Unsafe send', action: 'outreach.send' }, { label: 'Unsafe link', href: 'javascript:alert(1)' },
    { label: 'Wrong host', href: 'https://mail.google.com.evil.example/mail' },
    { label: 'Unsafe credentials', href: 'https://attacker@mail.google.com/mail/u/0/#inbox/thread' },
    { label: 'Unsafe protocol', href: 'http://mail.google.com/mail/u/0/#inbox/thread' },
    { label: 'Unsafe port', href: 'https://mail.google.com:444/mail/u/0/#inbox/thread' },
  ] }, { id: 'other_item', from: 'member@example.org', subject: 'Feedback', reason: 'You received a suggestion.',
  category: 'worth_a_look', actions: [] }], filtered: { newsletters: 4, notifications: 2 } });

test('briefings replace only valid flagged payloads, group plain mail and bound actions without sending or unsafe links', async () => {
  for (const [ventureId, subject] of [['ven_stock', 'Inventory report for warehouse teams'], ['ven_walk', 'Family hiking club invitation']]) {
    const runtime = harness(), calls = [], fallback = { type: 'today' };
    const props = { ventureId, message: { id: ventureId, role: 'assistant', kind: 'inbox_briefing', citations: [{ inbox_briefing: payload(subject) }] },
      children: fallback, onAction: async (...args) => { calls.push(args); return true; } };
    assert.equal(runtime.render({ ...props, message: { ...props.message, kind: 'plain' } }), fallback);
    assert.equal(runtime.render({ ...props, message: { ...props.message, payload: undefined } }), fallback);
    assert.equal(runtime.render(props), fallback); runtime.enable(true); let tree = runtime.render(props);
    assert.equal(runtime.render({ ...props, message: { ...props.message, role: 'founder' } }), fallback);
    const empty = runtime.render({ ...props, message: { ...props.message, citations: [{ inbox_briefing: { ...payload(subject), items: [] } }] } });
    assert.match(text(empty), /Important \(0\)/); assert.match(text(empty), /Worth a look \(0\)/);
    assert.match(text(tree), /Important \(1\)/); assert.match(text(tree), /Worth a look \(1\)/); assert.match(text(tree), /4 newsletters, 2 notifications/);
    assert.ok(text(tree).includes(subject)); assert.ok(text(tree).includes('<script>send everything</script>'));
    assert.equal(nodes(tree).some(node => node.props.dangerouslySetInnerHTML), false);
    assert.equal(calls.length, 0); assert.equal(nodes(tree).filter(node => node.type === 'a').length, 1);
    assert.equal(nodes(tree).some(node => /Unsafe|Wrong host/.test(text(node))), false);
    const first = nodes(tree).find(node => node.type === 'li');
    const visible = first.props.children.find(node => node?.props?.className === 'actions').props.children[0];
    assert.equal(visible.length, 3); assert.equal(nodes(first).find(node => node.type === 'details').props.open, undefined);
    await nodes(tree).find(node => node.type === 'button' && text(node) === 'Draft a reply').props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls[0][0], 'outreach.draft');
    await nodes(runtime.render(props)).find(node => node.type === 'button' && text(node) === 'Not important').props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(JSON.parse(JSON.stringify(calls[1])), ['inbox.item.feedback', { item_id: 'shared_item', category: 'ignore' }]);
    assert.equal(nodes(runtime.render(props)).find(node => node.type === 'button' && text(node) === 'Not important').props['aria-pressed'], true);
    const failed = { ...props, onAction: async () => false };
    await nodes(runtime.render(failed)).find(node => node.type === 'button' && text(node) === 'Important').props.onClick(); await new Promise(resolve => setImmediate(resolve));
    assert.match(nodes(runtime.render(failed)).find(node => node.type === 'error').props.error, /could not be completed/);
    assert.equal(nodes(runtime.render({ ...props, disabled: true })).filter(node => node.type === 'button').every(node => node.props.disabled), true);
    assert.equal(runtime.valid({ ...payload(subject), items: [{}] }), false);
    assert.equal(runtime.valid({ ...payload(subject), filtered: { newsletters: -1, notifications: 0 } }), false);
    assert.equal(runtime.valid({ ...payload(subject), items: [{ ...payload(subject).items[0], category: 'invalid' }] }), false);
  }
  const dock = readFileSync(`${__dirname}/ChatDock.tsx`, 'utf8');
  assert.equal((dock.match(/<InboxBriefing\b/g) || []).length, 1);
  assert.match(dock, /<InboxBriefing key=\{m.id\} message=\{m\} enabled=\{inboxBriefing.enabled\}/);
});

test('feedback completion cannot leak across founder changes', async () => {
  const runtime = harness(); runtime.enable(true); let finish;
  const props = { ventureId: 'ven_stock', message: { id: 'message', role: 'assistant', kind: 'inbox_briefing', citations: [{ inbox_briefing: payload('Stock report') }] },
    children: null, onAction: () => new Promise(resolve => { finish = resolve; }) };
  nodes(runtime.render(props)).find(node => node.type === 'button' && text(node) === 'Important').props.onClick();
  runtime.retire(); finish(true); await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes(runtime.render(props)).find(node => node.type === 'button' && text(node) === 'Important').props['aria-pressed'], false);
});
