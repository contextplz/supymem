const assert = require('node:assert/strict');
const {existsSync, readFileSync} = require('node:fs');
const {dirname, join, resolve} = require('node:path');
const {test} = require('node:test');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const ts = require('typescript');
const {cases} = require('../../test-fixtures/chat-contract/messages.json');

const src = resolve(__dirname, '../..');
const conversationId = 'thread_contract';

function setup() {
  const modules = new Map(), states = [], storage = new Map();
  let cursor, venture, request, history, post, saved, capabilities;
  const window = {__SUPYMEM__: {mode: 'local'}, setTimeout() {}, matchMedia: () => ({matches: false}), localStorage: {
    getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  }};
  const hooks = {...React, useEffect() {}, useCallback: callback => callback,
    useRef: current => states[cursor++] ?? (states[cursor - 1] = {current}),
    useState(initial) {
      const index = cursor++;
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
      return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }];
    },
  };
  function load(path) {
    const file = ['', '.ts', '.tsx'].map(extension => path + extension)
      .find(candidate => existsSync(candidate) && /\.tsx?$/.test(candidate));
    assert.ok(file, `Missing console module ${path}`);
    if (modules.has(file)) return modules.get(file).exports;
    const module = {exports: {}};
    modules.set(file, module);
    const localRequire = name => {
      if (name.endsWith('.css')) return {default: new Proxy({}, {get: (_, key) => key})};
      if (name === 'react' && file.endsWith('/useVentureChat.ts')) return hooks;
      if (name === 'next/navigation') return {usePathname: () => `/v/${venture}`, useRouter: () => ({}),
        useSearchParams: () => new URLSearchParams()};
      const dependency = name.startsWith('@/') ? join(src, name.slice(2)) : name.startsWith('.') ? resolve(dirname(file), name) : null;
      if (dependency?.replace(/\.ts$/, '') === join(src, 'lib/client')) return {
        ensureSession: async () => ({ok: true}), verifiedSessionSubject: () => 'founder', verifiedSessionEpoch: () => 1,
        api: async (url, options) => {
          if (options?.method === 'POST' && url === `/api/ventures/${venture}/chat`) {
            request = JSON.parse(options.body);
            return post.promise;
          }
          if (url === `/api/ventures/${venture}`) return {ok: true, data: {id: venture}};
          if (url === `/api/ventures/${venture}/device-work-orders/order_contract`)
            return {ok: true, data: {id: 'order_contract', venture_id: venture, state: 'running', requested_build: true}};
          throw new Error(`Unexpected request: ${url}`);
        },
        apiLive: async () => { history = true; return saved.promise; },
      };
      if (dependency === join(src, 'lib/activityRefresh')) return {useActivityRefresh: () => 0};
      if (dependency === join(src, 'components/settings/useInboxBriefing')) return {useInboxBriefing: () => ({enabled: true})};
      if (dependency === join(__dirname, 'useChatConversations')) return {useChatConversations: () => ({
        conversationId, conversationDraftId: conversationId, conversationVersion: 0,
        conversationReady: true, conversationCurrent: () => true,
      })};
      if (dependency === join(src, 'components/layout/LocalModeBoundary')) {
        const {surfaceVisible} = load(join(src, 'lib/localFeatureVisibility'));
        return {useLocalMode: () => true, useFeatureVisibility: () => (surface, value) => surfaceVisible(capabilities, surface, value, true)};
      }
      return dependency ? load(dependency) : require(name);
    };
    const {outputText} = ts.transpileModule(readFileSync(file, 'utf8'), {fileName: file, compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    }});
    new Function('require', 'module', 'exports', 'window', outputText)(localRequire, module, module.exports, window);
    return module.exports;
  }

  const {ChatDock} = load(join(__dirname, 'ChatDock'));
  const {useVentureChat} = load(join(__dirname, 'useVentureChat'));
  const {FounderUIContext} = load(join(src, 'lib/founderUI'));
  const hook = () => { cursor = 0; return useVentureChat(venture, {navigateOnResponse: false}); };
  return {
    reset(nextVenture) {
      venture = nextVenture; request = null; history = false;
      post = Promise.withResolvers(); saved = Promise.withResolvers();
      states.length = 0; storage.clear(); capabilities = [];
    },
    loadCapabilities(value) { capabilities = value; },
    remount() { states.length = 0; },
    hook,
    get request() { return request; },
    get history() { return history; },
    answer(message) { post.resolve({ok: true, data: message}); },
    save(items) { saved.resolve({ok: true, data: {conversation_id: conversationId, items}}); },
    render(messages, streaming) {
      return renderToStaticMarkup(React.createElement(FounderUIContext.Provider, {value: 'home'},
        React.createElement(ChatDock, {messages, streaming, ventureId: venture, conversationId, variant: 'panel',
          onSend() {}, onAction() {}, autoFocus: false})));
    },
  };
}

