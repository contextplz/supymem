const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { pathToFileURL } = require('node:url');

async function page(before = [], options = {}) {
  const dir = path.resolve(__dirname, '../../src-tauri/splashscreen/agent-cursor');
  const { glide } = await import(pathToFileURL(path.join(dir, 'path.mjs')).href);
  const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
  const source = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
    .replace(/import \{ glide \} from "\.\/path\.mjs";/, '');
  const elements = {};
  for (const id of ['cur', 'body', 'label', 'ripple', 'activityText']) {
    const classes = new Set();
    elements[id] = { style: {}, textContent: '', offsetWidth: 0, classList: {
      add: (...values) => values.forEach(value => classes.add(value)),
      remove: (...values) => values.forEach(value => classes.delete(value)),
      toggle: (value, enabled) => enabled ? classes.add(value) : classes.delete(value),
      contains: value => classes.has(value),
    }};
  }
  let frame, clock = 0, preferenceChanged, requestedFrames = 0;
  let queuedFrames = [], receipts = [];
  const animation = {playState: 'running', get currentTime() { return clock; }};
  elements.cur.getAnimations = () => options.animation === false ? [] : [animation];
  const context = { glide, window: { matchMedia: () => ({ matches: options.reducedMotion === true,
    addEventListener: (_, callback) => { preferenceChanged = callback; } }) }, innerWidth: 1000, innerHeight: 1000,
    document: { getElementById: id => elements[id] },
    performance: { now: () => clock }, Math,
    requestAnimationFrame: callback => { requestedFrames++; frame = callback; if (options.receipts) queuedFrames.push(callback); },
    getComputedStyle: element => ({opacity: element.classList.contains(element === elements.cur ? 'on' : 'show') ? '1' : '0'}),
    setTimeout: () => 1, clearTimeout: () => {},
  };
  if (options.receipts) context.window[options.misspelledBridge ? "__TAURI_INTERNALS" : "__TAURI_INTERNALS__"] = {invoke: (command, args) => {
    receipts.push({command, args}); return Promise.resolve(true);
  }};
  context.window.__agentCursorDocumentGeneration = 1;
  for (const script of before) vm.runInNewContext(script, context);
  vm.runInNewContext(source, context, { filename: 'agent-cursor/index.html' });
  return { elements, event: context.window.__agentCursor.event,
    run: script => vm.runInNewContext(script, context),
    tick: value => { clock = value; frame(value); }, frames: () => requestedFrames,
    committedFrame: value => { clock = value; const pending = queuedFrames; queuedFrames = []; pending.forEach(callback => callback(value)); },
    receipts,
    reduced: value => preferenceChanged({matches: value}) };
}

function delivered(sequence, kind = 'read', activity = 'Working on code') {
  return {kind, label:activity, receipt:{generation:3, document_generation:1, sequence, owner:'a'.repeat(32), kind, activity}};
}

test('closed renderer ACK follows two animation frames and committed DOM without private fields', async () => {
  const p = await page([], {receipts:true});
  p.event(delivered(1));
  assert.equal(p.receipts.length, 0);
  p.committedFrame(16); assert.equal(p.receipts.length, 0);
  p.committedFrame(32); assert.equal(p.receipts.length, 1);
  assert.equal(p.receipts[0].command, 'agent_cursor_rendered');
  assert.deepEqual(JSON.parse(JSON.stringify(p.receipts[0].args.receipt)), {
    generation:3, document_generation:1, sequence:1, owner:'a'.repeat(32), kind:'read',activity:'Working on code',
    content_visible:true, animation_frames:2, animation_running:true, reduced_motion:false,
  });
});

test('a misspelled IPC global cannot manufacture a native renderer acknowledgement', async () => {
  const p = await page([], {receipts:true, misspelledBridge:true});
  p.event(delivered(1)); p.committedFrame(16); p.committedFrame(32);
  assert.equal(p.receipts.length, 0);
  assert.equal(p.elements.activityText.textContent, 'Working on code');
});

