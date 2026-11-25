const assert = require("node:assert/strict");
const { existsSync, readFileSync } = require("node:fs");
const Module = require("node:module");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");
const vm = require("node:vm");

function compiled(file, mocks = {}) {
  const filename = path.join(__dirname, file);
  const output = ts.transpileModule(readFileSync(filename, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const loaded = new Module(filename, module);
  loaded.filename = filename; loaded.paths = Module._nodeModulePaths(path.dirname(filename));
  const native = loaded.require.bind(loaded);
  loaded.require = id => mocks[id] ?? native(id);
  loaded._compile(output, filename);
  return loaded.exports;
}
const helpers = compiled("../../lib/charlieGreeting.ts");
const mascotMocks = { "@/components/primitives/ErrorNotice": { ErrorNotice: "p" }, "@/lib/mascotPlayer.js": {}, "@/lib/charlieMascot": { setCharlieMascot() {} }, "@/components/layout/LocalModeBoundary": { useLocalMode: () => false } };
const flush = async () => { for (let count = 0; count < 20; count++) await Promise.resolve(); };
const elements = node => !node || typeof node !== "object" ? [] : [node, ...[node.props?.children].flat(5).flatMap(elements)];

test("local welcome shows your on-device explanation and real swing without requesting voice across domains", async () => {
  for (const domain of ["industrial maintenance teams", "neighborhood cooking clubs"]) {
    for (const reduced of [false, true]) {
      const effects = [], mounts = [], requests = [], choices = [];
      let disposed = 0;
      const jsx = (type, props) => {
        if (props?.ref) props.ref.current = {style: {}, appendChild() {}};
        return {type, props};
      };
      const {CharlieIntroWelcome} = compiled("CharlieIntro.tsx", {
        ...mascotMocks,
        react: {useRef: current => ({current}), useEffect: run => effects.push(run)},
        "react/jsx-runtime": {jsx, jsxs: jsx},
        "@/lib/client": {api: url => {requests.push(url); throw new Error("No voice key");}},
        "@/lib/desktop": {}, "@/lib/controlOutcome": {}, "@/lib/usageControl": {},
        "@/lib/charlieGreeting": helpers, "./CharlieIntro.module.css": {default: {}},
        "@/lib/mascotPlayer.js": {mountMascot: async (_host, motion, size) => {
          mounts.push({motion, size});
          return {setState() {}, dispose: () => {disposed++;}};
        }},
      });
      global.window = {matchMedia: () => ({matches: reduced})};
      const tree = CharlieIntroWelcome({onConnect: () => choices.push(domain)});
      const nodes = elements(tree);
      const cleanup = effects.map(run => run());
      await flush();
      assert.deepEqual(mounts, [{motion: reduced, size: 320}]);
      assert.ok(nodes.some(node => node.type === "img" && node.props.src === "/charlie/poster.png"));
      const description = nodes.find(node => node.type === "p").props.children;
      assert.match(description, /Supymem.*your machine/);
      assert.match(description, /your.*keys/);
      assert.equal(description.split(/[.!?]+/).filter(sentence => sentence.trim()).length, 2);
      assert.doesNotMatch(description, /Voice unavailable|\u2014/);
      assert.equal(tree.props.style.colorScheme, "light");
      assert.ok(nodes.some(node => node.props?.role === "dialog" && node.props["aria-modal"] === true));
      const buttons = nodes.filter(node => node.type === "button");
      assert.equal(buttons.length, 1);
      assert.equal(buttons[0].props.children, "Connect a model");
      buttons[0].props.onClick();
      assert.deepEqual(choices, [domain]);
      assert.deepEqual(requests, []);
      cleanup.forEach(stop => stop?.());
      assert.equal(disposed, 1);
    }
  }
});

test("local welcome disposes a mascot that finishes loading after you leave", async () => {
  let resolve, cleanup;
  let disposed = 0;
  const jsx = (type, props) => {
    if (props?.ref) props.ref.current = {};
    return {type, props};
  };
  const {CharlieIntroWelcome} = compiled("CharlieIntro.tsx", {
    ...mascotMocks,
    react: {useRef: current => ({current}), useEffect: run => {cleanup = run;}},
    "react/jsx-runtime": {jsx, jsxs: jsx}, "@/lib/client": {}, "@/lib/desktop": {},
    "@/lib/controlOutcome": {}, "@/lib/usageControl": {}, "@/lib/charlieGreeting": helpers,
    "./CharlieIntro.module.css": {default: {}},
    "@/lib/mascotPlayer.js": {mountMascot: () => new Promise(done => {resolve = done;})},
  });
  global.window = {matchMedia: () => ({matches: false})};
  CharlieIntroWelcome({onConnect() {}});
  const stop = cleanup();
  await flush();
  stop();
  resolve({dispose: () => {disposed++;}});
  await flush();
  assert.equal(disposed, 1);
});

test("missing exported artwork and demo media leave your controls usable in unrelated domains", async () => {
  const jsx = (type, props) => typeof type === "function" ? type(props) : {type, props};
  const react = {useRef: () => ({current: null}), useEffect() {}, useContext: () => "home", useState: initial => [initial, () => {}]};
  const dock = compiled("../chat/CharlieDock.tsx", {react, "react/jsx-runtime": {jsx, jsxs: jsx},
    "next/link": {default: "a"}, "@/lib/founderUI": {}, "@/lib/charlieMascot": {}, "./CharlieDock.module.css": {default: {}}});
  const guide = compiled("../guide/CapabilityGuide.tsx", {react, "react/jsx-runtime": {jsx, jsxs: jsx},
    "next/link": {default: "a"}, "@/components/chat/ChatLink": {default: "a", ChatAnchor: "a"},
    "@/lib/capabilityGuide": compiled("../../lib/capabilityGuide.ts"), "@/lib/chatPrefill": {},
    "@/components/layout/LocalModeBoundary": {useLocalMode: () => false, useFeatureVisibility: () => () => true},
    "@/lib/client": {api: () => assert.fail("Your hosted guide does not load local settings")},
    "@/app/settings/local/capabilities/capabilities": require("../../app/settings/local/capabilities/capabilities.ts"),
    "@/components/layout/AppShell": {}, "@/components/layout/NavIcons": {NAV_ICONS: new Proxy({}, {get: () => () => jsx("svg", {})})},
    "./CapabilityGuide.module.css": {default: {}}});
  for (const domain of ["warehouse dispatch teams", "family gardening clubs"]) {
    const intro = harness([{ok: true, status: 200, data: {text: `You help ${domain}`, greeted_at: null}}]);
    try {
      await intro.settle();
      const trees = [dock.CharlieFace({}), dock.CharlieMascot({}), guide.CapabilityGuide({ventureId: domain}), intro.render()];
      for (const tree of trees) for (const image of elements(tree).filter(node => node.type === "img")) {
        const target = {style: {}};
        assert.equal(typeof image.props.onError, "function", `${domain}: ${image.props.src}`);
        image.props.onError({currentTarget: target});
        assert.equal(target.style.display, "none");
      }
      assert.ok(intro.nodes().some(node => node.props?.["data-usage-control"] === "greeting_skip"));
      assert.ok(intro.nodes().some(node => node.type === "span" && node.props.children === "C"));
      const chat = dock.CharlieDock({managerName: domain, open: false, chatAvailable: true, onOpen() {}, onClose() {}});
      assert.ok(elements(chat).some(node => node.type === "button" && node.props["aria-label"] === `Open chat with ${domain}`));
    } finally { intro.dispose(); }
  }
  const root = path.resolve(__dirname, "../../../../..");
  for (const file of ["src-tauri/splashscreen/local-starting.html", "docs/site/index.html"]) {
    if (file.startsWith("docs/") && !existsSync(path.join(root, file))) continue;
    const html = readFileSync(path.join(root, file), "utf8");
    if (file.startsWith("src-tauri/")) {
      assert.doesNotMatch(html, /\bonerror=/);
      assert.doesNotMatch(html, /\burl\s*\(|<(?:img|script|link)\b/i);
      assert.match(html, /role="status"[^>]*>Your app is getting ready/);
      assert.match(html, /quit and reopen your app to retry/i);
    }
    for (const image of html.matchAll(/<img\b[^>]*>/g)) {
      const handler = image[0].match(/onerror="([^"]+)"/);
      assert.ok(handler, `${file}: ${image[0]}`);
      const target = {style: {}};
      vm.runInNewContext(`(function(){${handler[1]}}).call(target)`, {target});
      assert.equal(target.style.display, "none");
    }
  }
  const mascotSource = readFileSync(path.join(__dirname, "../../lib/mascotPlayer.js"), "utf8").replace(/^export /gm, "");
  const failedCanvas = {style: {}, setAttribute() {}, addEventListener() {}, removeEventListener() {}, remove() {}, getContext: () => ({})};
  const mascotHost = {dataset: {}, appendChild() {}};
  const mascotContext = {ArrayBuffer, document: {hidden: false, createElement: () => failedCanvas, addEventListener() {}, removeEventListener() {}},
    window: {matchMedia: () => ({matches: false, addEventListener() {}, removeEventListener() {}})},
    fetch: async () => ({ok: false, arrayBuffer: async () => new ArrayBuffer(0)})};
  vm.runInNewContext(mascotSource, mascotContext);
  const mounted = await mascotContext.mountMascot(mascotHost);
  assert.equal(mounted.player, null);
  assert.equal(failedCanvas.hidden, true);
  assert.equal(mascotHost.dataset.mascotReady, "false");
  mounted.dispose();
  const siteScript = path.join(root, "docs/site/charlie.js");
  if (!existsSync(siteScript)) return;
  const source = readFileSync(siteScript, "utf8").replace(/import\.meta\.url/g, '"https://example.org/charlie.js"');
  for (const present of [false, true]) {
    const listeners = {};
    const element = name => ({hidden: false, style: {}, src: `https://example.org/${name}`, poster: "https://example.org/poster.png",
      addEventListener: (event, handler) => {listeners[`${name}:${event}`] = handler;}, setAttribute() {}, removeAttribute: attribute => {delete nodes.video[attribute];},
      remove() {this.removed = true;}, querySelector: selector => nodes[selector === "source" ? "source" : "download"]});
    const nodes = Object.fromEntries(["button", "canvas", "poster", "video", "source", "download", "icon"].map(name => [name, element(name)]));
    const imageProbes = [];
    const context = {URL, Image: class {constructor() {imageProbes.push(this);}},
      document: {hidden: false, querySelector: selector => nodes[({"#charlie-toggle": "button", "#charlie-canvas": "canvas", "#charlie-poster": "poster", "video": "video", 'link[rel="icon"]': "icon"})[selector]], addEventListener: (event, handler) => {listeners[event] = handler;}},
      window: {matchMedia: () => ({matches: false, addEventListener() {}})}, fetch: async () => ({ok: present})};
    vm.runInNewContext(source, context);
    await flush();
    if (present) {
      assert.ok(!nodes.video.removed);
      assert.equal(nodes.download.hidden, false);
    } else {
      assert.equal(nodes.video.removed, true);
      assert.equal(imageProbes.length, 2);
      imageProbes[0].onerror();
      assert.equal(nodes.icon.removed, true);
      imageProbes[1].onerror();
      assert.equal(nodes.video.poster, undefined);
    }
    nodes.poster.style.display = "none";
    await listeners["button:click"]();
    assert.equal(nodes.canvas.hidden, true);
    assert.equal(nodes.button.hidden, true);
    listeners.visibilitychange();
    assert.equal(nodes.poster.style.display, "none");
  }
});

test("intro mounts the shared zero-dependency mascot at intro size and disposes it", async () => {
  const calls = [];
  const canvas = {style: {}, setAttribute: (name, value) => calls.push([name, value]), remove: () => calls.push(["remove"])};
  const host = {appendChild: target => assert.equal(target, canvas)};
  global.document = {createElement: name => {assert.equal(name, "canvas"); return canvas;}};
  const player = {pause: () => calls.push(["pause"]), dispose: () => calls.push(["dispose"])};
  const {mountMascot} = compiled("CharlieIntro.tsx", {
    ...mascotMocks, react: {}, "react/jsx-runtime": {}, "@/lib/client": {}, "@/lib/desktop": {},
    "@/lib/controlOutcome": {}, "@/lib/usageControl": {}, "@/lib/charlieGreeting": helpers,
    "./CharlieIntro.module.css": {},
    "@/lib/mascotPlayer.js": {mountMascot: async (target, reduced, size) => {
      assert.equal(target, host);
      assert.equal(size, 320); target.appendChild(canvas);
      if (reduced) player.pause();
      return {player, dispose: () => {player.dispose(); canvas.remove();}};
    }},
  });
  for (const reduced of [false, true]) {
    calls.length = 0;
    const mounted = await mountMascot(host, reduced);
    assert.equal(mounted.player, player);
    assert.equal(calls.some(call => call[0] === "pause"), reduced);
    mounted.dispose();
    assert.deepEqual(calls.slice(-2), [["dispose"], ["remove"]]);
  }
  const source = readFileSync(path.join(__dirname, "CharlieIntro.tsx"), "utf8");
  assert.doesNotMatch(source, /GLTFLoader|["']three["']|mascot-swing/);
});

test("intro retains the approved static image if WebGL2 loading fails", async () => {
  const {mountMascot} = compiled("CharlieIntro.tsx", {
    ...mascotMocks, react: {}, "react/jsx-runtime": {}, "@/lib/client": {}, "@/lib/desktop": {},
    "@/lib/controlOutcome": {}, "@/lib/usageControl": {}, "@/lib/charlieGreeting": helpers,
    "./CharlieIntro.module.css": {},
    "@/lib/mascotPlayer.js": {mountMascot: async () => ({player: null, setState() {}, dispose() {}})},
  });
  const mounted = await mountMascot({}, false);
  assert.equal(mounted.player, null);
  assert.match(readFileSync(path.join(__dirname, 'CharlieIntro.tsx'), 'utf8'), /src="\/charlie\/poster\.png"/);
});

function harness(results = [{ok: true, status: 200, data: {text: "Hey Ada", greeted_at: null}}], readyAudio = false, firstLaunch = null, reduced = true, runtime = {}) {
  const calls = [], controls = [], timers = [], events = new Map(), mascotStates = [], playerCalls = [], audioCalls = [];
  const player = {speak: () => playerCalls.push(["speak"]), stopSpeaking: () => playerCalls.push(["stopSpeaking"]),
    setVoiceLevel: level => playerCalls.push(["level", level]), pause: () => playerCalls.push(["pause"]), dispose: () => playerCalls.push(["dispose"])};
  let cells = [], effects = [];
  const introCells = cells, hubCells = [], paths = [];
  const owner = {subject: firstLaunch ? null : "usr_ada", epoch: 1};
  let greetedAt = firstLaunch?.greetedAt ?? null;
  let cursor = 0, tree, speech, source, native = runtime.desktop ?? true, nativeEvent;
  global.setTimeout = (callback, ms) => { const timer = {callback, ms, cancelled: false}; timers.push(timer); return timer; };
  global.clearTimeout = timer => { if (timer) timer.cancelled = true; };
  global.requestAnimationFrame = () => 0;
  const react = {
    useState: initial => { const state = cells, at = cursor++; state[at] ??= initial; return [state[at], value => {state[at] = typeof value === "function" ? value(state[at]) : value;}]; },
    useRef: current => { const at = cursor++; return cells[at] ??= {current}; },
    useEffect: (run, dependencies) => {
      const at = cursor++, previous = cells[at];
      if (!previous || dependencies.some((value, index) => value !== previous.dependencies[index])) {
        effects.push(() => { previous?.cleanup?.(); cells[at] = {dependencies, cleanup: run()}; });
      }
    },
    useCallback: callback => callback, useContext: context => context.value,
  };
  const add = (name, callback) => { if (!events.has(name)) events.set(name, new Set()); events.get(name).add(callback); };
  const remove = (name, callback) => events.get(name)?.delete(callback);
  global.document = {createElement: () => ({style: {}, setAttribute() {}, remove() {}})};
  global.window = {location: {search: ""}, localStorage: {getItem: () => null}, matchMedia: () => ({matches: reduced}),
    addEventListener: add, removeEventListener: remove, dispatchEvent: event => { for (const callback of events.get(event.type) ?? []) callback(event); },
    setTimeout: global.setTimeout, speechSynthesis: {speak: value => { speech = value; }, cancel() {}},
    SpeechSynthesisUtterance: class {constructor(text) {this.text = text;}},
    AudioContext: class {state = "running"; constructor() {audioCalls.push("create");} resume() {audioCalls.push("resume"); return Promise.resolve();} close() {return Promise.resolve();}
      decodeAudioData(_bytes, resolve) {resolve({});}
      createAnalyser() {return {fftSize: 256, connect() {}, getByteTimeDomainData: data => data.fill(128)};}
      createBufferSource() {source = {connect() {}, start() {audioCalls.push("start");}, stop() {}}; return source;}
    },
  };
  const api = async (url, init) => {
    calls.push({url, body: init?.body ? JSON.parse(init.body) : null});
    if (url.endsWith("/audio")) return readyAudio ? {ok: true, status: 200, data: {audio_base64: "SUQz"}} : new Promise(() => {});
    if (url.endsWith("/done")) {
      const result = firstLaunch?.saveOutcome ? await firstLaunch.saveOutcome(JSON.parse(init.body)) : {ok: true, status: 200, data: {ok: true}};
      if (result.ok && result.data?.ok === true) greetedAt = "2026-10-15T00:00:00Z";
      return result;
    }
    if (url.endsWith("/history")) return {ok: true, status: 200, data: {items: []}};
    if (firstLaunch) return {ok: true, status: 200, data: {text: firstLaunch.text ?? "Hey Ada", greeted_at: greetedAt}};
    const result = results.shift();
    if (result instanceof Error) throw result;
    return result ?? {ok: true, status: 200, data: {text: "Hey Ada", greeted_at: null}};
  };
  const jsx = (type, props) => {
    if (props?.ref) props.ref.current ??= {style: {}, appendChild() {}};
    return {type, props};
  };
  const client = {api, ensureSession: async () => {
    if (!owner.subject) { owner.subject = "usr_ada"; ++owner.epoch; }
    return {ok: true};
  }, apiSwr: async (_url, update) => update({ok: true, data: {items: []}}),
    verifiedSessionSubject: () => owner.subject, verifiedSessionEpoch: () => owner.epoch};
  const desktop = {isDesktopShell: () => native, desktopListen: async (_name, callback) => {nativeEvent = callback; return () => {};}};
  const usage = {USAGE_CONTROL_EVENT: "usage", announceUsageControl: control => {
    controls.push(control);
    window.dispatchEvent(new CustomEvent("usage", {detail: {controlId: control}}));
  }};
  const {CharlieIntro} = compiled("CharlieIntro.tsx", {
    ...mascotMocks,
    "@/lib/mascotPlayer.js": {mountMascot: async (_host, reduce) => {
      playerCalls.push(["mount", {src: "/charlie/mascot_animations.glb", clip: "Swing_Joy", autoplay: !reduce}]);
      return {player, setState(state, level) {if (reduce) return; if (state === "speaking") player.speak(); else player.stopSpeaking(); player.setVoiceLevel(level);}, dispose: player.dispose};
    }},
    "@/lib/charlieMascot": { setCharlieMascot: (state, level) => mascotStates.push({state, level}) },
    react, "react/jsx-runtime": {jsx, jsxs: jsx},
    "@/lib/client": client,
    "@/components/layout/LocalModeBoundary": {useLocalMode: () => runtime.local ?? false},
    "@/lib/desktop": {isDesktopShell: () => native, desktopListen: async (_name, callback) => {nativeEvent = callback; return () => {};}},
    "@/lib/controlOutcome": {CONTROL_OUTCOME_EVENT: "outcome"},
    "@/lib/usageControl": usage,
    "@/lib/charlieGreeting": helpers, "./CharlieIntro.module.css": {default: {}},
  });
  const router = {replace: value => paths.push(value)};
  const hub = firstLaunch && compiled("../ventures/VentureHub.tsx", {
    react, "react/jsx-runtime": {jsx, jsxs: jsx}, "next/link": {default: "a"}, "next/navigation": {useRouter: () => router},
    "@/components/chat/NewVentureLink": {NewVentureLink: "new-link"}, "@/lib/founderUI": {FounderUIContext: {value: firstLaunch.frame}},
    "@/lib/client": client, "@/lib/desktop": desktop, "@/lib/usageControl": usage, "@/lib/charlieGreeting": helpers,
    "@/lib/chatEntry": {splitEntryHistory: () => ({fresh: []}), unsentEntryDraftHref: () => null},
    "@/lib/chatComposer": {draftStorage: () => null}, "@/lib/format": {displayName: value => value},
    "@/components/primitives/ErrorNotice": {ErrorNotice: "error"},
    "@/lib/betaCopy": compiled("../../lib/betaCopy.ts"),
    "@/lib/ventures": {}, "./ventureCards": compiled("../ventures/ventureCards.ts"),
    "./venturePortrait": {}, "./VentureHubHome.module.css": {default: {}}, "./VentureHub.module.css": {default: {}},
    "./DeleteVentureDialog": {}, "./VentureCard": {}, "./BetaPathStrip": {},
  });
  const render = () => {
    cells = introCells; cursor = 0; tree = CharlieIntro(); while (effects.length) effects.shift()();
    if (hub) { cells = hubCells; cursor = 0; hub.VentureHub(); while (effects.length) effects.shift()(); }
    return tree;
  };
  const settle = async () => { for (let count = 0; count < 5; count++) {render(); await flush();} return tree; };
  return {calls, controls, owner, timers, paths, mascotStates, playerCalls, audioCalls, render, settle, nodes: () => elements(tree), speech: () => speech, source: () => source,
    timer: async () => {const timer = timers.find(value => !value.cancelled); assert.ok(timer); timer.cancelled = true; timer.callback(); await settle();},
    dispose: () => [...introCells, ...hubCells].forEach(value => value?.cleanup?.()),
    change: async () => {window.dispatchEvent(new Event("supymem-session-changed")); await settle();},
    reconnect: async () => {window.dispatchEvent(new Event("supymem-reconnect")); await settle();},
    nativeAuth: async () => {nativeEvent?.(); await settle();},
    desktop: async value => {native = value; window.dispatchEvent(new Event("supymem-desktop-ready")); await settle();},
  };
}

test("local setup owns the silent welcome while only hosted desktop requests the spoken intro across domains", async () => {
  const {CharlieMascot} = compiled("../chat/CharlieDock.tsx", {
    react: {useRef: () => ({current: null}), useEffect() {}},
    "react/jsx-runtime": {jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})},
    "next/link": {}, "@/lib/founderUI": {}, "@/lib/charlieMascot": {}, "./CharlieDock.module.css": {default: {}},
  });
  for (const text of ["You manage industrial maintenance", "You plan family meals"]) {
    for (const local of [false, true]) for (const desktop of [false, true]) {
      const h = harness([{ok: true, status: 200, data: {text, greeted_at: null}}], true, null, true, {local, desktop});
      try {
        await h.settle();
        const enabled = desktop && !local;
        assert.equal(h.nodes().some(node => node.props?.role === "dialog"), enabled, `${text}: local=${local}, desktop=${desktop}`);
        assert.equal(h.calls.filter(call => call.url.endsWith("/audio")).length, enabled ? 1 : 0);
        assert.deepEqual(h.audioCalls, []);
        assert.ok(elements(CharlieMascot({})).some(node => node.type === "img" && node.props.src === "/charlie/poster.png"));
        if (!enabled) {
          await h.reconnect(); await h.change();
          assert.equal(h.nodes().length, 0);
          assert.deepEqual(h.calls, []);
          assert.equal(h.speech(), undefined);
        }
      } finally { h.dispose(); }
    }
  }
});

test("first-run swing is visible before one gesture reveals the greeting with speech, or keeps text and Skip when audio fails", async () => {
  for (const text of ["You help warehouses count spare parts.", "You help apartment gardeners share seedlings."]) {
    for (const reduced of [false, true]) {
      for (const audioAvailable of [true, false]) {
        const h = harness([{ok: true, status: 200, data: {text, greeted_at: null}}], true, null, reduced);
        try {
          await h.settle();
          const mount = h.playerCalls.find(call => call[0] === "mount");
          assert.equal(mount[1].src, "/charlie/mascot_animations.glb");
          assert.equal(mount[1].clip, "Swing_Joy");
          assert.equal(mount[1].autoplay, !reduced);
          assert.ok(h.nodes().some(node => node.props?.ref && node.props.style?.display !== "none" && node.props.style?.width == null));
          assert.ok(h.nodes().some(node => node.type === "img" && node.props.src === "/charlie/poster.png"));
          assert.ok(!h.nodes().some(node => node.props?.children === text));
          assert.deepEqual(h.audioCalls, []);
          if (!audioAvailable) {
            window.AudioContext = undefined;
            window.speechSynthesis = undefined;
          }
          h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_tap").props.onClick();
          if (audioAvailable) {
            assert.deepEqual(h.audioCalls, ["create", "resume"]);
            h.render();
            assert.ok(!h.nodes().some(node => node.props?.children === text));
          }
          await h.settle();
          assert.ok(h.nodes().some(node => node.type === "p" && node.props.children === text));
          assert.equal(h.audioCalls.filter(call => call === "start").length, audioAvailable ? 1 : 0);
          if (!audioAvailable) assert.ok(h.controls.includes("greeting_audio_failed"));
          h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_skip").props.onClick();
          await h.settle();
          await h.timer();
          assert.equal(h.nodes().length, 0);
          assert.equal(h.calls.filter(call => call.url.endsWith("/done")).at(-1).body.outcome, "skipped");
        } finally { h.dispose(); }
      }
    }
  }
});

test("dismissal waits for a saved outcome and lets you retry HTTP and transport failures across domains", async () => {
  for (const text of ["You manage industrial maintenance", "You plan family meals"]) {
    for (const outcome of ["skipped", "completed"]) {
      const pending = [];
      const h = harness(undefined, true, {frame: "home", text,
        saveOutcome: body => new Promise((resolve, reject) => pending.push({body, resolve, reject}))});
      try {
        await h.settle();
        const dismiss = async () => {
          if (outcome === "completed") {
            await h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_tap").props.onClick();
            await h.settle();
            h.source().onended();
          } else h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_skip").props.onClick();
          await h.settle();
        };
        const waiting = () => {
          assert.ok(h.nodes().some(node => node.props?.role === "dialog"));
          assert.deepEqual(h.paths, []);
          assert.ok(!h.controls.includes(`greeting_${outcome}`));
          assert.equal(h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_skip").props.disabled, true);
          h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_skip").props.onClick();
          assert.equal(h.calls.filter(call => call.url.endsWith("/done")).length, pending.length);
        };
        await dismiss();
        waiting();
        assert.deepEqual(pending[0].body, {outcome});
        pending[0].resolve({ok: false, status: 503});
        await h.settle();
        assert.ok(h.nodes().some(node => node.props?.source === "CharlieIntro.save" && node.props.error));
        assert.equal(h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_skip").props.disabled, false);
        await dismiss();
        waiting();
        pending[1].reject(new Error("Transport interrupted"));
        await h.settle();
        assert.ok(h.nodes().some(node => node.props?.source === "CharlieIntro.save" && node.props.error));
        await dismiss();
        waiting();
        pending[2].resolve({ok: true, status: 200, data: {ok: false}});
        await h.settle();
        assert.ok(h.nodes().some(node => node.props?.source === "CharlieIntro.save" && node.props.error));
        await dismiss();
        waiting();
        pending[3].resolve({ok: true, status: 200, data: {ok: true}});
        await h.settle();
        await h.timer();
        assert.equal(h.nodes().length, 0);
        assert.deepEqual(h.paths, ["/new"]);
        await h.reconnect(); await h.change();
        assert.equal(h.nodes().length, 0);
        assert.equal(pending.length, 4);
        assert.equal(h.controls.filter(control => control === `greeting_${outcome}`).length, 1);
      } finally { h.dispose(); }
    }
  }
});

for (const frame of ["classic", "home"]) {
  for (const greetedAt of [null, "2026-10-14T00:00:00Z"]) {
    test(`${frame}: zero ventures waits for a never-greeted founder, greeted goes straight to the idea`, async () => {
      const h = harness(undefined, false, {frame, greetedAt});
      await h.settle();
      if (greetedAt) {
        assert.equal(h.nodes().length, 0);
        assert.deepEqual(h.paths, ["/new"]);
      } else {
        assert.ok(h.nodes().some(node => node.props?.role === "dialog" && node.props["aria-label"] === "Meet Charlie"));
        assert.deepEqual(h.paths, []);
        h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_skip").props.onClick();
        await h.settle();
        await h.timer();
        assert.equal(h.nodes().length, 0);
        assert.deepEqual(h.paths, ["/new"]);
        await h.reconnect(); await h.change();
        assert.equal(h.nodes().length, 0);
        assert.equal(h.controls.filter(control => control === "greeting_shown").length, 1);
        assert.equal(h.calls.filter(call => call.url.endsWith("/done")).length, 1);
      }
      h.dispose();
    });
  }
}

test("intro caps quiet greeting retries, hides failure, and reconnect can retry", async () => {
  const h = harness([{ok: false, status: 503}, {ok: true, status: 200, data: {}}, new Error("offline")]);
  await h.settle(); assert.equal(h.nodes().length, 0);
  await h.timer(); await h.timer();
  assert.deepEqual(h.timers.map(timer => timer.ms), [1000, 2000]);
  assert.equal(h.calls.filter(call => call.url === "/api/me/greeting").length, 3);
  assert.equal(h.controls.filter(control => control === "greeting_fetch_failed").length, 1);
  assert.ok(!h.controls.includes("greeting_suppressed_greeted"));
  assert.equal(h.nodes().length, 0);
  await h.reconnect();
  assert.ok(h.nodes().find(node => node.type === "button" && node.props.children === "Tap to meet Charlie"));
  assert.ok(h.controls.includes("greeting_painted"));
  assert.equal(h.calls.filter(call => call.url.endsWith("/audio")).length, 1);
  h.dispose();
});

test("permanent greeting rejection hides intro without polling", async () => {
  const h = harness([{ok: false, status: 403}]);
  await h.settle();
  assert.equal(h.nodes().length, 0);
  assert.equal(h.timers.length, 0);
  assert.deepEqual(h.controls.filter(control => control === "greeting_fetch_failed"), ["greeting_fetch_failed"]);
  h.dispose();
});

test("prefetched WebAudio completes only after playback ends and never duplicates the fetch", async () => {
  const h = harness(undefined, true, null, false); await h.settle();
  h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_tap").props.onClick();
  await h.settle();
  assert.ok(h.source()); assert.equal(h.calls.some(call => call.url.endsWith("/done")), false);
  assert.equal(h.calls.filter(call => call.url.endsWith("/audio")).length, 1);
  assert.ok(h.mascotStates.some(value => value.state === "speaking" && value.level === 0));
  assert.ok(h.playerCalls.some(call => call[0] === "speak"));
  assert.ok(h.playerCalls.some(call => call[0] === "level" && call[1] === 0));
  h.source().onended(); await h.settle();
  assert.equal(h.mascotStates.at(-1).state, "idle");
  assert.deepEqual(h.playerCalls.at(-1), ["level", null]);
  assert.ok(h.playerCalls.some(call => call[0] === "stopSpeaking"));
  assert.equal(h.calls.filter(call => call.url.endsWith("/done")).at(-1).body.outcome, "completed");
  h.dispose();
  assert.deepEqual(h.playerCalls.at(-1), ["dispose"]);
});

test("a late greeting from a retired session cannot replace the current account intro", async () => {
  let resolve;
  const h = harness([new Promise(done => {resolve = done;})]); await h.settle();
  h.owner.subject = "usr_bea"; ++h.owner.epoch; await h.change();
  resolve({ok: true, status: 200, data: {text: "Retired greeting", greeted_at: null}}); await h.settle();
  assert.equal(h.controls.filter(control => control === "greeting_shown").length, 1);
  assert.ok(h.nodes().find(node => node.props?.children === "Tap to meet Charlie"));
  h.dispose();
});

test("native auth and reconnect re-read suppression; a late bridge enables the intro", async () => {
  const greeted = {ok: true, status: 200, data: {text: "Hey Ada", greeted_at: "2026-10-09"}};
  const h = harness([greeted, greeted]); await h.settle();
  assert.equal(h.nodes().length, 0); assert.ok(h.controls.includes("greeting_suppressed_greeted"));
  await h.nativeAuth(); assert.equal(h.calls.filter(call => call.url === "/api/me/greeting").length, 2);
  await h.reconnect(); assert.ok(h.nodes().find(node => node.props?.children === "Tap to meet Charlie"));
  const requestsBeforeBrowser = h.calls.length;
  await h.desktop(false); assert.equal(h.nodes().length, 0);
  await h.reconnect(); assert.equal(h.calls.length, requestsBeforeBrowser);
  await h.desktop(true); assert.ok(h.nodes().find(node => node.props?.children === "Tap to meet Charlie"));
  h.dispose();
});

test("audio timeout falls back without consuming intro; speech errors keep retry and skip is explicit", async () => {
  const h = harness(); await h.settle();
  h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_tap").props.onClick();
  await h.settle();
  assert.ok(h.nodes().some(node => node.props?.children === "Charlie is getting ready…"));
  await h.timer(); assert.ok(h.speech());
  assert.ok(h.calls.filter(call => call.url.endsWith("/done")).every(call => call.body.outcome === "failed"));
  h.speech().onerror(); await h.settle();
  assert.ok(h.nodes().find(node => node.props?.children === "Tap to hear Charlie"));
  assert.ok(h.calls.filter(call => call.url.endsWith("/done")).every(call => call.body.outcome === "failed"));
  h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_skip").props.onClick();
  await h.settle();
  assert.equal(h.calls.filter(call => call.url.endsWith("/done")).at(-1).body.outcome, "skipped");
  h.dispose();
});

test("only successful speech completion marks completed and session switches retry for the new owner", async () => {
  const h = harness(); await h.settle();
  h.nodes().find(node => node.props?.["data-usage-control"] === "greeting_tap").props.onClick();
  await h.timer(); h.speech().onstart(); h.speech().onend(); await h.settle();
  assert.equal(h.calls.filter(call => call.url.endsWith("/done")).at(-1).body.outcome, "completed");
  h.owner.subject = "usr_bea"; ++h.owner.epoch; await h.change();
  assert.equal(h.calls.filter(call => call.url === "/api/me/greeting").length, 2);
  assert.ok(h.nodes().find(node => node.props?.children === "Tap to meet Charlie"));
  h.dispose();
});
