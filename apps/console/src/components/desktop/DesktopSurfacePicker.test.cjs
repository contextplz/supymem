const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');
const settle = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  let cursor = 0, effects = [], tree, epoch = 1, ventureId = 'v_one';
  let snapshot = {'screen-recording': 'not_determined', accessibility: 'granted'};
  const hooks = [], calls = [], events = [];
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const react = {
    useState(initial) {const i = cursor++, slot = hooks[i] ||= {value: initial}; return [slot.value, next => {slot.value = typeof next === 'function' ? next(slot.value) : next;}];},
    useRef(initial) {const i = cursor++; return hooks[i] ||= {current: initial};},
    useEffect(effect, deps) {const i = cursor++, prior = hooks[i]; if (!prior || !same(prior.deps, deps)) effects.push(() => {prior?.cleanup?.(); hooks[i] = {deps, cleanup: effect()};});},
  };
  const native = async command => {calls.push(command); assert.ok(['permissions_status', 'desktop_app_inventory'].includes(command)); return command === 'permissions_status' ? snapshot : {items: []};};
  const jsx = (type, props) => ({type, props: props || {}});
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, 'DesktopSurfacePicker.tsx'), 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX}}).outputText, {
    exports, require: id => {
      if (id === 'react') return react;
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx, Fragment: 'fragment'};
      if (id === '@/lib/desktop') return {desktopInvoke: native, desktopInvokeOrThrow: native};
      if (id === '@/lib/client') return {verifiedSessionEpoch: () => epoch};
      if (id === '@/lib/macPermissions') return {MAC_TASK_PERMISSION_EVENT: 'supymem-mac-permissions-needed', macTaskPermissionsGranted: value => value?.['screen-recording'] === 'granted' && value?.accessibility === 'granted'};
      if (id === '@/lib/desktopSurfaceReview') return {surfaceKey: () => '', validDesktopSurface: () => true};
      if (id === '@/components/primitives/ErrorNotice') return {ErrorNotice: 'notice'};
      throw Error('unexpected import ' + id);
    },
    CustomEvent: class {constructor(type, init) {this.type = type; this.detail = init.detail;}},
    window: {dispatchEvent: event => {events.push(event);}},
  });
  const render = () => {cursor = 0; tree = exports.DesktopSurfacePicker({ventureId, disabled: false, onChange: () => {}}); const active = effects; effects = []; active.forEach(fn => fn()); return tree;};
  const choose = async () => {render(); nodes(tree).find(node => node.type === 'input').props.onChange({target: {checked: true}}); await settle(); render();};
  return {render, choose, calls, events, allow: () => {snapshot = {'screen-recording': 'granted', accessibility: 'granted'};}, move: () => {ventureId = 'v_other'; epoch++; render();}};
}
function nodes(tree) {if (!tree || typeof tree !== 'object') return []; if (Array.isArray(tree)) return tree.flatMap(nodes); return [tree, ...nodes(tree.props?.children)];}

test('window selection reads passive status before inventory and resumes only on explicit permission completion', async () => {
  const h = harness(); h.render(); assert.equal(h.calls.length, 0);
  await h.choose(); assert.deepEqual(h.calls, ['permissions_status']);
  assert.equal(h.events[0].type, 'supymem-mac-permissions-needed');
  h.allow(); await h.events[0].detail.resume();
  assert.deepEqual(h.calls, ['permissions_status', 'permissions_status', 'desktop_app_inventory']);
});

test('a retired window selection cannot inventory another venture from an old permission card', async () => {
  const h = harness(); await h.choose(); h.move(); h.allow(); await h.events[0].detail.resume();
  assert.deepEqual(h.calls, ['permissions_status']);
});