test('document replacement retires a pending ACK even with the same visual owner and sequence', async () => {
  const p = await page([], {receipts:true}); p.event(delivered(1));p.committedFrame(16);
  p.run('window.__agentCursorDocumentGeneration=2'); p.committedFrame(32);
  assert.equal(p.receipts.length,0);
  p.event(delivered(1));p.committedFrame(48);p.committedFrame(64);assert.equal(p.receipts.length,0);
});

test('replacement and end retire queued renderer receipts before their frames commit', async () => {
  const p = await page([], {receipts:true});
  p.event(delivered(1)); p.committedFrame(16);
  p.event(delivered(2, 'end', '')); p.committedFrame(32); p.committedFrame(48);
  assert.equal(p.receipts.length, 1);
  assert.equal(p.receipts[0].args.receipt.sequence, 2);
  assert.equal(p.receipts[0].args.receipt.content_visible, false);
  assert.equal(p.elements.cur.classList.contains('on'), false);
});

test('renderer receipt distinguishes reduced motion from a running animation', async () => {
  const p = await page([], {receipts:true, reducedMotion:true});
  p.event(delivered(1)); p.committedFrame(16); p.committedFrame(32);
  assert.equal(p.receipts[0].args.receipt.reduced_motion, true);
  assert.equal(p.receipts[0].args.receipt.animation_running, false);
});

test('wrong label, absent delivery identity, and no IPC never produce an ACK', async () => {
  const p = await page([], {receipts:true});
  p.event({kind:'read', label:'Private task',receipt:delivered(1).receipt});
  p.committedFrame(16); p.committedFrame(32); assert.equal(p.receipts.length,0);
  p.event({kind:'read',label:'Working on code'});p.committedFrame(48);p.committedFrame(64);
  assert.equal(p.receipts.length,0);
  const plain = await page(); plain.event(delivered(1));
  assert.equal(plain.elements.activityText.textContent,'Working on code');
});

for (const fixedLabel of ['Working on code', 'Working in browser']) {
  test(`noncoordinate worker label ${fixedLabel} renders without a fictitious input`, async () => {
    const p = await page();
    p.event({kind:'begin'});
    p.event({kind:'read',label:fixedLabel});
    assert.equal(p.elements.activityText.textContent, fixedLabel);
    assert.equal(p.elements.cur.classList.contains('on'), true);
    assert.equal(p.elements.ripple.classList.contains('go'), false);
    assert.equal(p.elements.body.classList.contains('press'), false);
    p.event({kind:'end'});
    assert.equal(p.elements.cur.classList.contains('on'), false);
  });
}

test('worker labels do not admit arbitrary task text into the renderer', async () => {
  const p = await page();
  p.event({kind:'read',label:'Private coding request'});
  assert.equal(p.elements.activityText.textContent, 'Reading screen');
  assert.equal(p.elements.label.textContent.includes('Private'), false);
});

test('founder pause parks Waiting and same-lease resume uses a fresh phase without input', async () => {
  const p = await page();
  p.event({kind:'begin'});
  p.event({kind:'read',label:'Reading page'});
  p.event({kind:'read',label:'Waiting'});
  assert.equal(p.elements.activityText.textContent, 'Waiting');
  p.event({kind:'read',label:'Reading page'});
  assert.equal(p.elements.activityText.textContent, 'Reading page');
  assert.equal(p.elements.ripple.classList.contains('go'), false);
  assert.equal(p.elements.body.classList.contains('press'), false);
  p.event({kind:'end'});
  assert.equal(p.elements.cur.classList.contains('on'), false);
});

for (const terminal of ['end', 'done', 'fail']) {
  for (const action of ['click', 'type']) {
    test(`${terminal} cancels pending ${action} arrival on actual page`, async () => {
      const p = await page();
      p.event({ kind: 'move', x: 10, y: 10 });
      p.event({ kind: action, x: 800, y: 700 });
      p.event({ kind: terminal });
      const position = p.elements.cur.style.transform;
      p.tick(10000);
      p.tick(10100);
      assert.equal(p.elements.cur.style.transform, position, 'terminal event cancels queued movement/bobbing');
      assert.equal(p.elements.ripple.classList.contains('go'), false, 'cancelled click never arrives');
      assert.equal(p.elements.body.classList.contains('press'), false);
      if (terminal === 'end') assert.equal(p.elements.cur.classList.contains('on'), false);
      if (terminal === 'fail') assert.equal(p.elements.cur.classList.contains('fail'), true);
    });
  }
}

