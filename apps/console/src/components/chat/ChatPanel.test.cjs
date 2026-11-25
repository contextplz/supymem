const assert = require('node:assert/strict'), test = require('node:test');
const {existsSync, readFileSync} = require('node:fs'), vm = require('node:vm'), ts = require('typescript');
const compile = source => ts.transpileModule(source, {compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
}}).outputText;
const markdown = {exports: {}};
vm.runInNewContext(compile(readFileSync(__dirname + '/../../lib/chatMarkdown.ts', 'utf8')), markdown);
const source = readFileSync(__dirname + '/ChatPanel.tsx', 'utf8');

test('comparison and price links keep the complete parenthesized source URL', () => {
  for (const [name, url] of [['Inventory', 'https://stock.example/Plans_(Depot_(EU))'],
    ['Trail guide', 'https://trails.test/routes/Loop_(North)?view=map']]) {
    const html = markdown.exports.renderLightMd(`| [${name}](${url}) | $10 ([source](${url})) |`);
    assert.equal(html.split(`href="${url}"`).length - 1, 2);
    assert.match(html, /<\/a>\) \|$/);
  }
});

test('working indicator has a readable status and three decorative dots for business and consumer chats', () => {
  const tree = ts.createSourceFile('ChatPanel.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const declaration = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'ChatWorking');
  assert.ok(declaration);
  for (const domain of ['Depot scheduling', 'Weekend hiking']) {
    const context = {exports: {}, styles: new Proxy({}, {get: (_, name) => name}), require: () => ({jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})})};
    vm.runInNewContext(compile(declaration.getText(tree)), context);
    const indicator = context.exports.ChatWorking();
    assert.equal(indicator.props.role, 'status', domain);
    assert.equal(indicator.props['aria-label'], 'Charlie is working');
    assert.equal(indicator.props.children[1].props.children.length, 3);
    assert.equal(indicator.props.children[1].props['aria-hidden'], 'true');
    assert.doesNotMatch(JSON.stringify(indicator), /spend|credits|cost/i);
  }
});

