const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const jsx = (type, props) => ({ type, props: props || {} });
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : !tree || typeof tree !== 'object' ? [] : [tree, ...nodes(tree.props?.children)];
const tick = () => new Promise(resolve => setImmediate(resolve));

test('standing choices preserve once, enforce server decisions and retire across unrelated founder sessions', async () => {
  const code = ts.transpileModule(readFileSync(`${__dirname}/ApprovalPolicyControls.tsx`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const noticeCode = ts.transpileModule(readFileSync(`${__dirname}/../primitives/ErrorNotice.tsx`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  for (const domain of ['warehousing', 'gardening']) {
    const slots = [], effects = [], calls = [], exports = {}, listeners = new Map();
    const reports = [], noticeExports = {};
    vm.runInNewContext(noticeCode, { exports: noticeExports, require: name => name === 'react'
      ? { createElement: (type, props, children) => jsx(type, { ...props, children }), useEffect: callback => effects.push(callback) }
      : name.endsWith('/LocalModeBoundary') ? {useLocalMode: () => false}
      : { reportUserError: report => reports.push(report) } });
    const renderElement = (type, props) => type === noticeExports.ErrorNotice ? type(props) : jsx(type, props);
    let cursor = 0, subject = `founder_${domain}`, epoch = 1, owned = true, succeed = true, deferred;
    let approved = 0, denied = 0;
    const react = {
      useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial;
        return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }]; },
      useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
      useEffect(callback, deps) { const index = cursor++, prior = slots[index];
        if (!prior || deps.some((value, offset) => value !== prior.deps[offset])) {
          prior?.cleanup?.(); slots[index] = { deps }; effects.push(() => { slots[index].cleanup = callback(); });
        } },
    };
    vm.runInNewContext(code, { exports, window: { addEventListener: (name, handler) => listeners.set(name, handler), removeEventListener: name => listeners.delete(name) },
      require: name => name === 'react' ? react : name === 'react/jsx-runtime' ? { jsx: renderElement, jsxs: renderElement }
        : name.endsWith('/ErrorNotice') ? noticeExports : {
        verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
        api: async (url, options) => { calls.push({ url, body: JSON.parse(options.body) });
          if (deferred) return new Promise(resolve => { deferred.resolve = resolve; });
          return { ok: succeed, data: { status: 'standing' } };
        },
      } });
    const props = { policy: { eligible: true, scope_label: 'these saved inputs', venture_id: `venture_${domain}`, workspace_id: `workspace_${domain}` },
      ventureId: `venture_${domain}`, messageId: 'saved_card', action: 'research.start', resultingState: { ceiling_micro: 5, task_allowance_micro: 10 }, disabled: false,
      current: () => owned, onApproved: () => ++approved, onDenied: () => ++denied };
    const render = () => { cursor = 0; const tree = exports.ApprovalPolicyControls(props); while (effects.length) effects.shift()(); return tree; };
    const button = control => nodes(render()).find(node => node.props['data-usage-control'] === control);
    button('approval_always').props.onClick(); button('approval_always').props.onClick(); await tick();
    assert.equal(calls.length, 1); assert.equal(approved, 1);
    assert.deepEqual(calls[0].body.detail.standing_policy_v1, { choice: 'always', venture_id: props.ventureId, message_id: props.messageId, action: 'research.start', resulting_state: props.resultingState });
    assert.equal(calls[0].body.requested_by, subject);
    succeed = false; button('approval_deny').props.onClick(); await tick();
    assert.equal(denied, 0);
    const notice = nodes(render()).find(node => node.props.role === 'alert');
    assert.ok(notice);
    assert.match(notice.props.children, /could not be confirmed/);
    assert.equal(notice.props['data-error-source'], 'chat.approval_policy');
    assert.equal(notice.type, 'span');
    assert.equal(reports.at(-1).source, 'chat.approval_policy');
    assert.equal(reports.at(-1).message, notice.props.children);
    succeed = true; button('approval_deny').props.onClick(); await tick(); assert.equal(denied, 1);
    props.policy.eligible = false; assert.equal(button('approval_always').props.disabled, true);
    assert.equal(button('approval_deny').props.disabled, false);
    const before = calls.length; button('approval_always').props.onClick(); await tick(); assert.equal(calls.length, before);
    props.policy.eligible = true; props.allowDisabled = true;
    assert.equal(button('approval_always').props.disabled, true); assert.equal(button('approval_deny').props.disabled, false);
    button('approval_always').props.onClick(); await tick(); assert.equal(calls.length, before);
    props.allowDisabled = false; deferred = {}; button('approval_always').props.onClick(); await tick();
    subject = `other_${domain}`; epoch++; listeners.get('supymem-session-changed')();
    deferred.resolve({ ok: true }); await tick(); assert.equal(approved, 1);
    deferred = null; render(); button('approval_deny').props.onClick(); await tick(); assert.equal(denied, 2);
    deferred = {}; button('approval_always').props.onClick(); await tick();
    props.resultingState = { ceiling_micro: 7, task_allowance_micro: 12 }; render();
    deferred.resolve({ ok: true }); await tick(); assert.equal(approved, 1);
    deferred = null; button('approval_always').props.onClick(); await tick(); assert.equal(approved, 2);
    assert.deepEqual(calls.at(-1).body.detail.standing_policy_v1.resulting_state, props.resultingState);
    owned = false; button('approval_deny').props.onClick(); await tick(); assert.equal(denied, 2);
  }
  const dock = readFileSync(`${__dirname}/ChatDock.tsx`, 'utf8');
  assert.match(dock, /"approval_policy" in conf \? "Approve once" : a.label/);
  assert.match(dock, /conf.state === "denied"/);
  assert.match(dock, /scope.current === policyScope/);
});
