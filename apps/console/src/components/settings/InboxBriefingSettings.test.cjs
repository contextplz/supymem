const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const jsx = (type, props) => typeof type === 'function' ? type(props) : { type, props: props || {} };
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : !tree || typeof tree !== 'object' ? [] : [tree, ...nodes(tree.props?.children)];
const tick = () => new Promise(resolve => setImmediate(resolve));

test('schedule edits preserve saved timezone windows, quiet hours and email opt-in across unrelated founders', async () => {
  for (const [ventureId, windows] of [['ven_stock', ['08:30', '17:30']], ['ven_walk', ['09:15']]]) {
    const slots = [], effects = [], calls = [], exports = {}; let cursor = 0;
    const settings = { windows, quiet_hours: { start: '21:00', end: '08:00' }, email_copy: false };
    const state = { enabled: true, busy: false, error: '', briefing: { settings }, save: async command => { calls.push(command); return !state.error; } };
    const react = { useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => { slots[index] = value; }]; },
      useEffect(callback, deps) { const index = cursor++, prior = slots[index]; if (!prior || deps.some((value, offset) => value !== prior[offset])) { slots[index] = deps; effects.push(callback); } } };
    const valid = value => value && Array.isArray(value.windows) && value.windows.length && value.windows.every(time => /^([01]\d|2[0-3]):[0-5]\d$/.test(time))
      && value.windows.length <= 2 && new Set(value.windows).size === value.windows.length && value.email_copy === false;
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/InboxBriefingSettings.tsx`, 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText, { exports, require: id => id === 'react' ? react : id === 'react/jsx-runtime' ? { jsx, jsxs: jsx }
      : id.includes('useInboxBriefing') ? { useInboxBriefing: () => state, validBriefingSettings: valid }
      : id.includes('ErrorNotice') ? { ErrorNotice: 'error' } : { default: {} } });
    const render = () => { cursor = 0; const tree = exports.InboxBriefingSettings({ ventureId }); while (effects.length) effects.shift()(); return tree; };
    render(); let tree = render();
    assert.equal(nodes(tree).filter(node => node.props?.type === 'time').length, windows.length + 2);
    nodes(tree).find(node => node.props['aria-label'] === 'Briefing time 1').props.onChange({ target: { value: '10:45' } }); tree = render();
    assert.equal(nodes(tree).find(node => node.props['data-usage-control'] === 'inbox_briefing_email_copy').props.disabled, true);
    nodes(tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); await tick();
    assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), { action: 'inbox.briefing.settings', payload: { ...settings, windows: ['10:45', ...windows.slice(1)] } });
    const add = nodes(render()).find(node => node.props['data-usage-control'] === 'inbox_briefing_window_add');
    if (windows.length === 1) { add.props.onClick(); tree = render(); assert.equal(nodes(tree).find(node => node.props.type === 'submit').props.disabled, true); }
    else assert.equal(add, undefined);
    state.busy = true; assert.equal(nodes(render()).find(node => node.type === 'fieldset').props.disabled, true); state.busy = false;
    state.error = 'Your inbox preference could not be saved. Try again.';
    assert.equal(nodes(render()).find(node => node.type === 'error').props.error, state.error);
    state.enabled = false; assert.equal(render(), null); state.enabled = true;
    state.briefing = undefined; assert.equal(render(), null);
  }
  assert.match(readFileSync(`${__dirname}/AccountSettings.tsx`, 'utf8'), /<InboxBriefingSettings/);
});
