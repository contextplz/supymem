const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

const settle = () => new Promise(resolve => setImmediate(resolve));

test('your local permission coach selects your local bundle without renaming the hosted app across domains', async () => {
  const root = join(__dirname, '../../../../../');
  const html = readFileSync(join(root, 'src-tauri/splashscreen/coach.html'), 'utf8');
  for (const domain of ['Warehouse routing', 'Weekend cooking']) {
    for (const local of [true, false]) {
      const elements = new Map();
      const element = id => {
        if (!elements.has(id)) elements.set(id, {textContent: id === 'wait' ? html.match(/id="wait">([^<]+)/)[1] : '', classList: {add() {}}, addEventListener(event, handler) {this[event] = handler;}});
        return elements.get(id);
      };
      let listener;
      const window = {__SUPYMEM__: {mode: local ? 'local' : 'hosted'}, __TAURI__: {core: {invoke: async () => ({kind: 'accessibility', title: domain, steps: []})}, event: {listen: async (event, handler) => {listener = handler;}}}, setInterval() {}, setTimeout() {}};
      vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], {window, document: {getElementById: element}});
      await settle();
      listener({payload: {kind: 'accessibility', title: domain, steps: []}});
      assert.ok(element('stage').innerHTML.includes(local ? 'Supymem Local' : 'Supymem'));
      assert.ok(element('wait').textContent.includes(`/Applications/${local ? 'Supymem Local' : 'Supymem'}.app`));
      await element('reopen').click();
      assert.ok(element('wait').textContent.includes(`Turn on ${local ? 'Supymem Local' : 'Supymem'},`));
      if (!local) assert.ok(!element('stage').innerHTML.includes('Supymem Local'));
    }
  }
  const native = readFileSync(join(root, 'src-tauri/src/perms.rs'), 'utf8');
  assert.match(native, /fn permission_copy[\s\S]*?crate::local_stack::is_local\(\)/);
});
function harness({snapshot = {'screen-recording': 'not_determined', accessibility: 'granted'}, storage = new Map(), task = true} = {}) {
  let cursor = 0, effects = [], tree, granted = 0, currentSnapshot = snapshot;
  const hooks = [], calls = [];
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const react = {
    useState(initial) { const i = cursor++, slot = hooks[i] ||= {value: typeof initial === 'function' ? initial() : initial}; return [slot.value, next => {slot.value = typeof next === 'function' ? next(slot.value) : next;}]; },
    useRef(initial) { const i = cursor++; return hooks[i] ||= {current: initial}; },
    useCallback(callback) { cursor++; return callback; },
    useEffect(effect, deps) { const i = cursor++, prior = hooks[i]; if (!prior || !same(prior.deps, deps)) effects.push(() => {prior?.cleanup?.(); hooks[i] = {deps, cleanup: effect()};}); },
  };
  const window = {localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)}};
  const jsx = (type, props) => ({type, props: props || {}});
  function compile(path, requireModule) {
    const exports = {};
    vm.runInNewContext(ts.transpileModule(readFileSync(path, 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX}}).outputText,
      {exports, require: requireModule, window});
    return exports;
  }
  const permissions = compile(join(__dirname, '../../lib/macPermissions.ts'), () => {throw Error('unexpected dynamic import');});
  const component = compile(join(__dirname, 'DesktopPermissionSetup.tsx'), id => {
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx, Fragment: 'fragment'};
    if (id === '@/lib/macPermissions') return permissions;
    if (id === '@/lib/desktop') return {isDesktopShell: () => true, desktopInvokeOrThrow: async (command, args) => {
      calls.push({command, args});
      assert.ok(['permissions_status', 'permissions_request', 'permissions_open_settings'].includes(command), 'helper permission probe must never run');
      if (command === 'permissions_request') assert.equal(storage.get('supymem-' + args.name + '-requested'), 'yes', 'persist before native request');
      return currentSnapshot;
    }};
    if (id === '@/components/primitives/ErrorNotice') return {ErrorNotice: 'notice'};
    throw Error('unexpected import ' + id);
  });
  const render = () => {cursor = 0; tree = component.DesktopPermissionSetup({inline: true, task, onGranted: () => {granted++;}}); const active = effects; effects = []; active.forEach(fn => fn()); return tree;};
  const click = async label => {render(); const button = nodes(tree).find(node => node.type === 'button' && text(node) === label); assert.ok(button, label); button.props.onClick(); await settle(); render();};
  return {render, click, calls, storage, snapshot: value => {currentSnapshot = value;}, get tree() {return tree;}, get granted() {return granted;}};
}
function nodes(tree) {if (!tree || typeof tree !== 'object') return []; if (Array.isArray(tree)) return tree.flatMap(nodes); return [tree, ...nodes(tree.props?.children)];}
function text(tree) {if (typeof tree === 'string') return tree; if (Array.isArray(tree)) return tree.map(text).join(''); return tree && typeof tree === 'object' ? text(tree.props?.children) : '';}