for (const action of ['click', 'type']) {
  test(`new task ${action} still arrives after cancelled predecessor`, async () => {
    const p = await page();
    p.event({ kind: 'move', x: 10, y: 10 });
    p.event({ kind: action, x: 800, y: 700 });
    p.event({ kind: 'end' });
    p.event({ kind: 'begin' });
    p.event({ kind: action, x: 200, y: 300 });
    p.tick(10000);
    assert.equal(p.elements.cur.style.transform, 'translate(200px, 300px)');
    assert.equal(p.elements.cur.classList.contains('on'), true);
    if (action === 'click') assert.equal(p.elements.ripple.classList.contains('go'), true);
    if (action === 'type') {
      p.tick(10100);
      assert.notEqual(p.elements.cur.style.transform, 'translate(200px, 300px)');
    }
  });
}

for (const action of ['click', 'type']) {
  test(`begin independently cancels abandoned ${action} arrival`, async () => {
    const p = await page();
    p.event({ kind: 'move', x: 10, y: 10 });
    p.event({ kind: action, x: 800, y: 700 });
    // Prior process may have crashed without delivering end.
    p.event({ kind: 'begin' });
    const position = p.elements.cur.style.transform;
    p.tick(10000);
    p.tick(10100);
    assert.equal(p.elements.cur.style.transform, position);
    assert.equal(p.elements.ripple.classList.contains('go'), false);
    assert.equal(p.elements.body.classList.contains('press'), false);
  });
}


test('early native guard loses init and event; loaded delivery preserves exact monitor origin', async () => {
  const init = 'window.__agentCursor&&window.__agentCursor.init(200,100,1000,1000)';
  const click = 'window.__agentCursor&&window.__agentCursor.event({kind:"click",x:250,y:170})';
  // Exact former native eval expressions before the module exists: both drop.
  const early = await page([init, click]);
  assert.equal(early.elements.cur.classList.contains('on'), false);
  early.run(click);
  assert.equal(early.elements.cur.style.transform, 'translate(250px, 170px)',
    'lost origin incorrectly uses primary-display default');
  // Readiness releases origin before the same queued global-coordinate event.
  const ready = await page();
  ready.run(init);
  ready.run(click);
  assert.equal(ready.elements.cur.classList.contains('on'), true);
  assert.equal(ready.elements.cur.style.transform, 'translate(50px, 70px)');
});

test('queued ended execution stays invisible after renderer becomes ready', async () => {
  const p = await page();
  p.run('window.__agentCursor.init(200,100,1000,1000)');
  for (const event of [{kind:'begin'}, {kind:'click',x:250,y:170}, {kind:'end'}]) p.event(event);
  p.tick(10000);
  assert.equal(p.elements.cur.classList.contains('on'), false);
  assert.equal(p.elements.ripple.classList.contains('go'), false);
});


test('module-ready handshake replays early origin and events including terminal', async () => {
  const init = 'window.__agentCursorInit=[200,100,1000,1000]';
  const event = 'window.__agentCursorPending=[{kind:"begin"},{kind:"click",x:250,y:170}]';
  const p = await page([event, init]);
  assert.equal(p.elements.cur.style.transform, 'translate(50px, 70px)');
  assert.equal(p.elements.cur.classList.contains('on'), true);
  const ended = await page([event.replace('}]', '},{kind:"end"}]'), init]);
  ended.tick(10000);
  assert.equal(ended.elements.cur.classList.contains('on'), false);
  assert.equal(ended.elements.ripple.classList.contains('go'), false);
});


test('same Webview document reload reapplies nonzero origin without stale hints', async () => {
  const documentInit = 'window.__agentCursorInit=[200,100,1000,1000]';
  const original = await page([documentInit]);
  original.event({kind:'begin'});
  original.event({kind:'click',x:250,y:170});
  original.event({kind:'end'});
  // A fresh document receives initialization_script, not consumed old hints.
  const reloaded = await page([documentInit]);
  assert.equal(reloaded.elements.cur.classList.contains('on'), false);
  reloaded.event({kind:'begin'});
  reloaded.event({kind:'click',x:260,y:180});
  assert.equal(reloaded.elements.cur.style.transform, 'translate(60px, 80px)');
});


