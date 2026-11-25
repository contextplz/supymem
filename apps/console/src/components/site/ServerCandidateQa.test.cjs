const chatRoutes = require("../../lib/chatRouteAction.ts");
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

function harness(domain = 'task_a') {
  let cursor = 0, actor = 'actor-a', epoch = 1, effects = [];
  const hooks = [], requests = [], navigations = [], listeners = new Map();
  const window = {location: {pathname: '/v/ven_a/site', assign: value => navigations.push(value)},
    addEventListener: (kind, fn) => listeners.set(kind, fn), removeEventListener: kind => listeners.delete(kind)};
  const react = {
    useState: value => { const i = cursor++; hooks[i] ||= {value}; return [hooks[i].value, v => hooks[i].value = v]; },
    useRef: value => hooks[cursor++] ||= {current: value},
    useEffect: (fn, deps) => { const i = cursor++, before = hooks[i];
      if (!before || deps.some((v, n) => v !== before.deps[n])) effects.push(() => {before?.cleanup?.(); hooks[i] = {deps, cleanup: fn()};}); },
  };
  const jsx = (type, props) => ({type, props});
  const output = ts.transpileModule(fs.readFileSync(__dirname + '/ServerCandidateQa.tsx', 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022},
  }).outputText;
  const exports = {};
  vm.runInNewContext(output, {exports, window, AbortController, Date, require: id => {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    if (id === "@/lib/chatRouteAction") return chatRoutes;
    if (id === "@/components/settings/AccountSettings") return { AccountSettings: "account-settings" };

    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    if (id === '@/lib/client') return {verifiedSessionSubject: () => actor, verifiedSessionEpoch: () => epoch,
      api: (path, init) => new Promise(resolve => requests.push({path, init, resolve}))};
    if (id.endsWith('/ErrorNotice')) return {ErrorNotice: 'error'};
    if (id.endsWith('/apiFailure')) return {apiFailure: require('../../lib/apiFailure.ts').apiFailure};
    throw new Error(id);
  }});
  let selection = {};
  const render = (changed) => {if (changed) selection = changed; cursor = 0; const tree = exports.ServerCandidateQa({ventureId: 'ven_a', jobId: 'job_a', variant: 0, planId: 'plan_a', taskId: domain, ...selection}); effects.splice(0).forEach(fn => fn()); return tree;};
  function find(tree, predicate) {if (!tree) return null; if (Array.isArray(tree)) return tree.map(x => find(x, predicate)).find(Boolean) || null; if (predicate(tree)) return tree; return find(tree.props?.children, predicate);}
  const flush = () => new Promise(setImmediate);
  const button = name => find(render(), n => n.type === 'button' && n.props.children === name);
  const quote = {quote_id: 'q_a', quote_digest: 'a'.repeat(64), qa_ceiling_micro: 12345, credits: 1.2345, expires_at: new Date(Date.now() + 600000).toISOString()};
  return {render, button, requests, navigations, quote, window, flush,
    approve: () => find(render(), n => n.type === 'input').props.onChange({target: {checked: true}}),
    retire: () => {actor = 'actor-b'; epoch++; listeners.get('supymem-session-changed')?.();},
    unmount: () => hooks.forEach(h => h.cleanup?.())};
}

test('your requested QA starts directly with its exact quote and duplicate clicks share one request in unrelated domains', async () => {
  for (const domain of ['Factory procurement', 'Family cycling']) {
  const h = harness(domain); h.render(); assert.equal(h.requests.length, 0);
  const start = h.button('Run independent QA') || h.button('Estimate independent QA'); const a = start.props.onClick(); start.props.onClick();
  assert.equal(h.requests.length, 1); assert.ok(h.requests[0].path.endsWith('/quote'));
  h.requests[0].resolve({ok: true, data: h.quote}); await a; await h.flush();
  assert.equal(h.requests.length, 2);
  assert.doesNotMatch(JSON.stringify(h.render()), /1\.2345|credits|cost ceiling/);
  const body = JSON.parse(h.requests[1].init.body);
  assert.equal(body.approve_qa_ceiling_micro, 12345); assert.equal(body.confirmed, true);
  assert.equal(body.job_id, 'job_a'); assert.equal(body.quote_digest, h.quote.quote_digest);
  h.requests[1].resolve({ok: true, data: {id: 'bpg_a', app_id: 'app_a'}}); await h.flush();
  assert.deepEqual(h.navigations, ['/v/ven_a/builds?app=app_a&run=bpg_a']);
  }
});

for (const retire of ['actor', 'path', 'unmount']) test(`late admission cannot navigate after ${retire} retirement`, async () => {
  const h = harness(); h.render(); const a = h.button('Run independent QA').props.onClick();
  h.requests[0].resolve({ok: true, data: h.quote}); await a; await h.flush();
  if (retire === 'actor') h.retire(); else if (retire === 'path') h.window.location.pathname = '/v/ven_other/site'; else h.unmount();
  h.requests[1].resolve({ok: true, data: {id: 'bpg_a', app_id: 'app_a'}}); await h.flush();
  assert.equal(h.navigations.length, 0);
});

test('a prior committed QA intent only offers its saved review without another admission', async () => {
  const h = harness(); h.render(); const a = h.button('Run independent QA').props.onClick();
  h.requests[0].resolve({ok: true, data: {existing_run: {id: 'bpg_a', app_id: 'app_a'}}}); await a; await h.flush();
  assert.equal(h.button('Start reviewed QA'), null); assert.equal(h.requests.length, 1);
  h.button('Open saved QA review').props.onClick(); assert.equal(h.requests.length, 1); assert.equal(h.navigations.length, 1);
});

test('same candidate cannot retain a delayed quote or cost approval from the previous plan/task', async () => {
  const h = harness(); h.render(); const a = h.button('Run independent QA').props.onClick();
  h.render({planId: 'plan_b', taskId: 'task_b'});
  h.requests[0].resolve({ok: true, data: h.quote}); await a; await h.flush();
  assert.equal(h.button('Start reviewed QA'), null);
  assert.equal(h.button('Run independent QA').props.disabled, false);
  h.button('Run independent QA').props.onClick();
  assert.equal(JSON.parse(h.requests[1].init.body).plan_id, 'plan_b');
  assert.equal(JSON.parse(h.requests[1].init.body).plan_task_id, 'task_b');
  h.requests[1].resolve({ok: true, data: h.quote}); await h.flush();
  assert.equal(h.requests.length, 3);
});
