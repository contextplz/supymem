const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const jsx = (type, props) => typeof type === 'function' ? type(props) : { type, props: props || {} };
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : !tree || typeof tree !== 'object' ? [] : [tree, ...nodes(tree.props?.children)];

test('each inbox consent defaults off, identifies only local IDs, preserves failures and hides with no flag or state', () => {
  const exports = {}, calls = [];
  let state = { enabled: true, busy: false, error: '', briefing: { mailboxes: [] }, save: command => calls.push(command) };
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/InboxBriefingConsent.tsx`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, { exports, require: id => id === 'react/jsx-runtime' ? { jsx, jsxs: jsx }
    : id.includes('useInboxBriefing') ? { useInboxBriefing: () => state }
    : id.includes('ErrorNotice') ? { ErrorNotice: 'error' } : { default: {} } });
  for (const [founder, email] of [['owner_stock', 'buyer@stock.example'], ['owner_walk', 'family@walks.example']]) {
    state.briefing.mailboxes = [{ mailbox_id: `${founder}_a`, email, status: 'active', consent: false }, { mailbox_id: `${founder}_b`, email: `other.${email}`, status: 'active', consent: true }];
    const render = props => exports.InboxBriefingConsent({ ventureId: `ven_${founder}`, workspaceId: `ws_${founder}`, ...props });
    let inputs = nodes(render({})).filter(node => node.type === 'input');
    assert.equal(inputs.length, 2); assert.equal(inputs[0].props.checked, false); assert.equal(inputs[1].props.checked, true);
    inputs[0].props.onChange({ target: { checked: true } }); inputs[1].props.onChange({ target: { checked: false } });
    assert.deepEqual(JSON.parse(JSON.stringify(calls.slice(-2))), [
      { action: 'inbox.briefing.consent', payload: { mailbox_id: `${founder}_a`, consent: true } },
      { action: 'inbox.briefing.consent', payload: { mailbox_id: `${founder}_b`, consent: false } },
    ]);
    assert.equal(nodes(render({ email: email.toUpperCase() })).filter(node => node.type === 'input').length, 1);
    assert.equal(nodes(render({ mailboxId: `${founder}_b` })).filter(node => node.type === 'input')[0].props.checked, true);
    state.briefing.mailboxes[1].status = 'needs_reconnect';
    assert.equal(nodes(render({ mailboxId: `${founder}_b` })).find(node => node.type === 'input').props.disabled, false);
    state.briefing.mailboxes[1].consent = false;
    assert.equal(nodes(render({ mailboxId: `${founder}_b` })).find(node => node.type === 'input').props.disabled, true);
    state.busy = true; assert.equal(nodes(render({})).find(node => node.type === 'input').props.disabled, true); state.busy = false;
    assert.equal(nodes(render({ ventureId: undefined })).find(node => node.type === 'input').props.disabled, true);
    state.error = 'Your inbox preference could not be saved. Try again.';
    assert.equal(nodes(render({})).find(node => node.type === 'error').props.error, state.error);
    state.enabled = false; assert.equal(render({}), null); state.enabled = true;
    const saved = state.briefing; state.briefing = undefined; assert.equal(render({}), null); state.briefing = saved;
  }
  assert.match(readFileSync(`${__dirname}/ConnectionsHub.tsx`, 'utf8'), /isGmail \? <InboxBriefingConsent/);
});