test('first task card reads passive native status, and only an explicit click requests once', async () => {
  const h = harness(); h.render(); await settle(); h.render();
  assert.deepEqual(h.calls.map(call => call.command), ['permissions_status']);
  assert.match(text(h.tree), /Supymem needs Screen Recording to see the app/);
  const grant = nodes(h.tree).find(node => node.type === 'button' && text(node) === 'Grant permission');
  assert.ok(grant); grant.props.onClick(); grant.props.onClick(); await settle(); h.render();
  assert.equal(h.calls.filter(call => call.command === 'permissions_request').length, 1);
  assert.equal(h.storage.get('supymem-screen-recording-requested'), 'yes');
  assert.doesNotMatch(text(h.tree), /Grant permission/);
  assert.match(text(h.tree), /Open System Settings.*Retry/);
  assert.equal(h.granted, 0);
  const reopened = harness({storage: h.storage}); reopened.render(); await settle(); reopened.render();
  assert.doesNotMatch(text(reopened.tree), /Grant permission/);
  assert.deepEqual(reopened.calls.map(call => call.command), ['permissions_status']);
});

test('after denial Settings and Retry stay passive and resume only after both permissions are granted', async () => {
  const h = harness({storage: new Map([['supymem-screen-recording-requested', 'yes']])}); h.render(); await settle(); h.render();
  await h.click('Open System Settings'); await h.click('Retry');
  assert.equal(h.granted, 0);
  h.snapshot({'screen-recording': 'granted', accessibility: 'granted'}); await h.click('Retry');
  assert.equal(h.granted, 1);
  assert.equal(h.calls.filter(call => call.command === 'permissions_request').length, 0);
  assert.deepEqual(h.calls.at(-1), {command: 'permissions_status', args: undefined});
});

test('accessibility uses its own explanation and request after screen access is granted', async () => {
  const h = harness({snapshot: {'screen-recording': 'granted', accessibility: 'not_determined'}}); h.render(); await settle(); h.render();
  assert.match(text(h.tree), /Supymem needs Accessibility to click and type/);
  assert.doesNotMatch(text(h.tree), /needs Screen Recording/);
  await h.click('Grant permission');
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))), {command: 'permissions_request', args: {name: 'accessibility'}});
  assert.equal(h.storage.get('supymem-accessibility-requested'), 'yes');
});

test('Settings reads permission status without prompting or implying task availability', async () => {
  const h = harness({task: false, snapshot: {'screen-recording': 'granted', accessibility: 'granted'}}); h.render(); await settle(); h.render();
  assert.deepEqual(h.calls.map(call => call.command), ['permissions_status']);
  assert.match(text(h.tree), /Granted/);
  assert.match(text(h.tree), /Granting a permission does not make a task available/);
  assert.equal(h.granted, 0);
});
