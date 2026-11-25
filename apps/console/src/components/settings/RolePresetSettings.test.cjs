const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

const jsx = (type, props) => ({ type, props: props || {} });
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : tree?.props ? [tree, ...nodes(tree.props.children)] : [];
const text = tree => Array.isArray(tree) ? tree.map(text).join('') : tree?.props ? text(tree.props.children ?? tree.props.error) : tree == null ? '' : String(tree);
const tick = () => new Promise(resolve => setImmediate(resolve));
const code = ts.transpileModule(readFileSync(`${__dirname}/RolePresetSettings.tsx`, 'utf8'), { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;

function harness(ventureId) {
  const slots = [], effects = [], calls = [], listeners = new Map(), exports = {};
  let cursor = 0, subject = 'usr_founder', epoch = 1, venture = ventureId;
  vm.runInNewContext(code, { exports, window: {
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: (name, callback) => { if (listeners.get(name) === callback) listeners.delete(name); },
  }, require(name) {
    if (name === 'react') return {
      useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial;
        return [slots[index], next => { slots[index] = typeof next === 'function' ? next(slots[index]) : next; }]; },
      useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
      useEffect(callback, deps) { const index = cursor++, before = slots[index];
        if (!before || deps.some((value, offset) => value !== before.deps[offset])) effects.push(() => {
          before?.cleanup?.(); slots[index] = { deps, cleanup: callback() };
        }); },
    };
    if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (name.endsWith('/ErrorNotice')) return { ErrorNotice: 'ErrorNotice' };
    if (name.endsWith('/client')) return {
      verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
      api: (path, init) => new Promise((resolve, reject) => calls.push({ path, init, resolve, reject })),
    };
    return { default: {} };
  } });
  return {
    calls, listeners,
    render() { cursor = 0; const tree = exports.RolePresetSettings({ ventureId: venture }); while (effects.length) effects.shift()(); return tree; },
    switchVenture(next) { venture = next; },
    switchFounder(next) { subject = next; epoch += 1; },
    unmount() { for (const slot of slots) slot?.cleanup?.(); },
  };
}

test('failed role loading uses the shared error reporter without allowing a settings write', async () => {
  for (const ventureId of ['ven_fleet_maintenance', 'ven_home_gardening']) {
    const screen = harness(ventureId);
    screen.render();
    screen.calls[0].resolve({ok: false}); await tick();
    const tree = screen.render();
    const notice = nodes(tree).find(node => node.type === 'ErrorNotice');
    assert.equal(notice.props.source, 'RolePresetSettings.error');
    assert.match(notice.props.error, /could not be loaded/);
    assert.equal(nodes(tree).find(node => node.type === 'fieldset')?.props.disabled ?? true, true);
    assert.equal(screen.calls.length, 1);
  }
});

test('you can save venture focus across business and consumer products without changing authority or leaking stale state', async () => {
  for (const [ventureId, audience] of [['ven_dispatch', 'warehouse teams'], ['ven_hiking', 'families planning hikes']]) {
    const screen = harness(ventureId);
    const value = preset => ({ role_preset: preset, focus: `Focus on the next decision for ${audience}.`, suggestions: `Suggest a question for ${audience}.` });
    const complete = async (request, preset) => { request.resolve({ ok: true, data: value(preset) }); await tick(); };
    const submit = tree => nodes(tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
    assert.match(text(screen.render()), /Loading your focus/);
    assert.equal(screen.calls[0].path, `/api/ventures/${ventureId}/task-preferences/role-preset`);
    await complete(screen.calls[0], 'chief_of_staff');
    let tree = screen.render();
    assert.deepEqual(nodes(tree).filter(node => node.type === 'option').map(node => text(node)), ['Sales', 'Chief of Staff', 'Product']);
    assert.equal(nodes(tree).find(node => node.type === 'select').props.value, 'chief_of_staff');
    assert.match(text(tree), /permissions, spending limits and sending approvals stay the same/);
    assert.match(text(tree), new RegExp(audience));
    for (const preset of ['sales', 'product', 'chief_of_staff']) {
      nodes(tree).find(node => node.type === 'select').props.onChange({ target: { value: preset } });
      tree = screen.render();
      const saving = submit(tree), call = screen.calls.at(-1), count = screen.calls.length;
      assert.equal(call.init.method, 'PATCH');
      assert.deepEqual(JSON.parse(call.init.body), { role_preset: preset });
      assert.equal(nodes(screen.render()).find(node => node.type === 'fieldset').props.disabled, true);
      await submit(tree);
      assert.equal(screen.calls.length, count);
      await complete(call, preset); await saving;
      tree = screen.render();
      assert.match(text(tree), /You saved your focus. Open chat to choose your next step/);
    }
    nodes(tree).find(node => node.type === 'select').props.onChange({ target: { value: 'sales' } });
    let saving = submit(screen.render());
    await complete(screen.calls.at(-1), 'product'); await saving;
    tree = screen.render();
    assert.match(text(tree), /could not be confirmed/);
    assert.equal(nodes(tree).find(node => node.type === 'fieldset').props.disabled, true);
    nodes(tree).find(node => node.type === 'button' && text(node) === 'Refresh saved settings').props.onClick();
    screen.render();
    await complete(screen.calls.at(-1), 'chief_of_staff');
    tree = screen.render();
    nodes(tree).find(node => node.type === 'select').props.onChange({ target: { value: 'sales' } });
    saving = submit(screen.render());
    const staleSave = screen.calls.at(-1);
    screen.switchVenture(`${ventureId}_other`);
    assert.doesNotMatch(text(screen.render()), new RegExp(audience));
    await complete(staleSave, 'sales'); await saving;
    assert.equal(nodes(screen.render()).some(node => node.type === 'select'), false);
    await complete(screen.calls.at(-1), 'product');
    assert.equal(nodes(screen.render()).find(node => node.type === 'select').props.value, 'product');
    screen.switchVenture(`${ventureId}_pending`); screen.render();
    const staleRead = screen.calls.at(-1);
    screen.switchFounder('usr_other');
    const before = screen.calls.length;
    screen.listeners.get('supymem-session-changed')();
    assert.equal(screen.calls.length, before);
    screen.render();
    await complete(staleRead, 'sales');
    assert.equal(nodes(screen.render()).some(node => node.type === 'select'), false);
    screen.calls.at(-1).reject(new Error('Network unavailable')); await tick();
    tree = screen.render();
    assert.match(text(tree), /Refresh saved settings to try again/);
    assert.equal(nodes(tree).some(node => node.type === 'select'), false);
    screen.switchFounder('');
    assert.match(text(screen.render()), /Sign in, then choose your focus/);
    screen.switchFounder('usr_founder'); screen.switchVenture(undefined);
    assert.match(text(screen.render()), /Choose a venture, then save your focus/);
    screen.unmount();
    assert.equal(screen.listeners.size, 0);
  }
  assert.match(readFileSync(`${__dirname}/AccountSettings.tsx`, 'utf8'), /<RolePresetSettings\b[^>]*ventureId=/);
  assert.doesNotMatch(readFileSync(`${__dirname}/RolePresetSettings.tsx`, 'utf8'), /\u2014/);
});
