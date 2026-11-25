const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const jsx = (type, props) => typeof type === 'function' ? type(props) : { type, props: props || {} };
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : !tree || typeof tree !== 'object' ? [] : [tree, ...nodes(tree.props?.children)];
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness(domain) {
  const slots = [], effects = [], calls = [], exports = {}, listeners = new Map();
  let cursor = 0, subject = `founder_${domain}`, epoch = 1, flag = true, succeed = true, pending = null;
  const props = { ventureId: `venture_${domain}`, workspaceId: `workspace_${domain}` };
  let saved = { id: `routine_${domain}`, title: `Saved ${domain} summary`, state: 'submitted', timezone: 'UTC',
    quiet_hours: { start: '22:00', end: '08:00' }, next_fire_at: '2026-11-06T09:00:00Z' };
  const react = {
    useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }]; },
    useRef(initial) { const index = cursor++; return slots[index] ||= { current: initial }; },
    useEffect(callback, deps) { const index = cursor++, prior = slots[index];
      if (!prior || deps.some((value, offset) => value !== prior.deps[offset])) {
        prior?.cleanup?.(); slots[index] = { deps }; effects.push(() => { slots[index].cleanup = callback(); });
      } },
  };
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/RoutinesSettings.tsx`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, { exports, window: { addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: name => listeners.delete(name) },
    FormData: class { constructor(values) { this.values = values; } get(name) { return this.values[name]; } },
    require: name => name === 'react' ? react : name === 'react/jsx-runtime' ? { jsx, jsxs: jsx }
      : name.endsWith('/ErrorNotice') ? { ErrorNotice: ({ error }) => error ? { type: 'p', props: { role: 'alert', children: error } } : null } : {
      verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
      api: async (url, options) => {
        calls.push({ url, options });
        if (url === '/api/ui-config') return { ok: true, data: flag ? { routines_v1: true } : {} };
        if (!options) return { ok: succeed, data: { items: saved ? [saved] : [] } };
        if (pending) return new Promise(resolve => { pending.resolve = resolve; });
        if (!succeed) return { ok: false };
        if (options.method === 'DELETE') saved = null;
        else saved = { ...saved, ...(options.method === 'POST' ? { state: 'paused', next_fire_at: null } : JSON.parse(options.body)) };
        return { ok: true, data: saved };
      },
    } });
  const render = () => { cursor = 0; const tree = exports.RoutinesSettings(props); while (effects.length) effects.shift()(); return tree; };
  return { render, calls, props, listeners,
    setFlag(value) { flag = value; }, setSuccess(value) { succeed = value; },
    defer() { pending = {}; return pending; },
    switchFounder() { subject = `other_${domain}`; epoch++; },
  };
}

test('17: you can reread a routine after an uncertain change without replaying it', async () => {
  for (const domain of ['warehouse_operators', 'home_cooks']) {
    const app = harness(domain); app.render(); await tick(); app.setSuccess(false);
    nodes(app.render()).find(node => node.props['aria-label'] === `Pause Saved ${domain} summary`).props.onClick(); await tick();
    const writes = app.calls.filter(call => call.options).length;
    app.setSuccess(true); nodes(app.render()).find(node => node.props.children === 'Refresh saved status').props.onClick();
    app.render(); await tick(); assert.equal(app.calls.filter(call => call.options).length, writes);
    assert.equal(nodes(app.render()).some(node => node.props.role === 'alert'), false);
    assert.equal(nodes(app.render()).filter(node => node.type === 'li').length, 1);
  }
});

test('routine settings save timezone and quiet hours and confirm pause/delete for unrelated audiences', async () => {
  for (const domain of ['warehouse_operators', 'home_cooks']) {
    const env = harness(domain);
    env.render(); await tick();
    assert.equal(nodes(env.render()).filter(node => node.type === 'li').length, 1);
    assert.equal(nodes(env.render()).filter(node => node.props.type === 'time').length, 2);
    const form = nodes(env.render()).find(node => node.type === 'form');
    form.props.onSubmit({ preventDefault() {}, currentTarget: { timezone: 'Asia/Kolkata', start: '21:30', end: '07:15' } });
    await tick();
    assert.deepEqual(JSON.parse(env.calls.at(-1).options.body), { timezone: 'Asia/Kolkata', quiet_hours: { start: '21:30', end: '07:15' } });
    assert.equal(nodes(env.render()).find(node => node.props.name === 'timezone').props.defaultValue, 'Asia/Kolkata');
    const pause = () => nodes(env.render()).find(node => node.props['aria-label'] === `Pause Saved ${domain} summary`);
    env.setSuccess(false); pause().props.onClick(); await tick();
    assert.equal(pause().props.disabled, false);
    assert.ok(nodes(env.render()).some(node => node.props.role === 'alert'));
    env.setSuccess(true); pause().props.onClick(); await tick();
    assert.equal(pause().props.disabled, true);
    assert.ok(env.calls.at(-1).url.endsWith('/pause'));
    nodes(env.render()).find(node => node.props['aria-label'] === `Delete Saved ${domain} summary`).props.onClick(); await tick();
    assert.equal(env.calls.at(-1).options.method, 'DELETE');
    assert.equal(nodes(env.render()).filter(node => node.type === 'li').length, 0);
  }
  assert.equal((readFileSync(`${__dirname}/VentureSettings.tsx`, 'utf8').match(/<RoutinesSettings workspaceId=\{workspaceId\} ventureId=\{ventureId\}/g) || []).length, 2);
});

test('flag-off is invisible and switched founders cannot receive stale routine responses', async () => {
  for (const domain of ['freight_teams', 'fitness_members']) {
    const env = harness(domain);
    env.setFlag(false); env.render(); await tick();
    assert.equal(env.render(), null);
    assert.equal(env.calls.filter(call => call.url.includes('/routines')).length, 0);
    env.setFlag(true); env.listeners.get('supymem-session-changed')(); await tick();
    assert.equal(nodes(env.render()).filter(node => node.type === 'li').length, 1);
    const pending = env.defer();
    nodes(env.render()).find(node => node.props['aria-label'] === `Delete Saved ${domain} summary`).props.onClick();
    await tick(); env.setFlag(false); env.switchFounder();
    assert.equal(env.render(), null);
    pending.resolve({ ok: true }); await tick();
    assert.equal(env.render(), null);
    assert.equal(nodes(env.render()).filter(node => node.type === 'li').length, 0);
  }
});

test('a retained routine handler cannot write after an account switch before rerender', async () => {
  for (const domain of ['retail_teams', 'garden_clubs']) {
    const env = harness(domain);
    env.render(); await tick();
    const retained = nodes(env.render()).find(node => node.props['aria-label'] === `Pause Saved ${domain} summary`).props.onClick;
    const before = env.calls.length;
    env.switchFounder(); retained(); await tick();
    assert.equal(env.calls.length, before);
    assert.equal(env.render(), null);
  }
});
