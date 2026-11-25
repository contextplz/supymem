const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const jsx = (type, props) => ({ type, props: props || {} });
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : !tree || typeof tree !== 'object' ? [] : [tree, ...nodes(tree.props?.children)];
const tick = () => new Promise(resolve => setImmediate(resolve));

test('settings list and revoke remain founder-private, retryable and invisible when disabled', async () => {
  const code = ts.transpileModule(readFileSync(`${__dirname}/StandingApprovalsSettings.tsx`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const noticeCode = ts.transpileModule(readFileSync(`${__dirname}/../primitives/ErrorNotice.tsx`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  for (const domain of ['procurement', 'music']) {
    const slots = [], effects = [], calls = [], exports = {}, listeners = new Map();
    const reports = [], noticeExports = {};
    vm.runInNewContext(noticeCode, { exports: noticeExports, require: name => name === 'react'
      ? { createElement: (type, props, children) => jsx(type, { ...props, children }), useEffect: callback => effects.push(callback) }
      : name.endsWith('/LocalModeBoundary') ? {useLocalMode: () => false}
      : { reportUserError: report => reports.push(report) } });
    const renderElement = (type, props) => type === noticeExports.ErrorNotice ? type(props) : jsx(type, props);
    let cursor = 0, subject = `founder_${domain}`, epoch = 1, succeed = true, loaded = true, enabled = true, deferred;
    const props = { workspaceId: `workspace_${domain}`, ventureId: `venture_${domain}` };
    const grant = { id: `grant_${domain}`, summary: `Read ${domain} documents`, expires_at: '2026-11-29T00:00:00Z', detail: { venture_id: props.ventureId } };
    vm.runInNewContext(code, { exports, window: { addEventListener: (name, handler) => listeners.set(name, handler), removeEventListener: name => listeners.delete(name) },
      require: name => name === 'react' ? {
        useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial;
          return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }]; },
        useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
        useEffect(callback, deps) { const index = cursor++, prior = slots[index];
          if (!prior || deps.some((value, offset) => value !== prior.deps[offset])) {
            prior?.cleanup?.(); slots[index] = { deps }; effects.push(() => { slots[index].cleanup = callback(); });
          } },
      } : name === 'react/jsx-runtime' ? { jsx: renderElement, jsxs: renderElement }
        : name.endsWith('/ErrorNotice') ? noticeExports : {
        verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
        api: async (url, options) => { calls.push({ url, body: options ? JSON.parse(options.body) : null });
          if (options) { if (deferred) return new Promise(resolve => { deferred.resolve = resolve; }); return { ok: succeed }; }
          return { ok: loaded, data: { items: enabled ? [grant, { ...grant, id: 'foreign', detail: { venture_id: 'another-venture' } }] : [] } };
        },
      } });
    const render = () => { cursor = 0; const tree = exports.StandingApprovalsSettings(props); while (effects.length) effects.shift()(); return tree; };
    render(); await tick(); assert.equal(nodes(render()).filter(node => node.type === 'li').length, 1);
    const button = () => nodes(render()).find(node => node.props['data-usage-control'] === 'approval_revoke');
    succeed = false; button().props.onClick(); await tick();
    assert.equal(nodes(render()).filter(node => node.type === 'li').length, 1);
    const notice = nodes(render()).find(node => node.props.role === 'alert');
    assert.ok(notice);
    assert.match(notice.props.children, /revocation could not be confirmed/);
    assert.equal(notice.props['data-error-source'], 'settings.standing_approvals');
    assert.equal(reports.at(-1).source, 'settings.standing_approvals');
    assert.equal(reports.at(-1).message, notice.props.children);
    assert.deepEqual(calls.at(-1).body, { decision: 'withdrawn', decided_by: subject });
    succeed = true; button().props.onClick(); await tick(); assert.equal(render(), null);
    listeners.get('supymem-session-changed')(); await tick(); render();
    deferred = {}; button().props.onClick(); await tick();
    subject = `other_${domain}`; epoch++; enabled = false;
    assert.equal(render(), null);
    deferred.resolve({ ok: true }); await tick(); assert.equal(render(), null);
    loaded = false; listeners.get('supymem-session-changed')(); await tick();
    const loadNotice = nodes(render()).find(node => node.props.role === 'alert');
    assert.ok(loadNotice);
    assert.match(loadNotice.props.children, /standing approvals could not be checked/);
    assert.equal(loadNotice.props['data-error-source'], 'settings.standing_approvals');
    assert.equal(reports.at(-1).source, 'settings.standing_approvals');
    assert.equal(reports.at(-1).message, loadNotice.props.children);
    assert.equal(nodes(render()).filter(node => node.type === 'li').length, 0);
  }
  assert.match(readFileSync(`${__dirname}/VentureSettings.tsx`, 'utf8'), /<StandingApprovalsSettings workspaceId=\{workspaceId\} ventureId=\{ventureId\}/);
});

test('17: you can refresh an uncertain revocation without sending it again', async () => {
  for (const domain of ['Wholesale logistics', 'Personal nutrition']) {
    const slots = [], effects = [], calls = [], exports = {}; let cursor = 0;
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/StandingApprovalsSettings.tsx`, 'utf8'), {compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    }}).outputText, {exports, window: {addEventListener() {}, removeEventListener() {}}, require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {useState(initial) {const index = cursor++; if (!(index in slots)) slots[index] = initial;
        return [slots[index], value => {slots[index] = typeof value === 'function' ? value(slots[index]) : value;}];},
        useRef(initial) {const index = cursor++; return slots[index] ??= {current: initial};},
        useEffect(callback, deps) {const index = cursor++; if (!slots[index] || deps.some((value, offset) => slots[index].deps[offset] !== value)) {
          slots[index]?.cleanup?.(); slots[index] = {deps}; effects.push(() => {slots[index].cleanup = callback();});}}};
      if (id.endsWith('/ErrorNotice')) return {ErrorNotice: 'error'};
      return {verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1, api: async (path, options) => {
        calls.push({path, options}); return {ok: !options, data: {items: [{id: 'grant', summary: domain,
          expires_at: '2099-01-01', detail: {venture_id: 'ven_domain'}}]}};}};
    }});
    const render = () => {cursor = 0; const tree = exports.StandingApprovalsSettings({workspaceId: 'workspace', ventureId: 'ven_domain'});
      effects.splice(0).forEach(effect => effect()); return tree;};
    render(); await tick(); nodes(render()).find(node => node.props['data-usage-control'] === 'approval_revoke').props.onClick(); await tick();
    nodes(render()).find(node => node.props.children === 'Refresh saved status').props.onClick(); render(); await tick();
    assert.equal(calls.filter(call => call.options).length, 1); assert.equal(calls.filter(call => !call.options).length, 2);
    assert.equal(nodes(render()).filter(node => node.type === 'li').length, 1);
  }
});
