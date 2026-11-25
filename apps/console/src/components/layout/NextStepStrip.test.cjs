const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

function harness(clarification = null) {
  let owner = 'owner-a', epoch = 1, pathname = '/v/venture-a/idea', cursor = 0;
  const hooks = [], effects = [], events = [];
  const window = { location: { pathname, href: 'https://console.example' + pathname }, dispatchEvent: event => events.push(event.detail) };
  const react = {
    createContext: () => null,
    useContext: () => clarification,
    useState(initial) { const index = cursor++; if (!(index in hooks)) hooks[index] = initial; return [hooks[index], value => { hooks[index] = value; }]; },
    useRef(initial) { return hooks[cursor++] ||= { current: initial }; },
    useEffect(fn, deps) {
      const index = cursor++, before = hooks[index];
      if (!before || deps.some((value, position) => value !== before.deps[position])) effects.push(() => {
        before?.cleanup?.(); hooks[index] = { deps, cleanup: fn() };
      });
    },
  };
  const output = ts.transpileModule(readFileSync(join(__dirname, 'NextStepStrip.tsx'), 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const exports = {}, jsx = (type, props) => ({ type, props });
  vm.runInNewContext(output, { exports, window, CustomEvent: class { constructor(_name, options) { this.detail = options.detail; } }, require(id) {
    if (id === "@/components/chat/ChatLink") return { default: "link", ChatAnchor: "a", captureChatLink() {} };

    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id === 'next/link') return { default: 'link' };
    if (id === 'next/navigation') return { usePathname: () => pathname };
    if (id === '@/lib/client') return { verifiedSessionSubject: () => owner, verifiedSessionEpoch: () => epoch };
    if (id === '@/lib/runStatusPresentation') return {};
    if (id === '@/lib/nextStep') return {};
    if (id === '@/lib/founderUI') return { FounderUIContext: {} };
    if (id === '@/components/site/PageChoices') return {};
    if (id.endsWith('.css')) return {};
    throw new Error(id);
  } });
  const step = { skip_step: 'research', has_website: true, receiptScope: { venture: 'venture-a', owner, epoch } };
  return { events, step, retire() { owner = 'owner-b'; epoch++; },
    render(props = {}) { cursor = 0; const tree = exports.JourneyChoices({ step, ...props }); effects.splice(0).forEach(fn => fn()); return tree; },
    navigate() { pathname = '/v/venture-b/idea'; window.location.pathname = pathname; window.location.href = 'https://console.example' + pathname; },
    unmount() { hooks.forEach(hook => hook?.cleanup?.()); } };
}
function nodes(tree) {
  if (!tree) return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return typeof tree === 'object' ? [tree, ...nodes(tree.props?.children)] : [];
}
test('skip and website jump request separate confirm cards with an honest consequence', () => {
  const h = harness(), controls = nodes(h.render());
  controls.find(node => node.props?.children === 'Skip this step').props.onClick();
  assert.equal(h.events[0].action, 'journey.skip_step');
  assert.equal(h.events[0].payload.step, 'research');
  controls.find(node => node.props?.children === 'Skip to Find customers').props.onClick();
  assert.equal(h.events[1].action, 'journey.jump_to');
  assert.equal(h.events[1].payload.step, 'find_customers');
  assert.ok(controls.some(node => typeof node.props?.children === 'string' && node.props.children.includes('lower confidence')));
});
test('restore requests confirmation, not a silent completion', () => {
  const h = harness(); h.render({ restore: 'research' }).props.onClick();
  assert.equal(h.events[0].action, 'journey.restore_step');
});
test('saved skip callbacks refuse retired account, route, and unmounted scopes', () => {
  for (const retire of ['retire', 'navigate', 'unmount']) {
    const h = harness(), handler = nodes(h.render()).find(node => node.props?.children === 'Skip this step').props.onClick;
    h[retire](); handler(); assert.deepEqual(h.events, []);
  }
});

test('clarification strip invokes the page skip path and announces progress, success, and retry', async () => {
  for (const saved of [true, false, 'throw']) {
    let finish, calls = 0;
    const h = harness({disabled: false, skip: () => { calls++; return new Promise((resolve, reject) => {finish = () => saved === 'throw' ? reject(new Error('offline')) : resolve(saved);}); }});
    h.step.skip_step = 'clarification';
    nodes(h.render()).find(node => node.props?.children === 'Skip this step').props.onClick();
    assert.equal(calls, 1); assert.deepEqual(h.events, []);
    let tree = nodes(h.render());
    assert.ok(tree.some(node => node.props?.role === 'status' && node.props.children === 'Skipping questions…'));
    assert.equal(tree.find(node => node.props?.children === 'Skip this step').props.disabled, true);
    finish(); await new Promise(resolve => setImmediate(resolve));
    tree = nodes(h.render());
    assert.ok(tree.some(node => node.props?.role === 'status' && node.props.children === (saved === true ? 'Questions skipped. Preparing your next step…' : 'Questions could not be skipped. Try again.')));
    assert.equal(tree.find(node => node.props?.children === 'Skip this step').props.disabled, false);
  }
  const h = harness({disabled: true, skip: () => {throw new Error('must not run');}}); h.step.skip_step = 'clarification';
  nodes(h.render()).find(node => node.props?.children === 'Skip this step').props.onClick();
  assert.deepEqual(h.events, []);
});

test('Research keeps a runnable saved-context action after setup skips without changing other pages', () => {
  for (const key of ['idea.review_validation', 'gtm.outreach']) {
    for (const pathname of ['/v/venture-a/research', '/v/venture-a/idea']) {
    const step = { key, title: 'Review idea', detail: 'Recommended validation',
      href: '/v/venture-a/idea', journey_overrides: { skipped: { clarification: {} } },
      receiptScope: { venture: 'venture-a', owner: 'owner-a', epoch: 1 } };
    const output = ts.transpileModule(readFileSync(join(__dirname, 'NextStepStrip.tsx'), 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
    } }).outputText;
    const exports = {}, jsx = (type, props) => ({ type, props });
    vm.runInNewContext(output, { exports, window: { location: { pathname, href: 'https://console.example' + pathname } }, require(id) {
    if (id === "@/components/chat/ChatLink") return { default: "link", ChatAnchor: "a", captureChatLink() {} };

      if (id === 'react') return { createContext: () => null, useContext: () => false,
        useState: () => [step, () => {}], useEffect() {}, useRef: current => ({ current }) };
      if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
      if (id === 'next/navigation') return { usePathname: () => pathname };
      if (id === '@/lib/client') return { verifiedSessionSubject: () => 'owner-a', verifiedSessionEpoch: () => 1 };
      if (id === '@/lib/nextStep') return { ownedByGtmFlow: () => false, failureOwnsCta: () => false, hasPageChoices: () => false };
      if (id.endsWith('.css')) return { default: {} };
      return {};
    } });
    const tree = exports.NextStepStrip({ ventureId: 'venture-a' });
    const button = nodes(tree).find(node => node.type === exports.NextStepButton);
    assert.ok(button);
    assert.equal(button.props.step.chat_action, pathname.endsWith('/research') ? 'chat.prefill' : undefined);
    assert.equal(button.props.step.key, pathname.endsWith('/research') ? 'research.start' : key);
    assert.equal(button.props.step.receiptScope.owner, 'owner-a');
    }
  }
});