test('coordinate-free task begins with a visible parked activity cursor on primary display', async () => {
  const primary = await page(['window.__agentCursorInit=[0,0,1000,1000]']);
  primary.event({kind:'begin'});
  assert.equal(primary.elements.cur.classList.contains('on'), true);
  assert.equal(primary.elements.cur.classList.contains('parked'), true);
  assert.equal(primary.elements.activityText.textContent, 'Starting');
  primary.event({kind:'read',label:'Reading page'});
  assert.equal(primary.elements.activityText.textContent, 'Reading page');
  assert.equal(primary.elements.label.classList.contains('show'), true);
  const secondary = await page(['window.__agentCursorInit=[1000,0,1000,1000]']);
  secondary.event({kind:'begin'});
  secondary.event({kind:'read'});
  assert.equal(secondary.elements.cur.classList.contains('on'), false);
});


test('known secondary-display target prevents a duplicate primary parked cursor', async () => {
  const primary = await page(['window.__agentCursorInit=[0,0,1000,1000]']);
  primary.event({kind:'begin'});
  primary.event({kind:'click',x:1200,y:300});
  primary.event({kind:'read',label:'Reading screen'});
  assert.equal(primary.elements.cur.classList.contains('on'), false);
  const secondary = await page(['window.__agentCursorInit=[1000,0,1000,1000]']);
  secondary.event({kind:'begin'});
  secondary.event({kind:'click',x:1200,y:300});
  secondary.event({kind:'read'});
  assert.equal(secondary.elements.cur.classList.contains('on'), true);
  assert.equal(secondary.elements.cur.style.transform, 'translate(200px, 300px)');
});


test('activity labels never render model menu paths, app names, chords or arbitrary content', async () => {
  const p = await page();
  for (const [label, expected] of [
    ['Menu customer-private-body', 'Using menu'], ['Pressing private-password', 'Using keyboard'],
    ['Opening Personal Notes App', 'Opening app'], ['Switching to Customer Document', 'Switching app'],
    ['<img src=x onerror=secret()>', 'Typing'], ['task is verified and paid', 'Typing'],
  ]) {
    p.event({kind:'type',x:50,y:50,label});
    assert.equal(p.elements.activityText.textContent, expected);
  }
  p.event({kind:'read',label:'Capturing screen'});
  assert.equal(p.elements.activityText.textContent, 'Capturing screen');
  assert.equal(p.elements.cur.classList.contains('capturing'), true);
  p.event({kind:'read',label:'Saving file…'});
  assert.equal(p.elements.activityText.textContent, 'Saving file');
});


test('reduced motion snaps exact target without glide or typing bob and remains readable', async () => {
  const p = await page([], {reducedMotion: true});
  p.event({kind:'begin'});
  p.event({kind:'type',x:200,y:300});
  assert.equal(p.elements.cur.style.transform, 'translate(200px, 300px)');
  p.tick(10000);
  p.tick(10100);
  assert.equal(p.elements.cur.style.transform, 'translate(200px, 300px)');
  assert.equal(p.elements.activityText.textContent, 'Typing');
});


test('turning reduced motion on during glide settles the current visual intent without later drift', async () => {
  const p = await page();
  p.event({kind:'move',x:10,y:10});
  p.event({kind:'type',x:800,y:700});
  p.reduced(true);
  assert.equal(p.elements.cur.style.transform, 'translate(800px, 700px)');
  p.tick(10000);
  p.tick(10100);
  assert.equal(p.elements.cur.style.transform, 'translate(800px, 700px)');
});


test('terminal hints retire animation scheduling instead of repainting invisible pages indefinitely', async () => {
  const p = await page();
  p.event({kind:'move',x:10,y:10});
  p.event({kind:'type',x:800,y:700});
  p.event({kind:'end'});
  const before = p.frames();
  p.tick(10000);
  p.tick(10100);
  assert.equal(p.frames(), before);
  assert.equal(p.elements.cur.classList.contains('on'), false);
  assert.equal(p.elements.activityText.textContent, '');
});
