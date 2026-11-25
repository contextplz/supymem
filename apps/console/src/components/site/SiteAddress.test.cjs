const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

function harness(answers) {
  let cursor = 0, tree;
  const hooks = [], effects = [], requests = [], timers = [], ready = [];
  const react = {
    useState(initial) { const i = cursor++; hooks[i] ||= { value: initial }; return [hooks[i].value, v => { hooks[i].value = v; }]; },
    useRef(initial) { return hooks[cursor++] ||= { current: initial }; },
    useEffect(fn, deps) { const i = cursor++; if (!hooks[i]) { hooks[i] = { deps }; effects.push(fn); } },
  };
  const jsx = (type, props) => ({ type, props });
  const window = {
    setTimeout(fn) { timers.push(fn); return timers.length; },
    clearTimeout(id) { timers[id - 1] = null; },
  };
  const exports = {};
  const code = ts.transpileModule(readFileSync(join(__dirname, 'SiteAddress.tsx'), 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  vm.runInNewContext(code, { exports, window, encodeURIComponent, require(id) {
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
    if (id === '@/lib/client') return { api: async path => { requests.push(path); return answers(decodeURIComponent(path.split('slug=')[1])); } };
    if (id.endsWith('.css')) return { default: new Proxy({}, { get: (_, key) => String(key) }) };
    throw new Error(id);
  } });
  const render = () => {
    cursor = 0;
    tree = exports.SiteAddress({ ventureId: 'ven_a', onReady: (slug, current) => ready.push([slug, current]) });
    while (effects.length) effects.shift()();
    return tree;
  };
  const find = (node, test) => {
    if (!node || typeof node !== 'object') return null;
    if (Array.isArray(node)) { for (const c of node) { const r = find(c, test); if (r) return r; } return null; }
    if (node.props && test(node)) return node;
    return node.props ? find(node.props.children, test) : null;
  };
  const text = node => Array.isArray(node) ? node.map(text).join('') : typeof node === 'object' && node ? text(node.props?.children) : node ?? '';
  const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
  return { render, find, text, settle, requests, timers, ready, exports };
}

const free = { ok: true, data: { available: true, normalized: 'bakery', reason: null, current: null, suggestion: null } };
const taken = { ok: true, data: { available: false, normalized: 'bakery', reason: 'taken', suggestion: 'bakery-2' } };

test('prefills from the suggestion, then checks it', async () => {
  const h = harness(slug => slug === '' ? { ok: true, data: { available: false, normalized: '', reason: 'too_short', suggestion: 'my-venture' } }
    : { ok: true, data: { available: true, normalized: slug, reason: null } });
  h.render(); await h.settle();
  const tree = h.render();
  assert.equal(h.find(tree, n => n.type === 'input').props.value, 'my-venture');
  assert.match(h.text(h.find(tree, n => n.props.id === 'site-address-status')), /Available/);
  assert.deepEqual(h.ready.at(-1), ['my-venture', null]);
});

test('typing is debounced, then shows Available and reports the address', async () => {
  const h = harness(() => free);
  h.render(); await h.settle();
  const before = h.requests.length;
  const input = h.find(h.render(), n => n.type === 'input');
  input.props.onChange({ target: { value: 'Bakery' } });
  input.props.onChange({ target: { value: 'bakery' } });
  assert.equal(h.requests.length, before);                  // nothing sent while typing
  assert.equal(h.exports.DEBOUNCE_MS, 400);
  const pending = h.timers.filter(Boolean);
  assert.equal(pending.length, 1);                          // the first keystroke's timer was cancelled
  pending[0](); await h.settle();
  assert.equal(h.requests.length, before + 1);
  const status = h.find(h.render(), n => n.props.id === 'site-address-status');
  assert.equal(status.props['aria-live'], 'polite');
  assert.match(h.text(status), /Available/);
  assert.deepEqual(h.ready.at(-1), ['bakery', null]);
});

test('taken offers a clickable suggestion that is then checked', async () => {
  const h = harness(slug => slug === 'bakery-2' ? { ok: true, data: { available: true, normalized: 'bakery-2', reason: null } } : taken);
  h.render(); await h.settle();
  const input = h.find(h.render(), n => n.type === 'input');
  input.props.onChange({ target: { value: 'bakery' } });
  h.timers.filter(Boolean)[0](); await h.settle();
  const status = h.find(h.render(), n => n.props.id === 'site-address-status');
  assert.equal(h.text(status), 'Taken, try bakery-2');
  assert.equal(h.ready.at(-1)[0], null);                    // taken: publishing stays disabled
  h.find(status, n => n.type === 'button').props.onClick(); await h.settle();
  const after = h.render();
  assert.equal(h.find(after, n => n.type === 'input').props.value, 'bakery-2');
  assert.deepEqual(h.ready.at(-1), ['bakery-2', null]);
});

test('other reasons are plain words, and a failed check is not "available"', async () => {
  const reasons = { reserved: /kept for Supymem/, invalid: /letters, numbers/, too_short: /at least 3/, too_long: /at most 48/ };
  for (const [reason, pattern] of Object.entries(reasons)) {
    const h = harness(() => ({ ok: true, data: { available: false, normalized: 'x', reason, suggestion: 'my-venture' } }));
    h.render(); await h.settle();
    assert.match(h.text(h.find(h.render(), n => n.props.id === 'site-address-status')), pattern, reason);
    assert.equal(h.ready.at(-1)[0], null);
  }
  const down = harness(() => ({ ok: false, status: 500, data: {} }));
  down.render(); await down.settle();
  assert.match(down.text(down.find(down.render(), n => n.props.id === 'site-address-status')), /Could not check/);
  assert.equal(down.ready.at(-1)[0], null);
});

test('the field is labelled and a slow older answer never overwrites a newer one', async () => {
  let release;
  const h = harness(slug => slug === 'slow' ? new Promise(r => { release = () => r(taken); }) : free);
  h.render(); await h.settle();
  const tree = h.render();
  assert.equal(h.find(tree, n => n.type === 'label').props.htmlFor, h.find(tree, n => n.type === 'input').props.id);
  const input = h.find(tree, n => n.type === 'input');
  input.props.onChange({ target: { value: 'slow' } });
  h.timers.filter(Boolean).at(-1)(); await h.settle();
  input.props.onChange({ target: { value: 'fast' } });
  h.timers.filter(Boolean).at(-1)(); await h.settle();
  release(); await h.settle();
  assert.match(h.text(h.find(h.render(), n => n.props.id === 'site-address-status')), /Available/);
  assert.equal(h.ready.at(-1)[0], 'bakery');
});
