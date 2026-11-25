const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const source = readFileSync(join(__dirname, "FounderCoaching.tsx"), "utf8");
const code = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
} }).outputText;
const safeUrl = value => { try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined; } catch {} };
const card = { id: "case-a", company: "Airbnb", stage: "Problem discovery", situation_at_this_stage: "Check listing quality",
  what_they_did: "Photographed listings", why_it_worked: "Observed customers directly", do: "Test with one host",
  avoid: "Avoid: Generalizing from one city", metric: "Qualified commitments", honesty: "Their result isn't proof yours will work — test it.",
  sources: [{ title: "Do Things That Don't Scale", url: "https://paulgraham.com/ds.html" }],
  yc_says: { text: "Founders are often afraid of pricing conversations.", title: "Talk about price", url: "https://www.youtube.com/watch?v=real" } };

function harness(realReact = false, saved = new Map()) {
  let cursor = 0, owner = "owner-a", epoch = 1, pathname = "/v/ven_a";
  const hooks = [], effects = [], requests = [], listeners = new Map(), timers = new Set();
  const react = realReact ? React : {
    useState(initial) { const index = cursor++; hooks[index] ||= { value: initial }; return [hooks[index].value, value => { hooks[index].value = typeof value === "function" ? value(hooks[index].value) : value; }]; },
    useRef(initial) { return hooks[cursor++] ||= { current: initial }; },
    useEffect(callback, deps) { const index = cursor++, before = hooks[index];
      if (!before || deps.some((value, position) => value !== before.deps[position])) effects.push(() => {
        before?.cleanup?.(); hooks[index] = { deps, cleanup: callback() };
      });
    },
  };
  const jsx = (type, props) => ({ type, props });
  const exports = {};
  vm.runInNewContext(code, { exports, URL, document: { hidden: false }, localStorage: {
    getItem: key => saved.get(key) || null, setItem: (key, value) => saved.set(key, value),
  }, setInterval: callback => { timers.add(callback); return callback; }, clearInterval: callback => timers.delete(callback),
  window: { addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: name => listeners.delete(name) },
  require(id) {
    if (id === "react") return react;
    if (id === "react/jsx-runtime") return realReact ? require(id) : { jsx, jsxs: jsx };
    if (id === "next/navigation") return { usePathname: () => pathname, useSearchParams: () => new URLSearchParams() };
    if (id === "./CaseReferences") return { safeUrl };
    if (id.endsWith(".css")) return { default: {} };
    if (id === "@/lib/client") return { ensureSession: async () => {}, verifiedSessionSubject: () => owner,
      verifiedSessionEpoch: () => epoch, api: (path, init) => new Promise(resolve => requests.push({ path, init, resolve })) };
    throw new Error(id);
  } });
  return { exports, saved, requests, render(ventureId = "ven_a") {
    cursor = 0; effects.length = 0;
    const view = exports.FounderCoaching({ ventureId }); effects.forEach(callback => callback()); return view;
  }, finish(items = [card], stage = card.stage, request = requests.at(-1)) {
    request.resolve({ ok: true, data: { venture_id: "ven_a", stage, items } });
  }, fail() { requests.at(-1).resolve({ ok: false }); }, retire() { owner = "owner-b"; epoch++; },
  route(path) { pathname = path; }, tick() { for (const timer of timers) timer(); },
  unmount() { for (const hook of hooks) hook.cleanup?.(); },
  change() { listeners.get("supymem-venture-changed")?.({ detail: { ventureId: "ven_a" } }); } };
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test("card renders coaching fields, safe sources, quote, controls and states", () => {
  const { CoachingView } = harness(true).exports;
  const render = props => renderToStaticMarkup(React.createElement(CoachingView, { onMore() {}, onDismiss() {}, ...props }));
  const html = render({ item: card });
  for (const text of ["From founders who were here", "Case study", "What they did", "Why it worked", "Try this", "Avoid", "Measure", "YC says:", "Dismiss this card", "More like this"]) assert.ok(html.includes(text), text);
  assert.match(html, /<dt>Try this<\/dt><dd>Test with one host<\/dd>/);
  assert.match(html, /<dt>Avoid<\/dt><dd>Generalizing from one city<\/dd>/);
  assert.match(html, /<blockquote[^>]*>.*<cite>/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(render({ loading: true }), /role="status"/);
  assert.match(render({ error: true }), /coaching_retry/);
  assert.equal(render({}), "");
  const unsafe = render({ item: { ...card, sources: [{ title: "bad", url: "javascript:bad()" }], yc_says: { ...card.yc_says, url: "javascript:bad()" } } });
  assert.ok(!unsafe.includes("javascript:") && !unsafe.includes("YC says:"));
});

test("coaching omits unknown explanation, guardrail and measurement rows", () => {
  const { CoachingView } = harness(true).exports;
  const html = renderToStaticMarkup(React.createElement(CoachingView, {
    item: { ...card, why_it_worked: null, avoid: null, metric: null }, onMore() {}, onDismiss() {},
  }));
  assert.doesNotMatch(html, /<dt>(Why it worked|Avoid|Measure)<\/dt>/);
  assert.match(html, /Photographed listings/);
});

test("coaching removes generated step prefixes without changing unprefixed advice", () => {
  const { CoachingView } = harness(true).exports;
  for (const advice of ["For Your company work is in progress: Test with one host", "For Problem discovery: Test with one host", "Test with one host"]) {
    const html = renderToStaticMarkup(React.createElement(CoachingView, { item: { ...card, do: advice }, onMore() {}, onDismiss() {} }));
    assert.match(html, /<dt>Try this<\/dt><dd>Test with one host<\/dd>/);
    assert.ok(!html.includes("For Your company work is in progress:"));
  }
});

test("proactive loading, more, persisted dismissal and stage refresh", async () => {
  const h = harness(); h.render(); await settle(); h.finish([card, { ...card, id: "case-b", company: "Dropbox" }]); await settle();
  let view = h.render(); assert.equal(view.props.item.company, "Airbnb");
  assert.equal(h.requests[0].init.cache, "no-store");
  view.props.onMore(); view = h.render(); assert.equal(view.props.item.company, "Dropbox");
  view.props.onDismiss(); view = h.render(); assert.equal(view.props.item.company, "Airbnb");
  const key = h.exports.coachingDismissalKey("owner-a", "ven_a", card.stage);
  assert.deepEqual(JSON.parse(h.saved.get(key)), ["case-b"]);
  h.unmount();
  const remount = harness(false, h.saved); remount.render(); await settle(); remount.finish([card, { ...card, id: "case-b" }]); await settle();
  view = remount.render(); view.props.onDismiss(); assert.equal(remount.render().props.item, undefined);
  remount.change(); await settle(); remount.finish([{ ...card, stage: "MVP and delivery" }], "MVP and delivery"); await settle();
  assert.equal(remount.render().props.item.id, "case-a");
  remount.unmount();
});

test("errors retry, abstention hides, and retired account responses and controls are inert", async () => {
  const h = harness(); h.render(); await settle(); h.fail(); await settle();
  let view = h.render(); assert.equal(view.props.error, true); view.props.onMore(); h.render(); await settle();
  h.finish([]); await settle(); assert.equal(h.render().props.item, undefined);
  h.tick(); await settle(); h.finish(); await settle(); view = h.render(); assert.equal(view.props.item.id, "case-a");
  h.retire(); view.props.onDismiss(); assert.equal(h.saved.size, 0);
  assert.equal(h.render().props.item, undefined);
  h.unmount();
  const pending = harness(); pending.render(); await settle(); pending.retire(); pending.finish(); await settle();
  assert.equal(pending.render().props.item, undefined); pending.unmount();
});

test("only Overview shows coaching; other surfaces do not load or repeat it", async () => {
  const h = harness(); h.route("/v/ven_a"); h.render(); await settle();
  h.finish(); await settle(); assert.equal(h.render().props.item.id, "case-a");
  h.tick(); await settle(); assert.equal(h.requests.length, 2); h.finish(); await settle();
  const reads = h.requests.length;
  for (const path of ["site", "research", "plan", "charter", "now", "settings"]) {
    h.route(`/v/ven_a/${path}`); assert.equal(h.render(), null); await settle();
    assert.equal(h.requests.length, reads);
  }
  h.route("/gtm"); assert.equal(h.render(), null); h.unmount();
  const shell = readFileSync(join(__dirname, "../layout/AppShell.tsx"), "utf8");
  const customers = readFileSync(join(__dirname, "../gtm/GtmGod.tsx"), "utf8");
  assert.match(shell, /pathname !== "\/gtm" && <FounderCoaching/);
  assert.match(customers, /owner && <FounderCoaching/);
});
