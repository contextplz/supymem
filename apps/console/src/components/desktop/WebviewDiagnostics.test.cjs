const assert = require("node:assert/strict");
const test = require("node:test");
const { installWebviewDiagnostics, recordWebviewError } = require("../../lib/webviewDiagnostics.ts");

function desktop(domain) {
  const original = { window: global.window, document: global.document };
  const calls = [];
  let now = 0;
  let scan;
  let main = [];
  let gates = [];
  const target = Object.assign(new EventTarget(), {
    location: { pathname: "/new", search: `?venture=${domain}` },
    performance: { now: () => now },
    setInterval: callback => { scan = callback; return 1; },
    clearInterval: () => { scan = undefined; },
    getComputedStyle: () => ({ visibility: "visible", opacity: "1" }),
  });
  Object.assign(global, { window: target, document: {
    querySelectorAll: selector => selector === "[data-webview-pending]" ? gates : selector.startsWith("[data-webview-main]") ? [] : main,
  } });
  return {
    target, calls,
    connect() { target.__TAURI_INTERNALS__ = { invoke: (command, args) => { calls.push({ command, ...args.record }); return Promise.resolve(); } }; },
    tick(time) { now = time; scan?.(); },
    render(content, pending) {
      const element = (innerText, gate) => ({ innerText, dataset: { webviewPending: gate }, closest: () => null,
        getClientRects: () => [{}], querySelectorAll: () => [], contains: () => false });
      gates = pending ? [element("Loading", pending)] : [];
      main = content === null ? gates : [element(content)];
    },
    restore() { Object.assign(global, original); },
  };
}

test("desktop capture keeps only safe error details and limits accepted records to twenty per rolling minute", () => {
  for (const domain of ["procurement.example", "gardening.example"]) {
    const { target, calls, connect, tick, restore } = desktop(domain);
    let stop = installWebviewDiagnostics();
    const failure = new TypeError(`password=owner@${domain}`);
    failure.stack = `TypeError: ${failure.message}\nfn@http://127.0.0.1:3100/_next/static/chunks/123-ab12cd34ef56ab78.js:73:5`;
    const emit = (type, detail) => target.dispatchEvent(Object.assign(new Event(type), detail));
    try {
      emit("error", { error: failure });
      recordWebviewError("render_error", failure);
      assert.equal(calls.length, 0);
      stop();
      connect();
      stop = installWebviewDiagnostics();
      emit("error", { error: failure });
      emit("unhandledrejection", { reason: failure.message });
      recordWebviewError("render_error", failure);
      assert.deepEqual(calls.map(call => call.kind), ["runtime_error", "promise_error", "render_error"]);
      assert.equal(calls[0].command, "append_webview_error");
      assert.deepEqual(calls[0].source, { chunk: "ab12cd34ef56ab78", line: 73, column: 5 });
      assert.equal(calls[0].errorType, "TypeError");
      assert.equal(calls[1].text, "Promise failure");
      assert.doesNotMatch(JSON.stringify(calls), /password|127\.0\.0\.1|owner@/);
      assert.ok(!JSON.stringify(calls).includes(domain));
      for (let index = 0; index < 22; index++) emit("error", { error: failure });
      assert.equal(calls.length, 19);
      tick(15_000);
      assert.equal(calls[19].kind, "blank_render");
      tick(59_999);
      emit("error", { error: failure });
      assert.equal(calls.length, 20);
      tick(60_000);
      emit("error", { error: failure });
      assert.equal(calls.length, 21);
      stop();
      emit("error", { error: failure });
      assert.equal(calls.length, 21);
    } finally { stop(); restore(); }
  }
});

test("blank render records the safe route and pending gates once per navigation", () => {
  for (const domain of ["logistics.example", "cycling.example"]) {
    const { target, calls, connect, tick, render, restore } = desktop(domain);
    connect();
    const stop = installWebviewDiagnostics();
    try {
      render(null, "local_capabilities");
      tick(14_999);
      assert.equal(calls.length, 0);
      tick(15_000);
      tick(30_000);
      target.location.pathname = `/v/${domain}`;
      tick(30_001);
      render(null, "conversation_load");
      tick(45_001);
      target.location.search = "?conversation=private";
      tick(45_002);
      render("Your chat is ready", undefined);
      tick(60_002);
      assert.deepEqual(calls.map(({ kind, route, pending }) => ({ kind, route, pending })), [
        { kind: "blank_render", route: "/new", pending: ["local_capabilities"] },
        { kind: "blank_render", route: "/v/{id}", pending: ["conversation_load"] },
      ]);
    } finally { stop(); restore(); }
  }
});
