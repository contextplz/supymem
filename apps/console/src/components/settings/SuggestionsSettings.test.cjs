const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const {readFileSync} = require('node:fs'), ts = require('typescript');
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : tree?.props ? [tree, ...nodes(tree.props.children)] : [];
const tick = () => new Promise(resolve => setImmediate(resolve));
const compile = file => ts.transpileModule(readFileSync(file, 'utf8'), {compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
}}).outputText;

function fixture(domain) {
  const states = [], refs = [], effects = [], calls = [], exports = {}, prefs = {};
  let cursor = 0, refCursor = 0, stored, read = async () => stored, write = async next => {stored = next; return next;};
  let pause = async () => {throw new Error('unavailable');};
  const desktop = {isDesktopShell: () => true,
    desktopInvoke: async (command, args) => {calls.push(command); return write(args.prefs);},
    desktopInvokeOrThrow: async command => {calls.push(command); return command === 'suggestions_get_prefs' ? read() : pause();}};
  vm.runInNewContext(compile(`${__dirname}/../../lib/suggestions.ts`), {exports: prefs, require: () => desktop});
  stored = {...prefs.DEFAULT_PREFS, enabled: true, min_gap_minutes: domain === 'B2B inventory' ? 30 : 60};
  const jsx = (type, props) => ({type, props});
  vm.runInNewContext(compile(`${__dirname}/SuggestionsSettings.tsx`), {exports, require(id) {
    if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    if (id === 'react') return {useState(initial) {const index = cursor++; if (!(index in states)) states[index] = initial;
      return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value;}];},
      useRef: initial => refs[refCursor++] ??= {current: initial}, useEffect(callback, deps) {
        const index = cursor++; if (!states[index] || deps.some((value, offset) => states[index].deps[offset] !== value)) {
          states[index]?.cleanup?.(); states[index] = {deps}; effects.push(() => {states[index].cleanup = callback();});}}};
    if (id.endsWith('/desktop')) return desktop;
    if (id.endsWith('/suggestions')) return prefs;
    return {default: {}};
  }});
  const render = () => {cursor = refCursor = 0; const tree = exports.SuggestionsSettings(); effects.splice(0).forEach(effect => effect()); return tree;};
  return {render, calls, setRead: value => {read = value;}, setWrite: value => {write = value;}, setPause: value => {pause = value;},
    checkbox: () => nodes(render()).find(node => node.type === 'input')};
}

test('14: failed suggestion saves retain your confirmed switches and block overlapping saves', async () => {
  for (const domain of ['B2B inventory', 'Weekend photography']) {
    const app = fixture(domain); app.render(); await tick(); let finish;
    app.setWrite(() => new Promise(resolve => {finish = resolve;}));
    app.checkbox().props.onChange({target: {checked: false}});
    assert.equal(app.checkbox().props.checked, true); assert.equal(app.checkbox().props.disabled, true);
    app.checkbox().props.onChange({target: {checked: false}});
    assert.equal(app.calls.filter(command => command === 'suggestions_set_prefs').length, 1);
    finish(null); await tick(); assert.equal(app.checkbox().props.checked, true);
    assert.match(JSON.stringify(app.render()), /last confirmed settings are shown/);
  }
});

test('15: unknown suggestion preferences remain unavailable until a successful read-only retry', async () => {
  for (const domain of ['B2B inventory', 'Weekend photography']) {
    const app = fixture(domain); app.setRead(async () => null); app.render(); await tick();
    assert.equal(app.checkbox().props.disabled, true); assert.match(JSON.stringify(app.render()), /Couldn’t read your saved settings/);
    app.checkbox().props.onChange({target: {checked: false}});
    assert.equal(app.calls.includes('suggestions_set_prefs'), false);
    app.setRead(async () => ({enabled: false}));
    nodes(app.render()).find(node => node.props.children === 'Refresh saved settings').props.onClick();
    app.render(); await tick(); assert.equal(app.checkbox().props.checked, false); assert.equal(app.checkbox().props.disabled, false);
  }
});

test('16: a failed pause clears old success feedback and asks you to reread its outcome', async () => {
  for (const domain of ['B2B inventory', 'Weekend photography']) {
    const app = fixture(domain); app.render(); await tick();
    app.checkbox().props.onChange({target: {checked: true}}); await tick(); assert.match(JSON.stringify(app.render()), /Saved\./);
    nodes(app.render()).find(node => node.props.children === 'Pause suggestions for today').props.onClick(); await tick();
    const tree = app.render(); assert.match(JSON.stringify(tree), /Couldn’t confirm the pause setting/);
    assert.doesNotMatch(JSON.stringify(tree), /Saved\./); assert.equal(app.checkbox().props.disabled, true);
    assert.ok(nodes(tree).some(node => node.props.children === 'Refresh saved settings'));
  }
});
