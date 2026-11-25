const assert = require('node:assert/strict'), test = require('node:test');
const fs = require('node:fs'), vm = require('node:vm'), ts = require('typescript');

test('polish3: your venture header stays light without blurred banners behind unrelated venture titles', () => {
  const css = fs.readFileSync(__dirname + '/VentureChatLauncher.module.css', 'utf8');
  for (const domain of ['Compliance tracker', 'Recipe journal']) {
    assert.doesNotMatch(css, /backdrop-filter|linear-gradient|\.header::before|\.header::after/, domain);
    assert.match(css, /\.homePanel \.header \{[^}]*background: #fff/);
  }
});

test('your conversation title names its settings action for unrelated domains', () => {
  const source = fs.readFileSync(__dirname + '/VentureChatLauncher.tsx', 'utf8');
  for (const title of ['Invoice planner', 'Garden journal']) assert.match(source, /aria-label=\{`Open settings for \$\{title\}`\}/, title);
});

test('your main chat has a heading and supplemental chat stays complementary', () => {
  const source = fs.readFileSync(__dirname + '/VentureChatLauncher.tsx', 'utf8');
  const output = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX}}).outputText;
  for (const title of ['Inventory audit', 'Recipe journal']) {
    const jsx = (type, props) => ({type, props});
    const exports = {};
    vm.runInNewContext(output, {exports, require: name => name === 'react/jsx-runtime' ? {jsx, jsxs: jsx}
      : name === 'react' ? {useState: value => [value, () => {}], useContext: () => 'home', useEffect() {}}
      : name === './useVentureChat' ? {useVentureChat: () => ({managerName: title})}
      : name === './useChatHistory' ? {useChatHistory() {}} : {default: {}}});
    const primary = exports.VentureChatLauncher({primary: true});
    const heading = primary.props.children[0].props.children.props.children[0].props.children[1];
    assert.equal(primary.props.role, 'main', title);
    assert.equal(primary.props.tabIndex, -1, title);
    assert.equal(heading.type, 'h1', title);
    assert.equal(heading.props['aria-label'], title);
    assert.equal(exports.VentureChatLauncher({open: false}).props.role, 'complementary', title);
    assert.equal(exports.VentureChatLauncher({open: false}).props.inert, true, title);
  }
});

test('pending asks run once through shared handling despite effect replay or history readiness changes across domains', () => {
  const source = fs.readFileSync(__dirname + '/VentureChatLauncher.tsx', 'utf8');
  const tree = ts.createSourceFile('VentureChatLauncher.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect;
  function find(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect' && node.arguments[0].getText(tree).includes('handledAsk.current')) {
      effect = node.arguments[0].getText(tree);
    }
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(effect);
  for (const [ventureId, audience] of [['ven_compliance', 'Factory safety teams'], ['ven_cycling', 'Weekend cycling groups']]) {
    for (const action of ['chat.prefill', 'status', 'chat.new_conversation']) {
      const calls = [], acknowledgments = [];
      const ask = {action, payload: {text: `Review saved context for ${audience}.`}};
      const context = vm.createContext({pendingAsk: ask, handledAsk: {current: null}, PREFILL_ACTION: 'chat.prefill',
        chat: {historyReady: false, confirmChatAction: (action, payload) => calls.push({ventureId, action, payload})},
        onAskSent: () => acknowledgments.push(true)});
      const run = vm.runInContext(ts.transpileModule(`(${effect})`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
      run(); run();
      assert.equal(calls.length, action === 'chat.prefill' ? 1 : 0);
      context.chat.historyReady = true;
      run(); run();
      context.chat.historyReady = false;
      run();
      assert.equal(calls.length, 1);
      assert.equal(acknowledgments.length, 1);
      assert.equal(calls[0].payload, ask.payload);
      context.pendingAsk = null; run();
      context.pendingAsk = {...ask}; context.chat.historyReady = true; run(); run();
      assert.equal(calls.length, 2, 'a new click is not suppressed');
    }
  }
});
