const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const test = require('node:test');
const ts = require('typescript');
const vm = require('node:vm');

test('Charlie listens before dictation produces text across business and consumer audiences', () => {
  const source = readFileSync(path.join(__dirname, 'DictateButton.tsx'), 'utf8');
  const tree = ts.createSourceFile('DictateButton.tsx', source, ts.ScriptTarget.Latest, true);
  let effect;
  function find(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect' && node.arguments[0].getText(tree).includes('setCharlieMascot')) effect = node.arguments[0].getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(effect);
  for (const audience of ['Business travel coordinators', 'Local pottery enthusiasts']) {
    const calls = [], context = {listening: true, latest: {current: {value: ''}}, setCharlieMascot: state => calls.push(state)};
    const run = vm.runInNewContext(ts.transpileModule(`(${effect})`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    const cleanup = run(); assert.deepEqual(calls, ['listening'], audience);
    cleanup(); assert.deepEqual(calls, ['listening', 'idle']);
    context.listening = false; run(); assert.equal(calls.length, 2);
  }
});

test('native dictation errors never expose provider details and leave Dictate available for retry', async () => {
  const filename = path.join(__dirname, 'DictateButton.tsx');
  const output = ts.transpileModule(readFileSync(filename, 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText;
  const cells = [], effects = []; let cursor = 0, callback, sessionId, detached = false;
  const jsx = (type, props) => ({type, props});
  const mocks = {
    react: {
      useRef: current => cells[cursor++] ??= {current},
      useState: initial => {const index = cursor++; cells[index] ??= initial; return [cells[index], next => {cells[index] = next;}];},
      useEffect: effect => effects.push(effect),
    },
    'react/jsx-runtime': {jsx, jsxs: jsx},
    '@/lib/desktop': {
      isDesktopShell: () => true, desktopInvokeQuiet: async () => true,
      desktopListenOrThrow: async (_name, handler) => {callback = handler; return () => {detached = true;};},
      desktopInvokeOrThrow: async (_name, args) => {sessionId = args.sessionId;},
    },
    '@/components/primitives/ErrorNotice': {ErrorNotice: 'error'}, './VoiceMic.module.css': {default: {}},
    '@/lib/charlieMascot': {setCharlieMascot() {}},
  };
  const loaded = new Module(filename, module); loaded.filename = filename;
  loaded.paths = Module._nodeModulePaths(path.dirname(filename));
  const nativeRequire = loaded.require.bind(loaded); loaded.require = name => mocks[name] ?? nativeRequire(name);
  loaded._compile(output, filename);
  const render = () => {cursor = 0; return loaded.exports.DictateButton({value: '', onChange() {}});};
  const previousNavigator = Object.getOwnPropertyDescriptor(global, 'navigator');
  Object.defineProperty(global, 'navigator', {configurable: true, value: {language: 'en-US'}});
  let cleanup;
  try {
    render(); cleanup = effects[0](); await new Promise(resolve => setImmediate(resolve));
    render().props.children[0].props.onClick(); await new Promise(resolve => setImmediate(resolve));
    callback({sessionId, error: 'PRIVATE_PROVIDER_ERROR: transcript text', isFinal: false});
    const nodes = render().props.children;
    assert.equal(nodes[0].props['aria-label'], 'Dictate'); assert.equal(detached, true);
    assert.match(nodes[1].props.error, /retry Dictate.*type your answer/);
    assert.doesNotMatch(nodes[1].props.error, /PRIVATE|transcript text/);
  } finally {
    cleanup?.();
    if (previousNavigator) Object.defineProperty(global, 'navigator', previousNavigator); else delete global.navigator;
  }
});
