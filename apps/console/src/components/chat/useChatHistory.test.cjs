const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

test('current onboarding conversation appears immediately in both chat surfaces without duplicates or archived entries', () => {
  const source = ts.transpileModule(readFileSync(__dirname + '/useChatHistory.ts', 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  }).outputText;
  for (const [ventureId, text] of [['ven_industrial', 'What do manufacturing teams use today?'], ['ven_family', 'What do parents use to plan dinners?']]) {
    const published = [], listeners = new Map(), selected = [], chat = {conversationsAvailable: true, conversationId: 'current',
      conversationItems: [], archivedConversationItems: [], messages: [], reloadConversations() {}, selectConversation: id => selected.push(id)};
    const exports = {};
    vm.runInNewContext(source, {exports, require: () => ({useEffect: effect => effect(), useRef: current => ({current}), isLocalConsole: () => false}),
      window: {dispatchEvent: event => published.push(event), addEventListener: (type, callback) => listeners.set(type, callback), removeEventListener() {}},
      CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options?.detail;}}});
    const render = () => {
      exports.useChatHistory(ventureId, chat);
      return published.filter(event => event.type === 'supymem:chat-history').at(-1).detail;
    };
    assert.equal(render().items.length, 0);
    chat.messages = [{id: 'question', role: 'assistant', body_md: text, ts: '2026-10-24T10:00:00Z'}];
    assert.equal(render().items[0].id, 'current');
    assert.equal(render().items[0].text, text);
    listeners.get('supymem:chat-jump')({detail: {id: 'current'}});
    assert.deepEqual(selected, ['current']);
    chat.conversationItems = [{id: 'current', title: 'Saved title'}];
    assert.equal(render().items.length, 1);
    assert.equal(render().items[0].text, 'Saved title');
    chat.conversationItems = [];
    chat.archivedConversationItems = [{id: 'current', title: 'Archived title', archived: true}];
    assert.equal(render().items.length, 0);
    assert.equal(render().archivedItems.length, 1);
  }
  assert.match(readFileSync(__dirname + '/VentureChatLauncher.tsx', 'utf8'), /useChatHistory\(ventureId, chat\)/);
  assert.match(readFileSync(__dirname + '/../../app/new/ConfirmedConversation.tsx', 'utf8'), /useChatHistory\(receipt\.venture_id, chat\)/);
});
