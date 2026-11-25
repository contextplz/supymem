const chatRoutes = require("../../lib/chatRouteAction.ts");
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

function harness(local = false) {
  let cursor = 0, tree, subject = 'founder-a', epoch = 1, closes = 0;
  const hooks = [], effects = [], requests = [], locations = [], listeners = new Map();
  const react = {
    useState(initial) {
      const index = cursor++;
      hooks[index] ||= { value: initial };
      return [hooks[index].value, next => { hooks[index].value = typeof next === 'function' ? next(hooks[index].value) : next; }];
    },
    useRef(initial) { return hooks[cursor++] ||= { current: initial }; },
    useEffect(run, deps) {
      const index = cursor++, before = hooks[index];
      if (!before || deps.some((value, position) => value !== before.deps[position])) effects.push(() => {
        before?.cleanup?.(); hooks[index] = { deps, cleanup: run() };
      });
    },
  };
  const jsx = (type, props) => typeof type === 'function' ? type(props) : { type, props: props || {} };
  const window = {
    location: { assign: path => locations.push(path) },
    addEventListener: (type, run) => listeners.set(type, run),
    removeEventListener: type => listeners.delete(type),
  };
  const request = (path, init) => new Promise(resolve => requests.push({ path, init, resolve }));
  const exports = {};
  const path = join(__dirname, '../office/CompanySettingsDialog.tsx');
  const output = ts.transpileModule(readFileSync(path, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  vm.runInNewContext(output, { exports, window, require: id => {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    if (id === "@/lib/chatRouteAction") return chatRoutes;
    if (id === "@/components/settings/AccountSettings") return { AccountSettings: "account-settings" };

    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id === 'next/link') return { default: props => jsx('a', props) };
    if (id === '@/components/settings/VentureSettings') return { VentureSettings: props => jsx('settings', props) };
    if (id === '@/lib/client') return {
      api: request, logout: () => request('logout'),
      ensureSession: async () => ({ ok: true, data: { email: `${subject}@example.com` } }),
      verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
    };
    if (id === '@/lib/desktop') return { isDesktopShell: () => false };
    if (id.endsWith('/LocalModeBoundary')) return { useLocalMode: () => local };
    if (id === '@/lib/serverReason') return { serverReason: (result, fallback) => result.error || fallback };
    if (id.endsWith('.module.css')) return { default: new Proxy({}, { get: (_, key) => key }) };
    throw new Error(`Unexpected import ${id}`);
  } }, { filename: path });
  return {
    requests, locations,
    get tree() { return tree; },
    get closes() { return closes; },
    render() {
      cursor = 0; tree = exports.CompanySettingsDialog({ ventureId: 'venture-a', onClose: () => { closes++; } });
      effects.splice(0).forEach(run => run()); return tree;
    },
    changeOwner(next) { subject = next; epoch++; listeners.get('supymem-session-changed')?.(); },
  };
}
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props.children)];
const text = tree => typeof tree === 'string' ? tree : Array.isArray(tree) ? tree.map(text).join(' ') : tree && typeof tree === 'object' ? text(tree.props.children) : '';

test('your local settings navigation has no hosted purchase pane across unrelated domains', () => {
  for (const domain of ['Industrial procurement', 'Family cooking']) {
    const app = harness(true); app.render();
    assert.doesNotMatch(text(app.tree), /Buy more/, domain);
    assert.doesNotMatch(text(app.tree), /Log out|Signing out/, domain);
    assert.ok(nodes(app.tree).some(node => node.type === 'settings' && node.props.section === 'venture'));
  }
});
const button = (h, label) => nodes(h.tree).find(node => node.type === 'button' && text(node) === label);
const settle = async () => { for (let count = 0; count < 5; count++) await Promise.resolve(); };

