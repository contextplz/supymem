const assert = require('node:assert/strict'), test = require('node:test');
const {readFileSync} = require('node:fs'), vm = require('node:vm'), ts = require('typescript');
const compile = source => ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
const scrolling = {exports: {}};
vm.runInNewContext(compile(readFileSync(__dirname + '/../../lib/chatScroll.ts', 'utf8')), scrolling);
function findEffect(source, hook, marker) {
  const tree = ts.createSourceFile('thread.tsx', source, ts.ScriptTarget.Latest, true);
  let effect;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === hook && node.arguments[0].getText(tree).includes(marker)) effect = node.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(effect);
  return compile('globalThis.update = ' + effect);
}
const dock = readFileSync(__dirname + '/ChatDock.tsx', 'utf8'), entry = readFileSync(__dirname + '/../../app/new/page.tsx', 'utf8');
const dockEffect = findEffect(dock, 'useLayoutEffect', 'scrollDecision');
const entryEffect = findEffect(entry, 'useEffect', 'scrollDecision');
const dockFocus = findEffect(dock, 'useEffect', '!resizable');
const entryFocus = findEffect(entry, 'useEffect', 'nameInput.current');

test('sending keeps keyboard focus in the composer without taking it back when Charlie replies', async () => {
  const tree = ts.createSourceFile('ChatDock.tsx', dock, ts.ScriptTarget.Latest, true);
  let submit;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'submit') submit = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  for (const domain of ['Freight scheduling', 'Family garden planning']) {
    let finish, focus = 'send';
    const context = {activeQuestion: null, computerUseActive: false, chatStopApplies: () => false,
      planSend: () => ({sendNow: true}), waiting: {current: []},
      inputRef: {current: {focus: () => { focus = 'composer'; }}},
      composer: {submit: () => new Promise(resolve => { finish = resolve; })},
      requestAnimationFrame: callback => callback()};
    const sending = vm.runInNewContext(compile(`${submit}\nsubmit(${JSON.stringify(domain)});`), context);
    assert.equal(focus, 'composer', domain);
    focus = 'sidebar'; finish(true);
    assert.equal(await sending, true);
    assert.equal(focus, 'sidebar', domain);
  }
});

test('the chat transcript is keyboard scrollable and starts with 24 of hundreds of messages', () => {
  for (const domain of ['Workshop supplies', 'Family recipes']) {
    assert.match(dock, /role="log" aria-label="Your conversation"[^>]*tabIndex=\{0\}/, domain);
    assert.match(dock, /Show earlier messages/, domain);
    const initial = Number(dock.match(/\[visibleCount, setVisibleCount\] = useState\((\d+)\)/)?.[1]);
    const slice = dock.match(/const visibleMessages = (allMessages\.slice\([^\n]+\))/)?.[1];
    const allMessages = Array.from({length: 500}, (_, index) => `${domain} ${index}`);
    const shown = vm.runInNewContext(slice, {allMessages, firstVisibleIndex: -1, visibleCount: initial});
    assert.equal(shown.length, 24);
    assert.equal(shown.at(-1), allMessages.at(-1));
  }
});

test('the saved-idea pending row stays out of your conversation across domains', () => {
  const tree = ts.createSourceFile('ChatDock.tsx', dock, ts.ScriptTarget.Latest, true);
  let expression;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'allMessages') expression = node.initializer.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(expression);
  for (const domain of ['Freight scheduling', 'Family garden planning']) {
    const pending = {id: domain, role: 'assistant', kind: 'entry_pending', body_md: 'Your idea is saved.'};
    const context = {ventureId: domain, messages: [pending], inboxBriefing: {enabled: false}, agents: {jobs: []},
      collapseAgentStatuses: messages => messages, collapseSearchProgress: messages => messages};
    assert.equal(vm.runInNewContext(expression, context).length, 0);
    context.messages = [{...pending, kind: 'idea_clarification_ready', body_md: 'Who needs this first?'}];
    const visible = vm.runInNewContext(expression, context);
    assert.equal(visible.length, 1);
    assert.equal(visible[0].body_md, 'Who needs this first?');
  }
});