const count = (text, substring) => text.split(substring).length - 1;
const escaped = text => text.replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#x27;'}[char]));

function assertMessage(markup, message, where, founderRows = 0) {
  const row = `data-message-id="${message.id}"`;
  assert.equal(count(markup, 'data-message-id="'), founderRows + Number(message.kind !== 'entry_pending'),
    `${where}: no duplicate rows, including rows with different IDs`);
  if (message.kind === 'entry_pending') {
    assert.equal(count(markup, row), 0, `${where}: transient entry anchor`);
    return;
  }
  assert.equal(count(markup, row), 1, `${where}: exactly one message row`);
  const start = markup.lastIndexOf('<div', markup.indexOf(row));
  const tags = markup.slice(start).matchAll(/<div\b[^>]*>|<\/div>/g);
  let depth = 0, end;
  for (const tag of tags) {
    depth += tag[0] === '</div>' ? -1 : 1;
    if (depth === 0) { end = start + tag.index + tag[0].length; break; }
  }
  assert.ok(end, `${where}: message row must have a closing tag`);
  const rowMarkup = markup.slice(start, end);
  const actions = [...(message.actions || []), ...(message.next_moves?.actions || [])];
  for (const citation of message.citations || []) {
    for (const item of citation.inbox_briefing?.items || []) actions.push(...item.actions);
    if (citation.cos_brief?.next_move) actions.push(citation.cos_brief.next_move, ...citation.cos_brief.alternatives);
  }
  const controls = [...rowMarkup.matchAll(/<(button|a)\b([^>]*)>([\s\S]*?)<\/\1>/g)];
  for (const action of actions) {
    const label = escaped(action.label === 'Preview' && action.href?.endsWith('/preview') ? 'Open preview'
      : message.kind === 'build_tool_started' && action.label === 'Watch your build' ? 'Watch your tool' : action.label);
    const matches = controls.filter(([, tag, attributes, text]) =>
      (text.replace(/<[^>]*>/g, '') === label || attributes.includes(`aria-label="${label}"`))
      && (tag === 'button' || (action.href && /\shref="[^"]+"/.test(attributes)))
      && !/\shidden(?:=|\s|$)|aria-hidden="true"/.test(attributes)
      && /\sdisabled(?:=|\s|$)|aria-disabled="true"/.test(attributes) === Boolean(action.disabled_reason));
    assert.ok(matches.length, `${where}: missing ${action.disabled_reason ? 'disabled' : 'enabled'} control labeled ${label}`);
    if (action.action === 'status' && action.task_id) assert.equal(matches.length, 1, `${where}: duplicate ${label}`);
  }
}

test('server messages and actions survive saved history and the optimistic send lifecycle', async context => {
  const chat = setup();
  for (const {case: name, venture, message} of cases) await context.test(name, async () => {
    chat.reset(venture);
    assertMessage(chat.render([message]), message, `${name} history`);

    const body = 'Show my saved update';
    const sent = chat.hook().sendChat(body);
    const pending = chat.hook();
    const pendingMarkup = chat.render(pending.messages, pending.streaming);
    assert.equal(count(pendingMarkup, body), 1, `${name} optimistic founder row`);
    assert.match(pendingMarkup, /aria-label="Charlie is thinking"/, `${name} reply placeholder`);
    assert.ok(pending.messages[0].id.startsWith('local-out-'), name);
    const pageStarted = name.endsWith('-page-started');
    if (pageStarted) chat.loadCapabilities(null);
    chat.answer(message);
    assert.equal(await sent, true, `${name} accepted POST`);
    const answered = chat.hook();
    assertMessage(chat.render(answered.messages, answered.streaming), message, `${name} POST reply before history resolves`, 1);

    await new Promise(setImmediate);
    assert.ok(chat.history, `${name}: a local send must read saved history without waiting for another activity event`);
    chat.loadCapabilities([]);
    const nonce = chat.request.idempotency_key;
    chat.save([{id: 'msg_saved_founder', role: 'user', body_md: body, client_request_id: nonce},
      {...message, client_request_id: nonce}]);
    await new Promise(setImmediate);
    const final = chat.hook();
    const markup = chat.render(final.messages, final.streaming);
    assert.equal(count(markup, 'data-message-id="msg_saved_founder"'), 1, `${name}: saved founder message`);
    assert.equal(count(markup, body), 1, `${name}: optimistic placeholder must not duplicate the founder message`);
    assertMessage(markup, message, `${name} reconciled reply`, 1);
    if (pageStarted) {
      chat.remount();
      await chat.hook().reloadHistory();
      const reloaded = chat.hook();
      assertMessage(chat.render(reloaded.messages, reloaded.streaming), message, `${name} reloaded reply`, 1);
    }
  });
});
