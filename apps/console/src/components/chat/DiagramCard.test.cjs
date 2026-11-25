const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function compile(filename, context) {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, { exports, ...context });
  return exports;
}
const { sanitizeDiagramSvg } = compile(path.join(__dirname, "../../lib/diagramSanitize.ts"), { TextEncoder });
const jsx = (type, props) => ({ type, props });
const elements = node => !node || typeof node !== "object" ? [] : [node, ...[node.props?.children].flat().flatMap(elements)];

function card(overrides = {}, environment = {}) {
  const states = [], refs = [], effects = [], pending = [], messages = [], downloads = [], revoked = [], timers = [];
  const listeners = new Map(), preferenceListeners = new Map();
  const contentWindow = {};
  let hook = 0, themeAttribute = null, mutation, disconnected = false;
  const preference = { matches: false, addEventListener: (name, listener) => preferenceListeners.set(name, listener), removeEventListener: name => preferenceListeners.delete(name) };
  const document = {
    documentElement: { getAttribute: () => themeAttribute },
    body: { appendChild() {} },
    createElement: name => {
      assert.equal(name, "a");
      return { click() { downloads.push({ href: this.href, filename: this.download }); }, remove() {} };
    },
  };
  const url = { createObjectURL: blob => { downloads.push({ blob }); return "blob:diagram"; }, revokeObjectURL: value => revoked.push(value) };
  const props = { title: "sales_pipeline", svg: '<svg viewBox="0 0 320 160"><g class="node c-purple" onclick="sendPrompt(\'Explain sales\')"><rect width="80"/><text class="th">Sales</text></g></svg>', onPrompt: text => messages.push(text), ...overrides };
  const component = compile(path.join(__dirname, "DiagramCard.tsx"), {
    require(name) {
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name === "./DiagramCard.module.css") return { default: {} };
      if (name === "@/lib/diagramSanitize") return { sanitizeDiagramSvg };
      if (name === "react") return {
        useId: () => "diagram-test",
        useMemo: callback => callback(),
        useRef: value => { const index = hook++; return refs[index] ||= { current: value }; },
        useState: initial => {
          const index = hook++;
          if (!(index in states)) states[index] = initial;
          return [states[index], value => { states[index] = value; }];
        },
        useEffect: (callback, dependencies) => {
          const index = hook++;
          if (!effects[index] || dependencies.some((value, position) => value !== effects[index].dependencies[position])) pending.push(() => {
            effects[index]?.cleanup?.();
            effects[index] = { dependencies, cleanup: callback() };
          });
        },
      };
      throw new Error(name);
    },
    window: {
      matchMedia: () => preference,
      addEventListener: (name, listener) => listeners.set(name, listener),
      removeEventListener: name => listeners.delete(name),
      setTimeout: callback => timers.push(callback),
    },
    MutationObserver: class { constructor(callback) { mutation = callback; } observe() {} disconnect() { disconnected = true; } },
    document, Blob, URL: url,
    ...environment,
  });
  const render = () => {
    hook = 0;
    const tree = component.DiagramCard(props);
    const frame = elements(tree).find(node => node.type === "iframe");
    if (frame) frame.props.ref.current ||= { contentWindow, clientWidth: 320 };
    return tree;
  };
  const flush = () => { while (pending.length) pending.shift()(); };
  const send = (data, source = contentWindow) => listeners.get("message")?.({ source, data: { source: "supymem-diagram", id: "diagram-test", ...data } });
  return { render, flush, send, component, props, contentWindow, messages, downloads, revoked, timers, listeners, preference,
    setTheme: value => { themeAttribute = value; mutation(); },
    setPreference: value => { preference.matches = value; preferenceListeners.get("change")(); },
    cleanup: () => effects.forEach(effect => effect?.cleanup?.()),
    isDisconnected: () => disconnected, preferenceListeners };
}
const frame = tree => elements(tree).find(node => node.type === "iframe");
const button = (tree, label) => elements(tree).find(node => node.props["aria-label"] === label);