test('a pending reply replaced by its answer, or a reply placed above the newest row, stays in view in both threads', () => {
  for (const code of [dockEffect]) {
    for (const domain of ['Coordinate loading bays', 'Share accessible walks']) {
      let top = 400, writes = 0, pill = false;
      const scroller = {scrollHeight: 800, clientHeight: 400, get scrollTop() {return top;}, set scrollTop(value) {top = value; writes++;}};
      const context = {visible: true, listRef: {current: scroller}, firstVisibleId: {current: undefined}, visibleMessages: [{id: 'pending'}],
        historyAnchor: {current: null}, readReloadScroll: () => null, scope: {current: domain}, prevCount: {current: 2}, lastMessageId: {current: 'pending'},
        followLatest: {current: true}, viewportAnchor: {current: null}, firstMessageId: {current: 'question'}, readScrollAnchor: () => null, restoreScrollAnchor: () => {},
        messages: [{id: 'question', role: 'user'}, {id: 'answer', role: 'assistant', text: domain}],
        isSelecting: () => false, scrollDecision: scrolling.exports.scrollDecision, streaming: null, setNewBelow: value => {pill = value;}};
      vm.runInNewContext(code, context);
      scroller.scrollHeight = 1200; context.update();
      assert.equal(top, 1200, 'the same-count replacement follows'); assert.equal(pill, false);
      context.messages = [{id: 'question', role: 'user'}, {id: 'inserted', role: 'assistant'}, {id: 'answer', role: 'assistant'}];
      scroller.scrollHeight = 1500; context.update();
      assert.equal(top, 1500, 'an anchored reply above the last row still follows');
      context.messages = [{id: 'older', role: 'user'}, ...context.messages];
      writes = 0; scroller.scrollHeight = 1800; context.update();
      assert.equal(writes, 0, 'earlier history prepended does not follow');
    }
  }
});

test('your transcript announces only new saved replies, not restored history or streamed tokens', () => {
  const source = readFileSync(__dirname + '/ChatDock.tsx', 'utf8');
  for (const domain of ['Invoice planner', 'Garden journal']) {
    assert.match(source, /role="log" aria-label="Your conversation" aria-live="off"/, domain);
    assert.match(source, /aria-relevant="additions"/, domain);
    assert.match(source, /announcedMessages\.current/, domain);
    assert.match(source, /historyReady/, domain);
    assert.match(source, /role="status"[^>]*aria-live="polite"[^>]*>\{replyAnnouncement\}/, domain);
    const announce = findEffect(source, 'useEffect', 'announcedMessages.current.scope');
    let announcement = '';
    const context = {messages: [], workKey: domain, visible: true,
      historyReady: false, historyLoading: false, streaming: null, Set,
      announcedMessages: {current: {scope: '', ids: new Set()}}, setReplyAnnouncement: value => {announcement = value;},
      parseEmployeeStamp: text => ({text})};
    const update = vm.runInNewContext(compile(`(${announce})`), context);
    update(); context.historyReady = true;
    context.messages.push({id: 'restored', role: 'assistant', body_md: domain}); update(); assert.equal(announcement, '');
    context.messages.push({id: 'reply', role: 'assistant', body_md: `You can continue with ${domain}.`}); update();
    assert.equal(announcement, `You can continue with ${domain}.`);
    context.historyLoading = true;
    context.messages.unshift({id: 'earlier', role: 'assistant', body_md: 'Earlier saved content'}); update();
    context.historyLoading = false; update(); assert.equal(announcement, '');
    context.streaming = {messageId: 'stream'};
    context.messages.push({id: 'stream', role: 'assistant', body_md: 'First token'}); update(); assert.equal(announcement, '');
    context.messages.at(-1).body_md = domain; update(); assert.equal(announcement, '');
    context.streaming = null; update(); assert.equal(announcement, domain);
  }
});

