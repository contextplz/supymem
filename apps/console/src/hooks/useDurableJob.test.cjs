const assert = require('node:assert/strict');
const test = require('node:test');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');

function observe(job) {
  let cursor = 0;
  const cache = new Map();
  const mocks = {
    react: {
      useState(initial) { return [cursor++ === 0 ? job : typeof initial === 'function' ? initial() : initial, () => {}]; },
      useRef: value => ({current: value}), useCallback: value => value, useEffect() {},
    },
    '@/lib/client': {}, './client.ts': {}, './apiFailure.ts': {}, './uiDiagnostics.ts': {},
  };
  function load(filename) {
    if (cache.has(filename)) return cache.get(filename).exports;
    const loaded = new Module(filename, module);
    loaded.filename = filename; loaded.paths = Module._nodeModulePaths(path.dirname(filename)); cache.set(filename, loaded);
    const native = loaded.require.bind(loaded);
    loaded.require = id => {
      if (id in mocks) return mocks[id];
      if (id === '@/lib/durableJobs') return load(path.resolve(__dirname, '../lib/durableJobs.ts'));
      if (id.startsWith('.')) return load(path.resolve(path.dirname(filename), id));
      return native(id);
    };
    loaded._compile(ts.transpileModule(readFileSync(filename, 'utf8'), {compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
    }}).outputText, filename);
    return loaded.exports;
  }
  return load(path.join(__dirname, 'useDurableJob.ts')).useDurableJob(job.id, () => {});
}

test('durable status shows scheduled and retry reasons without treating intentional waits as stalled work in two domains', () => {
  for (const audience of ['Procurement teams', 'Book club members']) {
    const now = Date.now(), old = new Date(now - 600_000).toISOString(), future = new Date(now + 600_000).toISOString();
    const job = {id: audience, kind: 'research.deep', status: 'retry_wait', scheduled_for: old, created_at: old,
      wait: {reason: 'retry_backoff', label: 'Starting soon. Waiting before trying again after a temporary problem.', until: future}};
    let status = observe(job);
    assert.equal(status.label, job.wait.label);
    assert.equal(status.delayed, false); assert.equal(status.needsRecovery, false);
    job.status = 'scheduled'; job.wait = {reason: 'scheduled', label: 'Starting soon. Your work is scheduled for later.', until: future};
    status = observe(job); assert.equal(status.label, job.wait.label); assert.equal(status.delayed, false);
    job.wait = {reason: 'queued', label: 'Starting soon. Your work is ready to try again.', until: new Date(now - 10_000).toISOString()};
    status = observe(job); assert.equal(status.label, job.wait.label); assert.equal(status.delayed, false);
    job.wait = {reason: 'usage_limit', label: 'Starting soon. Your usage limit needs time to reset.', until: future};
    status = observe(job); assert.equal(status.label, job.wait.label); assert.equal(status.needsRecovery, false);
    job.wait = {reason: 'paused', label: 'Paused. Resume this work when you are ready.', until: null};
    status = observe(job); assert.equal(status.label, job.wait.label); assert.equal(status.delayed, false);
    job.wait = {reason: 'queued', label: 'Starting soon. Your work is saved but has not started yet.', until: null};
    status = observe(job); assert.equal(status.delayed, true); assert.equal(status.needsRecovery, true);
    assert.match(status.label, /longer than expected/);
    job.scheduled_for = new Date(now).toISOString(); job.wait = undefined;
    assert.equal(observe(job).label, 'Starting soon. Your work is saved but has not started yet.');
    job.status = 'retry_wait'; assert.match(observe(job).label, /temporary problem/);
    job.status = 'leased'; job.locked_at = new Date(now).toISOString(); assert.equal(observe(job).label, 'Working');
  }
});