test("renders an accessible full-width sandboxed card with a CSP and themed diagram classes", () => {
  const harness = card(), tree = harness.render(), iframe = frame(tree);
  assert.equal(elements(tree).find(node => node.type === "h3").props.children, "Sales pipeline");
  assert.equal(iframe.props.sandbox, "allow-scripts");
  assert.equal(iframe.props.scrolling, "no");
  assert.equal(iframe.props.title, "Sales pipeline diagram");
  assert.match(iframe.props.srcDoc, /default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:/);
  assert.doesNotMatch(iframe.props.srcDoc, /allow-same-origin/);
  for (const name of ["text-primary", "text-secondary", "border", "surface-1", "c-purple", "c-teal", "c-coral", "c-gray", "c-blue", "c-amber"]) assert.ok(iframe.props.srcDoc.includes(name), name);
  assert.match(iframe.props.srcDoc, /font-size:14px; font-weight:400/);
  assert.match(iframe.props.srcDoc, /\.th \{ font-weight:500/);
  assert.match(iframe.props.srcDoc, /\.node:hover \{ opacity:\.85/);
  assert.ok(button(tree, "Download SVG") && button(tree, "Download PNG"));
  assert.match(readFileSync(path.join(__dirname, "DiagramCard.module.css"), "utf8"), /\.card \{ width: 100%/);
});

test("invalid SVG renders nothing and the message-part helper rejects other part shapes", () => {
  const harness = card({ svg: "<script>alert(1)</script>" });
  assert.equal(harness.render(), null);
  harness.flush();
  assert.equal(harness.listeners.size, 0);
  for (const part of [null, "diagram", {}, { type: "text", title: "x", svg: "<svg/>" }, { type: "diagram", title: 4, svg: "<svg/>" }]) assert.equal(harness.component.renderDiagramPart(part, () => {}), null);
  assert.equal(harness.component.renderDiagramPart({ type: "diagram", title: "x", svg: "<svg/>" }, () => {}).type, harness.component.DiagramCard);
});

test("rejected lab SVGs still get security sanitisation before sandboxed rendering", () => {
  const harness = card({ svg: '<svg viewBox="0 0 680 300"><script>private()</script><foreignObject>foreign</foreignObject><g onmouseover="bad()"><rect x="40" width="200"/><rect x="50" width="200"/></g><use href="https://evil.test/asset"/></svg>' });
  const tree = harness.render();
  assert.ok(frame(tree));
  assert.doesNotMatch(frame(tree).props.srcDoc, /private\(\)|foreignObject|onmouseover|evil\.test/);
  assert.equal(frame(tree).props.sandbox, "allow-scripts");
});

test("messages require the iframe window, source marker and id, and prompts are trimmed and bounded", () => {
  const harness = card();
  harness.render(); harness.flush();
  harness.send({ type: "prompt", text: "bad" }, {});
  harness.send({ type: "prompt", text: "bad" }, null);
  harness.send({ source: "other", type: "prompt", text: "bad" });
  harness.send({ id: "other", type: "prompt", text: "bad" });
  harness.send({ type: "prompt", text: 4 });
  harness.send({ type: "prompt", text: "   " });
  assert.deepEqual(harness.messages, []);
  harness.send({ type: "prompt", text: "  Explain sales  " });
  harness.send({ type: "prompt", text: "x".repeat(300) });
  assert.deepEqual(harness.messages, ["Explain sales", "x".repeat(200)]);
  harness.send({ type: "height", h: 246.2 });
  assert.equal(frame(harness.render()).props.style.height, 247);
  for (const height of [NaN, Infinity, -1, 0, "50", 40000]) harness.send({ type: "height", h: height });
  assert.equal(frame(harness.render()).props.style.height, 247);
  harness.props.onPrompt = () => {};
  harness.render(); harness.flush();
  assert.equal(frame(harness.render()).props.style.height, 247);
  harness.cleanup();
  assert.equal(harness.listeners.size, 0);
});

test("the iframe bridge posts prompts/heights and adds keyboard-accessible interactions", () => {
  const harness = card(), messages = [], attributes = new Map(), keyboard = new Map(), clicks = [];
  let resize;
  const svg = { getBoundingClientRect: () => ({ height: 123.4 }) };
  const node = { textContent: "Sales", setAttribute: (key, value) => attributes.set(key, value), getAttribute: key => attributes.get(key), addEventListener: (name, listener) => keyboard.set(name, listener), dispatchEvent: event => clicks.push(event) };
  const window = {};
  const script = frame(harness.render()).props.srcDoc.match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInNewContext(script, { window, parent: { postMessage: (data, target) => messages.push({ ...data, target }) },
    document: { querySelector: () => svg, querySelectorAll: () => [node] },
    ResizeObserver: class { constructor(callback) { resize = callback; } observe(target) { assert.equal(target, svg); } },
    MouseEvent: class { constructor(type) { this.type = type; } },
  });
  assert.deepEqual(messages[0], { source: "supymem-diagram", id: "diagram-test", type: "height", h: 124, target: "*" });
  resize();
  window.sendPrompt("Explain sales");
  assert.deepEqual(messages.at(-1), { source: "supymem-diagram", id: "diagram-test", type: "prompt", text: "Explain sales", target: "*" });
  assert.equal(attributes.get("role"), "button");
  assert.equal(attributes.get("tabindex"), "0");
  assert.equal(attributes.get("aria-label"), "Sales");
  let prevented = 0;
  for (const key of ["Enter", " ", "Escape"]) keyboard.get("keydown")({ key, preventDefault: () => prevented++ });
  assert.equal(prevented, 2);
  assert.equal(clicks.length, 2);
});

test("SVG download contains only sanitized SVG, uses the title filename and releases its URL", async () => {
  const harness = card({ svg: '<svg><script>alert(1)</script><rect/></svg>' });
  button(harness.render(), "Download SVG").props.onClick();
  assert.equal(harness.downloads[0].blob.type, "image/svg+xml");
  assert.match(await harness.downloads[0].blob.text(), /<rect\/>/);
  assert.doesNotMatch(await harness.downloads[0].blob.text(), /script|alert/);
  assert.equal(harness.downloads[1].filename, "sales_pipeline.svg");
  harness.timers.forEach(callback => callback());
  assert.deepEqual(harness.revoked, ["blob:diagram"]);
});

function pngCard(failure) {
  const attributes = new Map([["viewBox", "0 0 320 160"]]), embedded = [], images = [], draws = [], downloads = [];
  const root = { getAttribute: key => attributes.get(key), setAttribute: (key, value) => attributes.set(key, value), prepend: style => embedded.push(style) };
  const canvas = { getContext: () => failure === "canvas" ? null : { drawImage: (...args) => draws.push(args) }, toBlob: callback => callback(failure === "blob" ? null : new Blob(["png"], { type: "image/png" })) };
  const harness = card({}, {
    document: {
      documentElement: { getAttribute: () => "dark" },
      body: { appendChild() {} },
      createElement: name => name === "canvas" ? canvas : { click() { downloads.push(this.download); }, remove() {} },
      createElementNS: () => ({}),
    },
    DOMParser: class { parseFromString() { if (failure === "parser") throw new Error("private details"); return { documentElement: root }; } },
    XMLSerializer: class { serializeToString() { return `<svg data-theme="${attributes.get("data-theme")}" width="320" height="160"><style>${embedded[0].textContent}</style></svg>`; } },
    Image: class { set src(value) { images.push(value); if (failure === "image") this.onerror(); else this.onload(); } },
    URL: { createObjectURL: () => "blob:png", revokeObjectURL() {} },
  });
  return { harness, attributes, embedded, images, draws, canvas, downloads };
}

test("PNG embeds the current themed stylesheet, loads a data URL and draws at 2x", async () => {
  const { harness, attributes, embedded, images, draws, canvas, downloads } = pngCard();
  harness.render(); harness.flush();
  await button(harness.render(), "Download PNG").props.onClick();
  assert.equal(attributes.get("data-theme"), "light");
  assert.match(embedded[0].textContent, /\.c-purple/);
  assert.match(images[0], /^data:image\/svg\+xml;charset=utf-8,/);
  assert.match(decodeURIComponent(images[0]), /<style>/);
  assert.equal(canvas.width, 640);
  assert.equal(canvas.height, 320);
  assert.deepEqual(draws[0].slice(1), [0, 0, 640, 320]);
  assert.deepEqual(downloads, ["sales_pipeline.png"]);
  assert.equal(button(harness.render(), "Download PNG").props.disabled, false);
});

test("PNG rejects oversized dimensions before allocating a canvas or loading an image", async () => {
  const { harness, attributes, images, draws } = pngCard();
  attributes.set("viewBox", "0 0 100000 100000");
  await button(harness.render(), "Download PNG").props.onClick();
  assert.deepEqual(images, []);
  assert.deepEqual(draws, []);
  assert.equal(elements(harness.render()).find(node => node.props.role === "status").props.children, "PNG download is unavailable. Please try Download SVG.");
});

test("export failures show friendly recovery text without raw errors and re-enable PNG", async () => {
  for (const failure of ["parser", "image", "canvas", "blob"]) {
    const { harness } = pngCard(failure);
    await button(harness.render(), "Download PNG").props.onClick();
    const tree = harness.render();
    assert.equal(elements(tree).find(node => node.props.role === "status").props.children, "PNG download is unavailable. Please try Download SVG.");
    assert.equal(button(tree, "Download PNG").props.disabled, false);
  }
  const harness = card({}, { URL: { createObjectURL() { throw new Error("private details"); } } });
  button(harness.render(), "Download SVG").props.onClick();
  assert.equal(elements(harness.render()).find(node => node.props.role === "status").props.children, "Download is unavailable. Please try again.");
});