test('shared mascot accepts numeric, null and omitted voice levels across unrelated audiences', () => {
  const filename = require('node:path').resolve(__dirname, '../../lib/mascotPlayer.typecheck.ts');
  const source = `
    import { mountMascot } from './mascotPlayer.js';
    async function update(host: HTMLElement) {
      const mascot = await mountMascot(host);
      const updates: { audience: string; state: 'speaking'; level: number | null }[] = [
        { audience: 'Freight dispatch teams', state: 'speaking', level: 0.2 },
        { audience: 'Home gardeners', state: 'speaking', level: 0.8 },
      ];
      for (const detail of updates) {
        mascot.setState(detail.state, detail.level);
        mascot.setState('idle', null);
        mascot.setState('listening');
      }
    }
  `;
  const options = {
    allowJs: true, strict: true, noEmit: true, skipLibCheck: true,
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
  };
  const host = ts.createCompilerHost(options);
  const getSourceFile = host.getSourceFile.bind(host);
  host.getSourceFile = (requested, languageVersion, onError, shouldCreateNewSourceFile) =>
    requested === filename ? ts.createSourceFile(filename, source, languageVersion, true)
      : getSourceFile(requested, languageVersion, onError, shouldCreateNewSourceFile);
  const program = ts.createProgram([filename], options, host);
  const diagnostics = ts.getPreEmitDiagnostics(program);
  assert.deepEqual(diagnostics.map(diagnostic => ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n')), []);
});

test('message selection survives unchanged Markdown polls and pointer handlers leave native selection alone across ventures', () => {
  for (const text of ['**Cargo Ledger** schedules loading bays for freight terminals.', '**Trail Journal** shares accessible walks with families.']) {
    let memo, selection = {isCollapsed: true}, ref, cleanup;
    const documentListeners = new Map(), windowListeners = new Map();
    const context = {exports: {}, document: {
      addEventListener: (name, handler, capture) => {assert.equal(capture, true); documentListeners.set(name, handler);},
      removeEventListener: (name, handler) => {assert.equal(documentListeners.get(name), handler); documentListeners.delete(name);},
    }, window: {
      getSelection: () => selection,
      addEventListener: (name, handler) => windowListeners.set(name, handler),
      removeEventListener: (name, handler) => {assert.equal(windowListeners.get(name), handler); windowListeners.delete(name);},
    }, require: name => {
      if (name === 'react') return {
        useMemo: (create, deps) => {if (!memo || !Object.is(memo.text, deps[0])) memo = {text: deps[0], value: create()}; return memo.value;},
        useRef: value => ref ||= {current: value}, useEffect: effect => {cleanup = effect();}, useCallback: callback => callback,
      };
      if (name === 'react/jsx-runtime') return {jsx: (type, props) => ({type, props})};
      if (name === '@/lib/chatMarkdown') return markdown.exports;
      return {};
    }};
    vm.runInNewContext(compile(source), context);
    const {ChatMessageText, useChatSelection} = context.exports;
    const isSelecting = useChatSelection();
    const first = ChatMessageText({text, className: 'message'});
    assert.equal(first.type, 'div');
    const selectedNode = {text: first.props.dangerouslySetInnerHTML.__html};
    selection = {isCollapsed: false, anchorNode: selectedNode, focusNode: selectedNode};
    assert.equal(isSelecting(), true);
    for (let poll = 0; poll < 3; poll++) {
      const next = ChatMessageText({text, className: poll ? 'message refreshed' : 'message'});
      assert.equal(next.props.dangerouslySetInnerHTML, first.props.dangerouslySetInnerHTML, 'no innerHTML write can replace the selected text nodes');
      assert.equal(selection.anchorNode, selectedNode);
    }
    const changed = ChatMessageText({text: text + ' <script>alert(1)</script>'});
    assert.notEqual(changed.props.dangerouslySetInnerHTML, first.props.dangerouslySetInnerHTML);
    assert.match(changed.props.dangerouslySetInnerHTML.__html, /&lt;script&gt;/);
    assert.doesNotMatch(changed.props.dangerouslySetInnerHTML.__html, /<script>/);
    selection = {isCollapsed: true};
    assert.equal(isSelecting(), false);
    const pointer = button => ({button, preventDefault() {assert.fail('text selection must not be cancelled');}});
    documentListeners.get('pointerdown')(pointer(2)); assert.equal(isSelecting(), false);
    documentListeners.get('pointerdown')(pointer(0)); assert.equal(isSelecting(), true);
    windowListeners.get('pointerup')(); assert.equal(isSelecting(), false);
    for (const event of ['pointercancel', 'blur']) {
      documentListeners.get('pointerdown')(pointer(0)); windowListeners.get(event)(); assert.equal(isSelecting(), false);
    }
    cleanup(); assert.equal(documentListeners.size, 0); assert.equal(windowListeners.size, 0);
    delete context.window; assert.equal(isSelecting(), false, 'server rendering has no selection');
  }
  const dock = readFileSync(__dirname + '/ChatDock.tsx', 'utf8');
  assert.match(dock, /<ChatMessageText[^>]+text=\{seg\.text\}/);
  assert.match(dock, /<ChatMessageText[^>]+text=\{m\.next_moves\.body_md\}/);
  assert.doesNotMatch(dock, /dangerouslySetInnerHTML/);
  assert.match(dock, /ChatMessageRow key=\{m\.id\}/);
});

test('the real mascot listens, speaks and falls back safely for business and consumer conversations', async () => {
  const source = readFileSync(__dirname + "/../../lib/mascotPlayer.js", "utf8");
  const {createHash} = require("node:crypto");
  for (const audience of ['Warehouse dispatch teams', 'Home vegetable gardeners']) {
    for (const supported of [true, false]) {
      let appendedCanvas;
      const events = new Map(), calls = [], host = {dataset: {}, appendChild(target) {appendedCanvas = target;}};
      const motion = {matches: false, addEventListener: (name, handler) => events.set('motion:' + name, handler), removeEventListener: name => events.delete('motion:' + name)};
      const canvas = {style: {}, hidden: false, setAttribute() {}, addEventListener: (name, handler) => events.set(name, handler), removeEventListener: name => events.delete(name), remove: () => calls.push('remove')};
      const player = {clip: 'Swing_Joy', speaking: false,
        play(name) {if (name) this.clip = name; calls.push(['play', name]);}, pause: () => calls.push('pause'),
        playOnce(name) {this.clip = name; calls.push(['once', name]);},
        speak() {this.clip = 'Speak_Start'; this.speaking = true; calls.push('speak');},
        stopSpeaking() {if (this.speaking) {this.clip = 'Speak_End'; this.speaking = false;}},
        setVoiceLevel: level => calls.push(['level', level]), dispose: () => calls.push('dispose')};
      const context = {exports: {}, document: {hidden: false, createElement: () => canvas,
        addEventListener: (name, handler) => events.set(name, handler), removeEventListener: name => events.delete(name)},
        window: {matchMedia: () => motion}};
      vm.runInNewContext(compile(source) + '\ncreateMascotPlayer = globalThis.load;', {...context, load: async (target, options) => {
        assert.equal(target, canvas, audience);
        assert.equal(target, appendedCanvas, audience);
        assert.equal(options.src, '/charlie/mascot_animations.glb', audience);
        assert.equal(options.clip, 'Swing_Joy');
        assert.equal(options.loop, true);
        calls.push(['mount', options]);
        if (!supported) throw new Error('WebGL unavailable');
        return player;
      }});
      assert.equal(typeof context.exports.mountMascot, 'function', audience);
      const mounted = await context.exports.mountMascot(host, false, 48);
      assert.equal(host.dataset.mascotReady, supported ? 'true' : 'false');
      if (supported) {
        mounted.setState('listening');
        assert.equal(player.clip, 'Listen_Nod');
        mounted.setState('listening');
        assert.equal(calls.filter(call => Array.isArray(call) && call[0] === 'once').length, 1);
        player.clip = 'Swing_Joy'; calls.find(call => Array.isArray(call) && call[0] === 'mount')[1].onClipEnd('Listen_Nod');
        assert.equal(player.clip, 'Listen_Nod');
        mounted.setState('speaking', 5); assert.equal(player.clip, 'Speak_Start');
        assert.deepEqual(calls.at(-2), ['level', 1]);
        mounted.setState('idle'); assert.equal(player.clip, 'Speak_End');
        player.clip = 'Swing_Joy'; calls.find(call => Array.isArray(call) && call[0] === 'mount')[1].onClipEnd('Speak_End');
        assert.equal(player.clip, 'Swing_Joy');
        motion.matches = true; events.get('motion:change')();
        assert.equal(host.dataset.mascotReady, 'false'); assert.equal(canvas.hidden, true);
        const count = calls.length; mounted.setState('speaking'); assert.equal(calls.length, count + 1);
        motion.matches = false; events.get('motion:change')(); assert.equal(player.clip, 'Speak_Start');
        context.document.hidden = true; events.get('visibilitychange')(); assert.equal(calls.at(-1), 'pause');
        context.document.hidden = false; events.get('visibilitychange')();
        events.get('webglcontextlost')({preventDefault() {}}); assert.equal(host.dataset.mascotReady, 'false');
      }
      mounted.dispose(); assert.equal(events.size, 0); assert.equal(calls.at(-1), 'remove');
    }
  }
  const dock = readFileSync(__dirname + '/ChatDock.tsx', 'utf8');
  assert.doesNotMatch(dock, /charlie-mark\.png|CharlieFace/);
  assert.match(dock, /<CharlieMascot active=\{visible\}/);
  assert.match(dock, /setCharlieMascot\(streaming\?\.text/);
  const component = readFileSync(__dirname + '/CharlieDock.tsx', 'utf8');
  assert.match(component, /export function CharlieMascot/);
  assert.match(component, /src="\/charlie\/poster\.png"/);
  assert.match(component, /local \? <CharlieMascot \/> : <CharlieFace \/>/);
  const posterPath = __dirname + '/../../../public/charlie/poster.png';
  const modelPath = __dirname + '/../../../public/charlie/mascot_animations.glb';
  assert.equal(existsSync(posterPath), existsSync(modelPath));
  if (existsSync(posterPath)) {
    const poster = readFileSync(posterPath);
    const model = readFileSync(modelPath);
    assert.ok(poster.includes(Buffer.from('charlie_clip\0Swing_Joy')));
    assert.ok(poster.includes(Buffer.from('charlie_model_sha256\0' + createHash('sha256').update(model).digest('hex'))));
  }
});
