const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

class HTMLInputElement {
  constructor(value = '', type = 'text') { this.value = value; this.type = type; this.checked = this.defaultChecked = false; this.defaultValue = ''; }
  closest() { return null; }
}

function harness(store = new Map(), storageFails = false) {
  let states = [false], slot = 0, mounted = false, cleanup, reloads = 0, now = 0, timerId = 0;
  const requests = [], timers = new Map(), intervals = new Map(), listeners = new Map();
  const deadlines = new Map(), fields = [], scrollers = [];
  const storage = {
    getItem(key) { if (storageFails) throw new Error('storage unavailable'); return store.get(key) ?? null; },
    setItem(key, value) { if (storageFails) throw new Error('storage unavailable'); store.set(key, value); },
    removeItem(key) { store.delete(key); },
  };
  const window = {
    sessionStorage: storage,
    location: { reload() { reloads++; } },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, fn); deadlines.set(id, now + ms); return id; },
    clearTimeout(id) { timers.delete(id); deadlines.delete(id); },
    setInterval(fn, ms) { const id = intervals.size + 1; intervals.set(id, { fn, ms }); return id; },
    clearInterval(id) { intervals.delete(id); },
    addEventListener(type, fn) { listeners.set(`window:${type}`, fn); },
    removeEventListener(type) { listeners.delete(`window:${type}`); },
  };
  const document = { hidden: false, blocked: false,
    querySelector() { return this.blocked ? {} : null; },
    querySelectorAll(selector) { return selector === '[data-console-scroll]' ? scrollers : fields; },
    addEventListener(type, fn) { listeners.set(`document:${type}`, fn); },
    removeEventListener(type) { listeners.delete(`document:${type}`); },
  };
  const source = readFileSync(join(__dirname, 'ConsoleBuildUpdate.tsx'), 'utf8');
  const output = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const exports = {};
  const reloadRef = { current: () => {} };
  vm.runInNewContext(output, { exports, window, document, AbortController, HTMLInputElement,
    Date: { now: () => now },
    process: { env: { NEXT_PUBLIC_CONSOLE_BUILD_ID: 'build-a' } },
    fetch: (url, options) => new Promise((resolve, reject) => {
      requests.push({ url, options, resolve, reject });
      options.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
    }),
    require: id => id === 'react' ? {
      useState: () => { const index = slot++ % states.length; return [states[index], value => { states[index] = value; }]; },
      useRef: () => reloadRef,
      useEffect: fn => { if (!mounted) { mounted = true; cleanup = fn(); } },
    } : { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
  });
  function advance(ms) {
    now += ms;
    for (const [id, deadline] of [...deadlines]) if (deadline <= now && timers.has(id)) {
      const fn = timers.get(id); window.clearTimeout(id); fn();
    }
  }
  return { requests, document, timers, intervals, listeners, fields, scrollers, store, storage, HTMLInputElement,
    advance,
    activity: () => listeners.get('document:input')?.(),
    retry: () => [...intervals.values()].find(timer => timer.ms === 30_000)?.fn(),
    readScroll: scope => exports.readReloadScroll(scope),
    render: () => { slot = 0; return exports.ConsoleBuildUpdate(); },
    focus: () => listeners.get('window:focus')?.(),
    visible: () => listeners.get('document:visibilitychange')?.(),
    unmount: () => cleanup?.(),
    get reloads() { return reloads; },
  };
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const reply = data => ({ ok: true, json: async () => data });

test('matching build stays quiet; a changed build offers reload without doing it', async () => {
  const h = harness(); assert.equal(h.render(), null);
  h.requests[0].resolve(reply({ ok: true, buildId: 'build-a' })); await settle();
  assert.equal(h.render(), null);
  h.focus(); h.requests[1].resolve(reply({ ok: true, buildId: 'build-b' })); await settle();
  const card = h.render(); assert.equal(card.props.role, 'status'); assert.equal(h.reloads, 0);
  card.props.children[1].props.onClick(); assert.equal(h.reloads, 1);
  assert.equal(h.requests[0].url, '/api/health'); assert.equal(h.requests[0].options.cache, 'no-store');
});

test('older, malformed, unhealthy and offline responses leave work uninterrupted', async () => {
  const h = harness(); h.render();
  for (const data of [{ ok: true }, { ok: true, buildId: '<script>' }, { ok: false, buildId: 'build-b' }]) {
    h.requests.at(-1).resolve(reply(data)); await settle(); assert.equal(h.render(), null); h.focus();
  }
  h.requests.at(-1).reject(new Error('offline')); await settle();
  assert.equal(h.render(), null); assert.equal(h.reloads, 0);
});

test('hidden windows make no request and repeated focus does not duplicate an active check', async () => {
  const h = harness(); h.document.hidden = true; h.render(); h.focus();
  assert.equal(h.requests.length, 0);
  h.document.hidden = false; h.visible(); h.focus(); h.visible();
  assert.equal(h.requests.length, 1);
  h.requests[0].resolve(reply({ ok: true, buildId: 'build-b' })); await settle();
  assert.ok(h.render()); assert.equal(h.reloads, 0);
});

test('unmount aborts the check and retires a body response already in flight', async () => {
  const h = harness(); h.render(); let body;
  h.requests[0].resolve({ ok: true, json: () => new Promise(resolve => { body = resolve; }) }); await settle();
  h.unmount(); assert.equal(h.requests[0].options.signal.aborted, true); assert.equal(h.listeners.size, 0);
  body({ ok: true, buildId: 'build-b' }); await settle();
  assert.equal(h.render(), null); assert.equal(h.reloads, 0);
});

test('timed-out checks release the request so a later focus can retry', async () => {
  const h = harness(); h.render(); [...h.timers.values()].forEach(fn => fn()); await settle();
  assert.equal(h.requests[0].options.signal.aborted, true); assert.equal(h.timers.size, 0);
  h.focus(); assert.equal(h.requests.length, 2);
  h.requests[1].resolve(reply({ ok: true, buildId: 'build-b' })); await settle();
  assert.ok(h.render()); assert.equal(h.reloads, 0);
});

test('unsafe work keeps the reload card visible even when Reload is clicked', async () => {
  const h = harness(); h.render();
  h.requests[0].resolve(reply({ ok: true, buildId: 'build-b' })); await settle();
  const card = h.render(); assert.equal(card.props.style.left, 16);
  h.document.blocked = true;
  card.props.children[1].props.onClick();
  assert.ok(h.render()); assert.equal(h.reloads, 0);
});

test('a window that stays focused still learns about a new build on the periodic check', async () => {
  const h = harness(); h.render();
  h.requests[0].resolve(reply({ ok: true, buildId: 'build-a' })); await settle();
  const [poll] = [...h.intervals.values()];
  assert.ok(poll && poll.ms >= 60_000);
  poll.fn(); h.requests[1].resolve(reply({ ok: true, buildId: 'build-b' })); await settle();
  assert.equal(h.render().props.role, 'status'); assert.equal(h.reloads, 0);
  h.unmount(); assert.equal(h.intervals.size, 0);
});

test('autoreload protects work, preserves chat scroll and guards each version across unrelated audiences', async () => {
  function attribute(file, name) {
    const tree = ts.createSourceFile(file, readFileSync(join(__dirname, file), 'utf8'), ts.ScriptTarget.Latest, true);
    let expression;
    function visit(node) {
      if (ts.isJsxAttribute(node) && node.name.getText(tree) === name) expression = node.initializer.expression.getText(tree);
      ts.forEachChild(node, visit);
    }
    visit(tree); assert.ok(expression, name); return expression;
  }
  const blocked = attribute('../chat/ChatDock.tsx', 'data-console-reload-blocked');
  const entryBlocked = attribute('../../app/new/page.tsx', 'data-console-reload-blocked');
  const voiceBlocked = attribute('../chat/VoiceMic.tsx', 'data-console-reload-blocked');
  const dictationBlocked = attribute('../chat/DictateButton.tsx', 'data-console-reload-blocked');
  const scrollScope = attribute('../chat/ChatDock.tsx', 'data-console-scroll');
  const source = readFileSync(join(__dirname, '../chat/ChatDock.tsx'), 'utf8');
  const tree = ts.createSourceFile('ChatDock.tsx', source, ts.ScriptTarget.Latest, true);
  let restore;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useLayoutEffect'
      && node.arguments[0].getText(tree).includes('historyScrollPosition')) restore = node.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(restore);
  const restoreJs = ts.transpileModule(`globalThis.restore = ${restore};`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  const detect = async (h, buildId = 'build-b') => {
    h.render(); h.requests.at(-1).resolve(reply({ ok: true, buildId })); await settle();
  };
  for (const [conversation, text] of [
    ['dental-team', 'Coordinate appointment reminders for dental practices'],
    ['birdwatchers', 'Share weekend bird sightings with neighbors'],
  ]) {
    const h = harness(), scope = `actor:chat:${conversation}`;
    const scroller = {scrollTop: 175, dataset: {consoleScroll: scope, consoleScrollCount: '72', consoleScrollLoaded: '96'}};
    h.scrollers.push(scroller);
    await detect(h); assert.equal(h.reloads, 0);
    h.advance(19_999); assert.equal(h.reloads, 0);
    h.activity(); h.advance(19_999); assert.equal(h.reloads, 0);
    h.advance(1); assert.equal(h.reloads, 1);
    assert.equal(h.store.get('console.reloaded:build-b'), '1');
    assert.deepEqual(JSON.parse(JSON.stringify(h.readScroll(scope))), {top: 175, count: 72, loaded: 96});
    h.retry(); h.document.hidden = true; h.visible(); assert.equal(h.reloads, 1);
    h.unmount(); assert.equal(h.listeners.size, 0); assert.equal(h.timers.size, 0);

    const state = {visible: true, listRef: {current: {scrollTop: 0, scrollHeight: 1200, clientHeight: 400}},
      historyAnchor: {current: null}, firstVisibleId: {current: undefined}, visibleMessages: [{id: conversation}], historyReady: false, historyLoading: false, visibleCount: 24,
      hasEarlier: true, messages: Array.from({length: 24}, () => ({role: 'assistant'})),
      followLatest: {current: true}, prevCount: {current: 0}, lastMessageId: {current: undefined}, scope: {current: scope},
      readReloadScroll: h.readScroll, setVisibleCount: value => {state.visibleCount = value;},
      isNearBottom: el => el.scrollHeight - el.scrollTop - el.clientHeight < 80,
      window: {sessionStorage: h.storage}, onLoadEarlier: () => earlier++,
    };
    let earlier = 0;
    vm.runInNewContext(restoreJs, state);
    state.restore(); assert.equal(state.listRef.current.scrollTop, 0); assert.equal(earlier, 0);
    state.historyReady = true; state.restore(); assert.equal(state.visibleCount, 72); assert.equal(earlier, 0);
    state.restore(); assert.equal(earlier, 1);
    state.historyLoading = true; state.restore(); assert.equal(earlier, 1);
    state.messages = Array.from({length: 96}, (_, index) => ({id: `saved-${index}`, role: 'assistant'}));
    state.restore(); assert.equal(state.listRef.current.scrollTop, 175);
    assert.equal(state.followLatest.current, false); assert.equal(state.prevCount.current, 96); assert.equal(state.lastMessageId.current, 'saved-95');
    assert.equal(h.readScroll(scope), null);
    assert.equal(vm.runInNewContext(scrollScope, state), scope);

    const loop = harness(h.store); await detect(loop); loop.advance(30_000); loop.retry();
    loop.render().props.children[1].props.onClick(); assert.equal(loop.reloads, 0);
    loop.focus(); await detect(loop, 'build-c'); assert.equal(loop.reloads, 1);
    const back = harness(h.store); await detect(back); back.advance(30_000); assert.equal(back.reloads, 0);
    const hidden = harness(); await detect(hidden); hidden.document.hidden = true; hidden.visible(); assert.equal(hidden.reloads, 1);

    const base = {composer: {draft: ''}, sending: false, streaming: undefined, waitingCount: 0,
      pendingActions: new Set(), attachments: {uploading: false, files: []},
      messages: [{id: conversation}], clicked: new Set(), choiceDrafts: {}};
    for (const change of [
      {composer: {draft: text}}, {sending: true}, {streaming: {text: ''}}, {waitingCount: 1},
      {pendingActions: new Set(['confirmation'])}, {attachments: {uploading: true, files: []}},
      {attachments: {uploading: false, files: [{name: conversation}]}}, {choiceDrafts: {[conversation]: [text]}},
    ]) {
      const busy = harness(); await detect(busy);
      busy.document.blocked = vm.runInNewContext(blocked, {...base, ...change});
      assert.equal(busy.document.blocked, true);
      busy.document.hidden = true; busy.visible(); busy.advance(30_000); busy.retry();
      busy.render().props.children[1].props.onClick(); assert.equal(busy.reloads, 0);
      busy.document.blocked = vm.runInNewContext(blocked, base);
      busy.retry(); assert.equal(busy.reloads, 1);
    }
    const entry = {busy: false, draft: '', attachments: {uploading: false, files: []}, selectedChoices: []};
    for (const change of [{busy: true}, {draft: text}, {attachments: {uploading: true, files: []}}, {selectedChoices: [text]}]) {
      assert.equal(vm.runInNewContext(entryBlocked, {...entry, ...change}), true);
    }
    assert.equal(vm.runInNewContext(voiceBlocked, {live: true, v: {summaryPending: false}}), true);
    assert.equal(vm.runInNewContext(voiceBlocked, {live: false, v: {summaryPending: true}}), true);
    assert.equal(vm.runInNewContext(voiceBlocked, {live: false, v: {summaryPending: false}}), false);
    assert.equal(vm.runInNewContext(dictationBlocked, {listening: true}), true);
    assert.equal(vm.runInNewContext(dictationBlocked, {listening: false}), false);
    for (const field of [new h.HTMLInputElement(text), {value: text, closest: () => null},
      {textContent: text, closest: () => null}, Object.assign(new h.HTMLInputElement('', 'checkbox'), {checked: true}),
      new h.HTMLInputElement('receipt.csv', 'file')]) {
      const form = harness(); form.fields.push(field); await detect(form);
      form.advance(30_000); form.document.hidden = true; form.visible(); assert.equal(form.reloads, 0);
      form.fields.length = 0; form.retry(); assert.equal(form.reloads, 1);
    }
    const confirmation = harness(); await detect(confirmation); confirmation.document.blocked = true;
    confirmation.advance(30_000); confirmation.retry(); assert.equal(confirmation.reloads, 0);
    confirmation.document.blocked = false; confirmation.retry(); assert.equal(confirmation.reloads, 1);
    const noStorage = harness(new Map(), true); await detect(noStorage); noStorage.advance(30_000);
    noStorage.retry(); assert.equal(noStorage.reloads, 0); assert.ok(noStorage.render());
    h.store.set(`console.reload.scroll:${scope}`, '{invalid'); assert.equal(h.readScroll(scope), null);
  }
});