test('your light chat text retains the design with readable foreground and accent tokens', () => {
  const css = readFileSync(__dirname + '/ChatDock.module.css', 'utf8');
  const tokens = readFileSync(__dirname + '/../../styles/tokens.css', 'utf8');
  const contrast = (first, second) => {
    const luminance = hex => hex.match(/[a-f\d]{2}/gi).map(value => parseInt(value, 16) / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4).reduce((total, value, index) => total + value * [.2126, .7152, .0722][index], 0);
    const values = [luminance(first), luminance(second)].sort((left, right) => right - left);
    return (values[0] + .05) / (values[1] + .05);
  };
  for (const domain of ['Invoice planner', 'Garden journal']) {
    for (const selector of ['imName', 'msgSystem']) assert.match(css, new RegExp(`homePanel \\.${selector}\\s*\\{[^}]*color: var\\(--fg-2\\)`), domain);
    assert.match(css, /homePanel \.bubbleOwn\s*\{[^}]*background: var\(--accent-hover\)/, domain);
    assert.match(css, /homePanel \.optionsHintButton\s*\{[^}]*color: var\(--accent-hover\)/, domain);
    const foreground = /--fg-2: (#[a-f\d]{6})/.exec(tokens)[1];
    const accent = /--accent-hover: (#[a-f\d]{6})/.exec(tokens)[1];
    assert.ok(contrast(foreground, '#ffffff') >= 4.5);
    assert.ok(contrast(accent, '#e9e9eb') >= 4.5);
    assert.ok(contrast('#ffffff', accent) >= 4.5);
  }
});

test('your local white input boundaries remain visible and preserve focus rings', () => {
  const css = readFileSync(__dirname + '/../../app/settings/local/local.module.css', 'utf8');
  for (const domain of ['Invoice planner', 'Garden journal']) {
    assert.match(css, /border:1px solid #8d8194/, domain);
    assert.match(css, /focus-visible\{outline:3px solid #7844ad/, domain);
  }
});

test('the new-message pill and tray do not change the reader layout', () => {
  const css = readFileSync(__dirname + '/ChatDock.module.css', 'utf8');
  assert.match(css.match(/\.newPill\s*\{([^}]+)\}/)[1], /position:\s*absolute/);
  assert.equal(/:has\(\[aria-label="Running agents"\]\)\s+\.messages/.test(css), false);
});

test('both threads follow only new arrivals at the bottom and keep your selection and history mounted across ventures', () => {
  for (const [venture, text] of [['Cargo Ledger', 'Coordinate loading bays for freight terminals'], ['Trail Journal', 'Share accessible walks with families']]) {
    for (const code of [dockEffect, entryEffect]) {
      let top = 200, writes = 0, selecting = false, pill = false;
      const scroller = {scrollHeight: 800, clientHeight: 400, get scrollTop() {return top;}, set scrollTop(value) {top = value; writes++;}};
      const context = {
        visible: true,
        listRef: {current: scroller}, threadRef: {current: scroller},
        firstVisibleId: {current: undefined}, visibleMessages: [{id: 'first', text}],
        viewportAnchor: {current: null}, firstMessageId: {current: 'first'}, readScrollAnchor: () => null, restoreScrollAnchor: () => {},
        historyAnchor: {current: null}, readReloadScroll: () => null, scope: {current: venture},
        prevCount: {current: 1}, prevTurns: {current: 1}, lastMessageId: {current: 'first'},
        followLatest: {current: true}, following: {current: true},
        messages: [{id: 'first', role: 'assistant', text}], turns: [{id: 'first', role: 'assistant', text}],
        isSelecting: () => selecting, scrollDecision: scrolling.exports.scrollDecision,
        streaming: null, setNewBelow: value => {pill = value;},
      };
      vm.runInNewContext(code, context);
      const append = (id, role = 'assistant') => {
        context.messages = [...context.messages, {id, role, text}]; context.turns = context.messages; context.update();
      };
      context.update(); assert.equal(writes, 0, 'an unchanged poll does not scroll');
      context.messages = context.messages.map(message => ({...message, text: text + ' updated'})); context.turns = context.messages;
      context.update(); assert.equal(writes, 0, 'stream and background edits do not scroll');
      append('reply'); assert.equal(writes, 1); assert.equal(top, 800); assert.equal(pill, false);
      context.update(); assert.equal(writes, 1);
      selecting = true; append('selected-arrival'); assert.equal(writes, 1); assert.equal(pill, true);
      selecting = false; context.update(); assert.equal(writes, 1, 'releasing a selection does not replay a pending scroll');
      context.followLatest.current = false; context.following.current = false; top = 100;
      append('your-send', 'user'); assert.equal(writes, 2, 'your own send always shows'); assert.equal(top, 800);
      context.followLatest.current = false; context.following.current = false; top = 100;
      context.messages = [{id: 'older', role: 'assistant', text}, ...context.messages]; context.turns = context.messages;
      context.update(); assert.equal(writes, 2, 'older history is not a new arrival');
      context.followLatest.current = true; context.following.current = true;
      append('resumed-arrival'); assert.equal(writes, 3); assert.equal(pill, false);
      context.messages = context.messages.slice(0, -1); context.turns = context.messages;
      context.update(); assert.equal(writes, 3, 'removing a message is not an arrival');
      if (code === dockEffect) {
        context.visible = false; append('hidden-arrival'); assert.equal(writes, 3, 'hidden chat cannot scroll');
        context.visible = true; context.update(); assert.equal(writes, 4, 'reopening follows messages not yet seen at the bottom');
      }
    }
    for (const code of [dockFocus, entryFocus]) {
      let selecting = true, focused = 0, frame;
      const input = {focus: options => {assert.equal(options.preventScroll, true); focused++;}};
      const context = {resizable: true, autoFocus: true, hideComposer: false, visible: true, busy: false, activeReceipt: null, historyLoaded: true,
        proposal: null, nameInput: {current: input}, answerRef: {current: input}, inputRef: {current: input},
        isSelecting: () => selecting, requestAnimationFrame: callback => {frame = callback; return 1;}, cancelAnimationFrame() {}};
      vm.runInNewContext(code, context);
      context.update(); frame?.(); assert.equal(focused, 0, 'background focus must not collapse your selection');
      selecting = false; context.update(); frame?.(); assert.equal(focused, 1);
      if (code === dockFocus) {
        frame = undefined; context.autoFocus = false; context.update(); assert.equal(frame, undefined);
      }
      context.update(); selecting = true; frame?.();
      assert.equal(focused, code === dockFocus ? 1 : 2, 'scheduled composer focus rechecks selection');
    }
    const tree = ts.createSourceFile('ChatDock.tsx', dock, ts.ScriptTarget.Latest, true);
    const declarations = new Map();
    function visit(node) {
      if (ts.isVariableDeclaration(node)) declarations.set(node.name.getText(tree), node.initializer?.getText(tree));
      ts.forEachChild(node, visit);
    }
    visit(tree);
    const context = {local: false, allMessages: Array.from({length: 24}, (_, index) => ({id: `${venture}-${index}`, text})),
      firstVisibleId: {current: `${venture}-0`}, followLatest: {current: false}, isSelecting: () => false, visibleCount: 24};
    const render = () => {
      context.firstVisibleIndex = vm.runInNewContext(declarations.get('firstVisibleIndex'), context);
      return vm.runInNewContext(declarations.get('visibleMessages'), context);
    };
    const first = render()[0]; context.allMessages.push({id: `${venture}-24`, text});
    assert.equal(render()[0], first, 'new messages do not evict the selected oldest bubble');
    context.followLatest.current = true; context.isSelecting = () => true; assert.equal(render()[0], first);
    context.isSelecting = () => false; assert.equal(render()[0].id, `${venture}-1`);
    context.followLatest.current = false; context.visibleCount = 30;
    context.allMessages.unshift({id: `${venture}-older`, text}); assert.equal(render()[0].id, `${venture}-older`);
  }
  assert.doesNotMatch(entry, /activeQuestion\.current\?\.scrollIntoView/);
  assert.match(dock, /New messages ↓/); assert.match(entry, /New messages ↓/);
});
