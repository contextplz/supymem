const assert = require('node:assert/strict');
const { existsSync, readFileSync } = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const test = require('node:test');
const ts = require('typescript');

function compiled(file, mocks) {
  const filename = path.join(__dirname, file);
  const output = ts.transpileModule(readFileSync(filename, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const loaded = new Module(filename, module);
  loaded.filename = filename; loaded.paths = Module._nodeModulePaths(path.dirname(filename));
  const native = loaded.require.bind(loaded);
  loaded.require = id => mocks[id] ?? native(id);
  loaded._compile(output, filename);
  return loaded.exports;
}
const css = new Proxy({}, { get: (_, key) => key });
const jsx = (type, props) => ({ type, props });
const styles = { default: css };
const elements = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(5).flatMap(elements)];
function mic(phase, overrides = {}, props = {}, effects = []) {
  const portalTarget = {};
  global.document = { body: portalTarget };
  const { VoiceMic } = compiled('VoiceMic.tsx', {
    react: { useRef: current => ({ current }), useEffect: effect => effects.push(effect), useCallback: fn => fn },
    'react/jsx-runtime': { jsx, jsxs: jsx }, 'react-dom': { createPortal: (children, target) => ({ type: 'portal', props: { children, target } }) },
    './VoiceOrb': { VoiceOrb: 'orb' }, './VoiceMic.module.css': styles,
    '@/lib/desktop': { isDesktopShell: () => false },
    '@/lib/voice': { voiceSessionError: () => 'Open Settings > Keys and connections > Your AI provider and save your OpenAI key, then try voice again.', LONG_PRESS_MS: 250, PHASE_LABEL: { connecting: 'Connecting…', listening: 'Listening', speaking: 'Speaking', thinking: 'Thinking' } },
    './useVoiceChat': { useVoiceKeys() {}, voiceCallActive: () => callActive, useVoiceChat: () => ({ phase, voiceFirst: true, enabled: true, getLevel: () => .7, live: [{ id: 'transcript', role: 'founder', text: 'Hello' }], ...overrides }) },
    '@/components/primitives/ErrorNotice': { ErrorNotice: 'error' },
  });
  return { tree: VoiceMic({ ventureId: null, onVoiceSend: async () => null, ...props }), portalTarget };
}
let callActive = false;
test('unavailable voice keeps setup help in a closed microphone popover across domains', () => {
  const note = 'Open Settings > Keys and connections > Your AI provider and save your OpenAI key, then try voice again. You can keep typing.';
  for (const audience of ['Warehouse dispatch teams', 'People planning garden beds']) {
    const tree = mic('idle', {enabled: false, note}, {screen: {step: audience}}).tree;
    assert.ok(tree);
    assert.equal(tree.type, 'details');
    assert.notEqual(tree.props.open, true);
    assert.equal(elements(tree).find(node => node.type === 'summary').props['aria-label'], 'Talk live');
    assert.equal(elements(tree).find(node => node.props['data-voice-note']).props.children, note);
    assert.equal(elements(tree).some(node => node.type === 'portal'), false);
  }
});

test('losing voice availability never hides your active microphone or Type instead control across domains', () => {
  for (const audience of ['Warehouse dispatch teams', 'People planning garden beds']) {
    const tree = mic('listening', {enabled: false, note: 'Check your OpenAI key.'}, {screen: {step: audience}}).tree;
    assert.ok(elements(tree).some(node => node.props?.['aria-label'] === 'End voice'));
    assert.ok(elements(tree).some(node => node.props?.['data-usage-control'] === 'voice_end'));
  }
});
test('chat controls do not advertise usage charges across unrelated audiences', t => {
  const previousDocument = global.document;
  t.after(() => { global.document = previousDocument; });
  for (const [ventureId, audience] of [['ven_labs', 'Clinical lab schedulers'], ['ven_trails', 'Weekend hikers']]) {
    const button = elements(mic('idle', {}, {ventureId, screen: {page: '/new', step: audience}}).tree)
      .find(node => node.props?.['data-usage-control'] === 'voice_mic_press');
    assert.doesNotMatch(button.props.title, /credits?/i);
    assert.doesNotMatch(button.props['aria-label'], /credits?/i);
  }
  const dock = readFileSync(path.join(__dirname, 'ChatDock.tsx'), 'utf8');
  assert.doesNotMatch(dock, /<ReferralCard/);
  assert.doesNotMatch(dock, /You can review your referrals in Settings\./);
  const referralPath = path.join(__dirname, '../credits/ReferralCard.tsx');
  if (existsSync(referralPath)) {
    assert.doesNotMatch(readFileSync(referralPath, 'utf8'), /<p>Earn|formatCreditBalance\(referral\.used_credits\)/);
  }
});

test('both unrelated ventures use Talk live in typing, voice-first, retry and recovery presentations', () => {
  for (const [ventureId, audience] of [['ven_billing', 'Hospital billing teams'], ['ven_birds', 'Backyard birdwatchers']]) {
    for (const voiceFirst of [true, false]) {
      for (const state of [{}, {error: 'Connection failed'}, {recovery: 'Resume your conversation'}]) {
        const button = elements(mic('idle', {voiceFirst, ...state}, {ventureId, screen: {step: audience}}).tree)
          .find(node => node.props?.['data-usage-control'] === 'voice_mic_press');
        assert.equal(button.props['aria-label'], 'Talk live');
        assert.equal(button.props.children[1].props.children, 'Talk live');
        assert.match(button.props.title, /^Talk live/);
      }
    }
  }
});
test('voice mode has separate nonshrinking icon and label slots and an accessible narrow control across domains', t => {
  const previousDocument = global.document;
  t.after(() => { global.document = previousDocument; });
  for (const [ventureId, audience] of [['ven_billing', 'Hospital billing teams'], ['ven_birds', 'Backyard birdwatchers']]) {
    for (const phase of ['idle', 'connecting', 'listening']) {
      const button = elements(mic(phase, {}, {ventureId, compact: true, screen: {page: '/new', step: audience}}).tree)
        .find(node => node.props?.['data-usage-control'] === 'voice_mic_press');
      const [icon, label] = button.props.children;
      assert.equal(icon.props['aria-hidden'], 'true');
      assert.equal(label.props.className, 'micLabel');
      assert.notEqual(icon, label);
      assert.equal(button.props['aria-label'], phase === 'idle' ? 'Talk live' : 'End voice');
    }
  }
  const voice = readFileSync(path.join(__dirname, 'VoiceMic.module.css'), 'utf8');
  assert.match(voice, /\.wrap \{[^}]*display: inline-flex;[^}]*gap: 6px;[^}]*flex-shrink: 0/);
  assert.match(voice, /\.mic svg \{[^}]*flex: none/);
  assert.match(voice, /\.micLabel \{[^}]*white-space: nowrap/);
  const composer = readFileSync(path.join(__dirname, 'ChatDock.module.css'), 'utf8');
  assert.match(composer, /\.homePanel \{[^}]*container-type: inline-size/);
  assert.match(composer, /button\[data-usage-control="voice_mic_press"\] \{[^}]*width: auto;[^}]*padding: 0 8px;[^}]*gap: 6px/);
  assert.match(composer, /button\[data-usage-control="voice_mic_press"\] > span:not\(\[aria-hidden\]\) \{ display: inline; \}/);
  assert.match(composer, /@container \(max-width: 520px\) \{\s*\.homePanel \.composerTools button\[data-usage-control="voice_mic_press"\] > span:not\(\[aria-hidden\]\) \{ display: none; \}/);
});
test('voice-first text controls keep intrinsic width and wrap without colliding across domains', t => {
  const previousDocument = global.document;
  t.after(() => { global.document = previousDocument; });
  for (const [ventureId, audience] of [['ven_inventory', 'Warehouse inventory teams'], ['ven_recipes', 'Home cooks']]) {
    let ended = 0;
    const tree = mic('idle', {typeInstead: () => ended++}, {ventureId, screen: {page: '/new', step: audience}}).tree;
    const controls = elements(tree).filter(node => node.type === 'button');
    assert.equal(controls[0].props.children[1].props.children, 'Talk live');
    const typing = controls.find(node => node.props['data-voice-type-instead'] !== undefined);
    assert.ok(typing, audience);
    assert.equal(typing.props.children, 'Type instead');
    typing.props.onClick();
    assert.equal(ended, 1);
  }
  const composer = readFileSync(path.join(__dirname, 'ChatDock.module.css'), 'utf8');
  assert.match(composer, /\.homePanel \.composerTools button\[data-voice-type-instead\]\s*\{[^}]*width: auto;[^}]*max-width: 100%;[^}]*height: auto;[^}]*padding: 0 8px;[^}]*white-space: normal;[^}]*overflow-wrap: anywhere;/);
  assert.match(composer, /\.composerActions button\s*\{[^}]*flex: none/);
  assert.match(readFileSync(path.join(__dirname, 'VoiceMic.module.css'), 'utf8'), /\.wrap\s*\{[^}]*flex-wrap: wrap;[^}]*gap: 6px/);
});
test('live voice shortcut uses the composer toggle and respects voice availability and activity', t => {
  const previousWindow = global.window, previousEvent = global.CustomEvent, previousDocument = global.document;
  t.after(() => { global.window = previousWindow; global.CustomEvent = previousEvent; global.document = previousDocument; });
  const listeners = new Map();
  global.window = { addEventListener: (name, listener) => listeners.set(name, listener), removeEventListener: name => listeners.delete(name),
    dispatchEvent: event => listeners.get(event.type)?.(event) };
  let stopped = 0;
  global.CustomEvent = class { constructor(type) { this.type = type; } stopImmediatePropagation() { stopped++; } };
  const launcher = compiled('VentureChatLauncher.tsx', {
    react: { useContext: () => 'home', useState: initial => [initial, () => {}], useRef: current => ({current}), useEffect() {} },
    'react/jsx-runtime': { jsx, jsxs: jsx }, '@/lib/founderUI': { FounderUIContext: {} },
    './CharlieDock': { CharlieMascot: 'face' }, './ChatDock': { ChatDock: 'dock' },
    './useVentureChat': { useVentureChat: () => ({ managerName: 'Guide', messages: [], conversationItems: [], archivedConversationItems: [] }) },
    './useChatHistory': {useChatHistory() {}},
    '@/lib/chatPrefill': {}, '@/components/primitives/ErrorNotice': { ErrorNotice: 'error' }, './VentureChatLauncher.module.css': styles,
  });
  assert.equal(elements(launcher.VentureChatLauncher({ ventureId: 'ven_voice', size: {}, onSizeChange() {} }))
    .some(node => node.props?.['aria-label'] === 'Talk to Charlie live'), false);
  const requestVoice = () => window.dispatchEvent(new CustomEvent('supymem-chat-voice'));
  let toggles = 0;
  const toggle = () => toggles++;
  const effects = [];
  const composer = elements(mic('idle', { toggle }, { active: true }, effects).tree)
    .find(node => node.props?.['data-usage-control'] === 'voice_mic_press');
  const cleanups = effects.map(effect => effect());
  requestVoice(); assert.equal(toggles, 1);
  assert.equal(stopped, 1, 'the panel that handles the click keeps other panels from starting a second call');
  callActive = true;
  requestVoice(); assert.equal(toggles, 1, 'another panel owns the live call, so this one ignores the click');
  callActive = false;
  composer.props.onClick({ detail: 0 }); assert.equal(toggles, 2);
  cleanups.forEach(cleanup => cleanup?.());
  assert.equal(listeners.size, 0);
  for (const [voice, props] of [[{ enabled: false }, {}], [{}, { active: false }], [{}, { disabled: true }], [{ summaryPending: true }, {}]]) {
    const blockedEffects = [];
    mic('idle', { toggle, ...voice }, props, blockedEffects);
    const blockedCleanups = blockedEffects.map(effect => effect());
    requestVoice(); assert.equal(toggles, 2);
    blockedCleanups.forEach(cleanup => cleanup?.());
  }
});
test('compact clarification composers keep idle voice icon-only and share the accessible Send control at every width', () => {
  for (const [ventureId, audience] of [['ven_freight', 'Freight dispatch teams'], ['ven_garden', 'Home gardeners']]) {
    const props = { ventureId, compact: true, screen: { page: '/new', step: audience } };
    const idle = elements(mic('idle', {}, props).tree);
    assert.equal(idle.filter(node => node.type === 'button').length, 1);
    assert.equal(idle.some(node => node.type === 'small'), false);
    assert.equal(idle.some(node => node.props?.children === 'Type instead'), false);
    const live = elements(mic('listening', {}, props).tree);
    const portal = live.find(node => node.type === 'portal');
    assert.ok(elements(portal).some(node => node.props?.['data-usage-control'] === 'voice_end'));
  }
  const dock = readFileSync(path.join(__dirname, 'ChatDock.tsx'), 'utf8');
  assert.match(dock, /<VoiceMic[^>]*compact=\{homePanel\}/);
  assert.match(readFileSync(path.join(__dirname, '../../app/new/page.tsx'), 'utf8'), /const voiceControl = <VoiceMic[^>]* compact/);
  assert.match(dock, /aria-label=\{submitLabel\}[\s\S]*\{homePanel \? "↑" : submitLabel\}/);
  const css = readFileSync(path.join(__dirname, 'ChatDock.module.css'), 'utf8');
  for (const selector of ['composerBox', 'composerActions', 'composerTools']) {
    assert.match(css, new RegExp(`\\.homePanel \\.${selector} \\{[^}]*min-width: 0;[^}]*flex-wrap: wrap;`));
  }
  assert.match(css, /\.homePanel \.composerTools \{[^}]*gap: 4px/);
  assert.match(css, /\.homePanel \.composerTools \[data-voice-note\] \{[^}]*flex-basis: 100%;[^}]*white-space: normal/);
  assert.match(css, /button\[data-usage-control="chat_send"\],\s*\.homePanel \.composerActions button\[data-usage-control="chat_options_submit"\] \{[^}]*width: var\(--im-inline, 28px\);[^}]*border-radius: 50%/);
  assert.equal((css.match(/button\[data-usage-control="chat_options_submit"\]/g) || []).length, 1);
});
test('voice-first opens ready with explicit tap, keyboard hint and typing fallback, never a live orb', () => {
  let starts = 0, typing = 0;
  const nodes = elements(mic('idle', {toggle: () => {starts++;}, typeInstead: () => {typing++;}}).tree);
  assert.equal(starts, 0);
  const button = nodes.find(node => node.props?.['data-usage-control'] === 'voice_mic_press');
  assert.equal(button.props['aria-label'], 'Talk live');
  assert.equal(button.props['aria-pressed'], false);
  assert.ok(nodes.find(node => node.props?.children === 'Hold Space to talk'));
  assert.equal(nodes.some(node => node.type === 'orb'), false);
  nodes.find(node => node.type === 'button' && node.props.children === 'Type instead').props.onClick();
  assert.equal(typing, 1); assert.equal(starts, 0);
  button.props.onClick({detail: 0}); assert.equal(starts, 1);
  button.props.onClick({detail: 1}); assert.equal(starts, 1);
});
test('typing presentation retains a working voice action, and legacy unavailable copy becomes closed setup help', () => {
  const nodes = elements(mic('idle', {voiceFirst: false}).tree);
  assert.equal(nodes.find(node => node.props?.['data-usage-control'] === 'voice_mic_press').props['aria-label'], 'Talk live');
  assert.equal(nodes.some(node => node.type === 'button' && node.props.children === 'Type instead'), false);
  const fallback = mic('idle', {enabled: false, note: 'Voice unavailable, type instead'}).tree;
  assert.equal(fallback.type, 'details');
  assert.notEqual(fallback.props.open, true);
  assert.match(elements(fallback).find(node => node.props['data-voice-note']).props.children, /Keys and connections > Your AI provider/);
});
test('connecting mic uses its own spinner and portalled status, never the listening pulse', () => {
  const { tree, portalTarget } = mic('connecting'), nodes = elements(tree);
  const button = nodes.find(node => node.props?.['data-usage-control'] === 'voice_mic_press');
  assert.equal(button.props['aria-busy'], true);
  assert.equal(button.props.title, 'Connecting…');
  assert.match(button.props.className, /connecting/);
  assert.doesNotMatch(button.props.className, /listening/);
  assert.ok(nodes.find(node => node.props?.className === 'spinner'));
  assert.ok(nodes.find(node => node.type === 'portal' && node.props.target === portalTarget));
  assert.ok(nodes.find(node => node.props?.['data-voice-status'] !== undefined));
  assert.ok(nodes.find(node => node.props?.['aria-live'] === 'polite'));
});
test('blocked playback exposes a gesture button and connecting exposes mic guidance', () => {
  let attempts = 0;
  const nodes = elements(mic('connecting', {playbackBlocked: true, enableSound: () => { attempts++; }, note: 'Allow microphone access'}).tree);
  const button = nodes.find(node => node.props?.['data-usage-control'] === 'voice_enable_sound');
  assert.equal(button.props.children, 'Enable sound'); button.props.onClick(); assert.equal(attempts, 1);
  assert.ok(nodes.find(node => node.props?.role === 'status' && node.props.children === 'Allow microphone access'));
});
test('dropped, ended and failed voice offer explicit Resume, Retry and typing actions', () => {
  for (const [phase, recovery, label] of [['idle', 'dropped', 'Resume'], ['idle', 'ended', 'Resume'], ['idle', '', 'Retry']]) {
    let invoked = 0, typed = 0;
    const nodes = elements(mic(phase, {recovery, error: 'Voice stopped. Try again.', note: recovery ? 'Call dropped - Resume' : '', voiceFirst: false,
      resume: () => invoked++, retry: () => invoked++, typeInstead: () => typed++}).tree);
    nodes.find(node => node.type === 'button' && node.props.children === label && !node.props['data-usage-control']).props.onClick();
    assert.equal(invoked, 1);
    nodes.find(node => node.type === 'button' && node.props.children === 'Type instead').props.onClick();
    assert.equal(typed, 1);
  }
});
test('listening and speaking preserve a visible end control and their distinct orb phase', () => {
  for (const phase of ['listening', 'speaking', 'thinking']) {
    const nodes = elements(mic(phase).tree);
    assert.equal(nodes.find(node => node.type === 'orb').props.phase, phase);
    assert.equal(nodes.find(node => node.props?.['data-usage-control'] === 'voice_mic_press').props['aria-busy'], false);
    assert.ok(nodes.find(node => node.props?.['data-usage-control'] === 'voice_end'));
  }
});
test('status and buttons wrap in one row, with a wrapping note below it', () => {
  const note = "I'm still working on it. I'll speak when the result arrives.";
  const nodes = elements(mic('listening', {note}).tree);
  const status = nodes.find(node => node.props?.['data-voice-status'] !== undefined);
  const row = elements(status).find(node => node.props?.className === 'bar');
  assert.ok(elements(row).find(node => node.props?.['aria-live'] === 'polite'));
  assert.ok(elements(row).find(node => node.props?.['data-usage-control'] === 'voice_end'));
  assert.equal(elements(row).some(node => node.props?.children === note), false);
  assert.equal(status.props.children.at(-1).props.children, note);
  const micCss = readFileSync(path.join(__dirname, 'VoiceMic.module.css'), 'utf8');
  assert.match(micCss, /\.bar\s*\{[^}]*flex-wrap: wrap/);
  assert.match(micCss, /\.label\s*\{[^}]*white-space: normal;[^}]*overflow-wrap: anywhere/);
});
function orb(reduced = false) {
  const properties = [], effects = [], frames = new Map(), listeners = new Map(); let next = 0;
  global.window = { matchMedia: () => ({ matches: reduced, addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) }) };
  global.requestAnimationFrame = fn => { frames.set(++next, fn); return next; };
  global.cancelAnimationFrame = id => frames.delete(id);
  const { VoiceOrb, orbState } = compiled('VoiceOrb.tsx', { 'react/jsx-runtime': { jsx, jsxs: jsx }, './VoiceOrb.module.css': styles,
    react: { useRef: () => ({ current: { style: { setProperty: (...args) => properties.push(args) } } }), useEffect: fn => effects.push(fn) },
  });
  const tree = VoiceOrb({ phase: 'listening', getLevel: () => 9, children: 'bars' });
  return { tree, orbState, properties, effects, frames, listeners };
}
test('orb clamps the audio signal and the shared loop stops on unmount', () => {
  const h = orb();
  assert.deepEqual(h.orbState('connecting', -1), { phase: 'connecting', level: 0 });
  assert.equal(h.orbState('speaking', Infinity).level, 0);
  assert.equal(h.orbState('listening', 4).level, 1);
  const cleanup = h.effects[0]();
  assert.equal(h.frames.size, 1);
  const [id, frame] = h.frames.entries().next().value; h.frames.delete(id); frame();
  assert.deepEqual(h.properties.at(-1), ['--lvl', '1.00']);
  assert.equal(h.tree.props.children[0].props['aria-hidden'], 'true');
  cleanup(); assert.equal(h.frames.size, 0); assert.equal(h.listeners.size, 0);
});
test('reduced motion disables the audio animation loop and CSS rotation/scaling', () => {
  const h = orb(true), cleanup = h.effects[0]();
  assert.equal(h.frames.size, 0); cleanup();
  const orbCss = readFileSync(path.join(__dirname, 'VoiceOrb.module.css'), 'utf8');
  const micCss = readFileSync(path.join(__dirname, 'VoiceMic.module.css'), 'utf8');
  assert.match(orbCss, /prefers-reduced-motion: reduce[\s\S]*animation: none; transform: none; rotate: none/);
  assert.match(micCss, /prefers-reduced-motion: reduce[\s\S]*\.listening, \.spinner \{ animation: none/);
  assert.match(micCss, /min-width: 1100px[\s\S]*width: min\(320px, 26vw\)/);
  assert.match(micCss, /\.status\s*\{\s*position: fixed;/);
});
