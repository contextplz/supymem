const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

function harness() {
  let owner = 'owner-a', epoch = 1, cursor = 0, finish, path = '/v/ven_a/site';
  const events = [];
  const window = {location: {pathname: path, href: 'https://fixture.example' + path}, dispatchEvent: event => events.push(event.detail)};
  const hooks = [], requests = [], navigations = [], effects = [];
  const react = {
    useState(initial) { const i = cursor++; hooks[i] ||= { value: initial }; return [hooks[i].value, v => hooks[i].value = v]; },
    useRef(initial) { return hooks[cursor++] ||= { current: initial }; },
    useEffect(fn, deps) {
      const i = cursor++, before = hooks[i];
      if (!before || deps.some((v, index) => v !== before.deps[index])) effects.push(() => {
        before?.cleanup?.(); hooks[i] = {deps, cleanup: fn()};
      });
    },
  };
  const jsx = (type, props) => ({ type, props });
  const exports = {};
  const code = ts.transpileModule(readFileSync(join(__dirname, 'PageChoices.tsx'), 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  }}).outputText;
  vm.runInNewContext(code, {exports, window, CustomEvent: class { constructor(name, options) { this.detail = options.detail; } }, require(id) {
    if (id === "@/components/chat/ChatLink") return { default: "link", ChatAnchor: "a", captureChatLink() {} };

    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id === 'next/link') return {default: 'link'};
    if (id === 'next/navigation') return {usePathname: () => path, useRouter: () => ({push: href => navigations.push(href)})};
    if (id === '@/components/primitives/ErrorNotice') return {ErrorNotice: 'notice'};
    if (id.endsWith('.css')) return {default: {button: 'same-style'}};
    if (id === '@/lib/client') return {
      verifiedSessionSubject: () => owner, verifiedSessionEpoch: () => epoch,
      api: (path, options) => { requests.push({path, options}); return new Promise(resolve => finish = resolve); },
    };
    throw new Error(id);
  }});
  return {requests, navigations, events, render(props = {}) {
    cursor = 0; effects.length = 0;
    const tree = exports.PageChoices({ventureId: 'ven_a', ...props});
    effects.forEach(fn => fn());
    const cleanups = hooks.filter(hook => hook.cleanup).map(hook => hook.cleanup);
    const nodes = [];
    function visit(v) { if (!v) return; if (Array.isArray(v)) return v.forEach(visit);
      if (typeof v === 'object') {nodes.push(v); visit(v.props?.children);} }
    visit(tree);
    return {nodes, cleanup: () => cleanups.forEach(fn => fn?.())};
  }, retire() {owner = 'owner-b'; epoch++;}, restore() {owner = 'owner-a'; epoch++;},
    route(next) {const url = new URL(next, 'https://fixture.example'); path = url.pathname;
      window.location.pathname = path; window.location.href = url.href;}, finish(ok = true) {finish({ok});}};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('secondary skip asks for confirmation without saving or navigating, and build remains available', () => {
  const h = harness(), {nodes} = h.render();
  const build = nodes.find(n => n.type === 'link'), skip = nodes.find(n => n.type === 'button');
  assert.equal(build.props.className, 'primary');
  assert.equal(skip.props.className, 'ghost sm');
  assert.equal(build.props.href, '/v/ven_a/site?build=1');
  skip.props.onClick();
  assert.equal(h.events[0].action, 'journey.skip_step');
  assert.equal(h.events[0].payload.step, 'landing_page');
  assert.deepEqual(h.requests, []);
  assert.deepEqual(h.navigations, []);
});

test('retired or unmounted choices never dispatch confirmation', () => {
  for (const state of ['retired', 'unmounted']) {
    const h = harness(), view = h.render();
    if (state === 'retired') h.retire();
    if (state === 'unmounted') view.cleanup();
    view.nodes.find(n => n.type === 'button').props.onClick();
    assert.deepEqual(h.events, []);
    assert.deepEqual(h.navigations, []);
  }
});

test('the website choice invokes its existing approved build path', () => {
  let starts = 0;
  const h = harness(), {nodes} = h.render({onBuild: () => starts++});
  nodes.find(n => n.type === 'button' && n.props['data-usage-control'] === 'page_choice_build').props.onClick();
  assert.equal(starts, 1);
  assert.deepEqual(h.requests, []);
});


test('retired controls refuse effects at entry and skip never mutates the venture', () => {
  const h = harness(), view = h.render();
  const skip = view.nodes.find(n => n.type === 'button');
  skip.props.onClick(); skip.props.onClick();
  assert.equal(h.requests.length, 0);
  let blocked = false;
  view.nodes.find(n => n.type === 'link').props.onClick({preventDefault: () => blocked = true});
  assert.equal(blocked, false);
  for (const retire of ['account', 'unmount']) {
    const old = harness(), v = old.render();
    if (retire === 'account') old.retire(); else v.cleanup();
    v.nodes.find(n => n.type === 'button').props.onClick();
    assert.deepEqual(old.requests, []);
    assert.deepEqual(old.events, []);
  }
});


test('a saved callback stays retired after venture or account returns to its original value', () => {
  for (const transition of ['venture', 'account']) {
    const h = harness(), old = h.render();
    const skip = old.nodes.find(n => n.type === 'button').props.onClick;
    if (transition === 'venture') {
      h.route('/v/ven_b/site'); h.render({ventureId: 'ven_b'});
      h.route('/v/ven_a/site'); h.render();
    } else { h.retire(); h.render(); h.restore(); h.render(); }
    skip();
    assert.deepEqual(h.requests, []);
    assert.deepEqual(h.events, []);
  }
});

test('a confirmation request does not navigate a newer venture on the same GTM pathname', () => {
  const h = harness(); h.route('/gtm?venture=ven_a');
  const view = h.render();
  view.nodes.find(n => n.type === 'button').props.onClick();
  assert.equal(h.events.length, 1);
  h.route('/gtm?venture=ven_b');
  assert.deepEqual(h.navigations, []);
});

test('same-path query navigation retires saved skip and build controls before rerender', () => {
  const h = harness(); h.route('/gtm?venture=ven_a');
  let starts = 0;
  const view = h.render({onBuild: () => starts++});
  h.route('/gtm?venture=ven_b');
  view.nodes.find(n => n.props?.['data-usage-control'] === 'page_choice_skip').props.onClick();
  view.nodes.find(n => n.props?.['data-usage-control'] === 'page_choice_build').props.onClick();
  assert.deepEqual(h.requests, []); assert.equal(starts, 0);
  assert.deepEqual(h.events, []);
});