test('settings dialog has six sections, real account identity, selected pane and working search', async () => {
  const h = harness(); h.render(); await settle(); h.render();
  assert.match(text(h.tree), /founder-a@example.com/);
  h.requests[0].resolve({ ok: true, data: { first_name: 'Actual founder' } }); await settle(); h.render();
  assert.match(text(h.tree), /Actual founder/);
  const nav = nodes(h.tree).find(node => node.type === 'nav');
  assert.deepEqual(nodes(nav).filter(node => node.type === 'button').map(node => text(node)), ['Venture', 'AI manager', 'Models', 'This Mac', 'Your data', 'Memory']);
  assert.equal(button(h, 'Venture').props['aria-current'], 'page');
  assert.equal(typeof nodes(h.tree).find(node => node.type === 'settings').props.onClose, 'function');
  button(h, 'Memory').props.onClick(); h.render();
  assert.equal(nodes(h.tree).find(node => node.type === 'settings').props.section, 'memory');
  assert.equal(nodes(h.tree).find(node => node.type === 'h2').props.children, 'Memory');
  const search = () => nodes(h.tree).find(node => node.props['aria-label'] === 'Search settings');
  search().props.onChange({ target: { value: ' mod ' } }); h.render(); h.render();
  assert.equal(nodes(h.tree).find(node => node.type === 'settings').props.section, 'models');
  search().props.onChange({ target: { value: 'not a setting' } }); h.render();
  assert.match(text(h.tree), /No matching settings/); assert.ok(button(h, 'Log out'));
  button(h, '×').props.onClick(); assert.equal(h.closes, 1);
  h.tree.props.onCancel(); assert.equal(h.closes, 2);
  h.tree.props.onClick({ target: {}, currentTarget: {} }); assert.equal(h.closes, 2);
  const overlay = {}; h.tree.props.onClick({ target: overlay, currentTarget: overlay }); assert.equal(h.closes, 3);
});

test('logout is anchored below navigation and failed logout remains retryable without leaving the account', async () => {
  const h = harness(); h.render(); await settle(); h.render();
  const aside = nodes(h.tree).find(node => node.type === 'aside');
  assert.equal(aside.props.children.at(-1).props.className, 'foot');
  button(h, 'Log out').props.onClick(); button(h, 'Log out').props.onClick();
  assert.equal(h.requests.filter(request => request.path === 'logout').length, 1);
  h.requests.at(-1).resolve({ ok: false, error: 'Unlock Keychain and retry sign-out.' }); await settle(); h.render();
  assert.match(text(h.tree), /Unlock Keychain/); assert.equal(h.locations.length, 0); assert.equal(h.closes, 0);
  button(h, 'Log out').props.onClick();
  h.requests.at(-1).resolve({ ok: true, data: { upstream_logout_url: 'https://accounts.example.com/logout' } }); await settle();
  assert.deepEqual(h.locations, ['https://accounts.example.com/logout']); assert.equal(h.closes, 1);
});

test('account changes discard late private identity responses', async () => {
  const h = harness(); h.render(); await settle();
  const old = h.requests[0]; h.changeOwner('founder-b'); await settle(); h.render();
  old.resolve({ ok: true, data: { first_name: 'Previous private founder' } }); await settle(); h.render();
  assert.doesNotMatch(text(h.tree), /Previous private founder/); assert.match(text(h.tree), /founder-b@example.com/);
});

test('dialog geometry and settings rows retain PR CSS values and visible keyboard focus', () => {
  const css = readFileSync(join(__dirname, '../office/CompanySettingsDialog.module.css'), 'utf8');
  for (const value of ['padding: 28px', 'rgba(28, 28, 30, 0.28)', 'width: min(980px, 100%)', 'height: min(680px, 100%)', 'grid-template-columns: 232px minmax(0, 1fr)', 'background: #f3f0f7', 'width: 32px', 'width: 28px', '650 22px/1.2', 'grid-template-columns: 140px minmax(0, 1fr)', 'min-height: 52px', '#eeeef2']) assert.ok(css.includes(value), value);
  assert.match(css, /\.foot\s*\{\s*margin-top: auto/);
  assert.match(css, /focus-visible[^}]+outline: 2px solid #007aff/s);
});
