const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mount } = require('../../src-tauri/splashscreen/connection.js');
function fixture(invoke = async () => ({ connected: false, code: 'service_unavailable', http_status: 503 })) {
  let clock = 0, id = 0, calls = 0;
  const events = {}, queue = new Map();
  const button = { disabled: false, addEventListener: (name, fn) => { events[name] = fn; } };
  const document = { hidden: false, addEventListener: (name, fn) => { events[name] = fn; } };
  const status = { textContent: '' };
  const controller = mount({ button, document, status,
    invoke: (...args) => { calls++; return invoke(...args); },
    schedule: (fn, delay) => { queue.set(++id, { fn, at: clock + delay }); return id; },
    cancel: (key) => queue.delete(key),
  });
  async function step() {
    const item = [...queue].sort((a,b) => a[1].at - b[1].at)[0];
    if (!item) return false;
    queue.delete(item[0]); clock = item[1].at; item[1].fn();
    await Promise.resolve(); await Promise.resolve(); return true;
  }
  return { button, document, status, controller, events, step, calls: () => calls, queue };
}
test('three failed automatic checks stop, show HTTP reason, and preserve manual recovery', async () => {
  const f = fixture();
  for (let i = 0; i < 10 && await f.step(); i++);
  assert.equal(f.calls(), 3); assert.equal(f.queue.size, 0);
  assert.match(f.status.textContent, /HTTP 503.*paused after three attempts/);
  assert.equal(f.button.disabled, false);
  f.events.click(); await Promise.resolve();
  assert.equal(f.calls(), 4);
  await f.step(); assert.equal(f.queue.size, 0);
});
test('double clicks and timer cannot overlap a pending check', async () => {
  let done;
  const f = fixture(() => new Promise(resolve => { done = resolve; }));
  f.events.click(); f.events.click();
  assert.equal(f.calls(), 1); assert.equal(f.queue.size, 0);
  done({ connected: false, code: 'timeout' }); await Promise.resolve();
  f.events.click(); assert.equal(f.calls(), 1);
  assert.match(f.status.textContent, /eight seconds/);
  f.controller.dispose(); assert.equal(f.queue.size, 0);
});
test('hidden screen does no background work; visibility resumes only remaining budget', async () => {
  const f = fixture();
  f.document.hidden = true; f.events.visibilitychange();
  assert.equal(await f.step(), false); assert.equal(f.calls(), 0);
  f.document.hidden = false; f.events.visibilitychange(); await f.step();
  assert.equal(f.calls(), 1); f.controller.dispose();
});
test('successful recovery and native IPC failure both stop automatic polling', async () => {
  for (const invoke of [async () => ({ connected: true }), async () => { throw Error('native unavailable'); }]) {
    const f = fixture(invoke); await f.step();
    assert.equal(f.calls(), 1); assert.equal(f.queue.size, 0);
    f.events.click(); assert.equal(f.calls(), 1);
  }
});
test('late response after page is disposed cannot reschedule or change status', async () => {
  let done;
  const f = fixture(() => new Promise(resolve => { done = resolve; }));
  f.events.click(); f.controller.dispose();
  const before = f.status.textContent;
  done({ connected: false }); await Promise.resolve();
  assert.equal(f.queue.size, 0); assert.equal(f.status.textContent, before);
});
