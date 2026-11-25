const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const jsx = (type, props) => ({ type, props: props || {} });
const nodes = node => Array.isArray(node) ? node.flatMap(nodes) : node && typeof node === "object"
  ? [node, ...nodes(node.props?.children)] : [];
const text = node => Array.isArray(node) ? node.map(text).join("") : node && typeof node === "object"
  ? text(node.props?.children) : node == null ? "" : String(node);
const flush = () => new Promise(resolve => setImmediate(resolve));

test("Home reconnect is scoped, uses the existing OAuth flow and disappears for two unrelated domains", async () => {
  const code = ts.transpileModule(readFileSync(join(__dirname, "GmailReconnectBanner.tsx"), "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  for (const [ventureId, workspaceId] of [["stock-planners", "dispatch-teams"], ["trail-journals", "weekend-hikers"]]) {
    const exports = {}, hooks = [], effects = [], connected = [], navigated = [], reads = [];
    let cursor = 0, needsReconnect = true, focus, owner = "you";
    vm.runInNewContext(code, { exports, URL, require(name) {
      if (name === "react") return {
        useRef(initial) { const index = cursor++; return hooks[index] ||= { current: initial }; },
        useState(initial) { const index = cursor++; if (!(index in hooks)) hooks[index] = initial;
          return [hooks[index], value => { hooks[index] = value; }]; },
        useEffect(callback) { effects.push(callback); },
      };
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name === "@/lib/client") return { verifiedSessionSubject: () => owner, verifiedSessionEpoch: () => 1,
        api: async path => { reads.push(path); return { ok: true, data: { needs_reconnect: needsReconnect } }; } };
      if (name === "@/lib/opsApi") return { connectHubProvider: async (...args) => {
        connected.push(args); return { redirect_url: "https://connect.example/authorize" };
      } };
      if (name === "@/lib/desktop") return { isDesktopShell: () => false, desktopInvokeOrThrow: async () => assert.fail("Web reconnect uses the browser") };
      if (name === "@/components/primitives/ErrorNotice") return { ErrorNotice: "error-notice" };
      throw new Error(name);
    }, window: { addEventListener(event, callback) { assert.equal(event, "focus"); focus = callback; },
      removeEventListener() {}, location: { assign(url) { navigated.push(url); } } } });
    const render = () => { cursor = 0; return exports.GmailReconnectBanner({ ventureId, workspaceId }); };
    assert.equal(render(), null);
    const dispose = effects.shift()();
    await flush();
    let tree = render();
    assert.ok(text(tree).includes("Reconnect your Gmail to keep your emails going"));
    assert.equal(nodes(tree).find(node => node.type === "section").props.role, "status");
    const button = nodes(tree).find(node => node.type === "button");
    assert.equal(text(button), "Reconnect");
    button.props.onClick();
    await flush();
    assert.equal(JSON.stringify(connected), JSON.stringify([["gmail", { return_to: `/v/${ventureId}` }, workspaceId]]));
    assert.deepEqual(navigated, ["https://connect.example/authorize"]);
    assert.ok(reads.every(path => path.includes(encodeURIComponent(workspaceId)) && path.includes("/connections/gmail/status")));
    owner = "another-founder";
    assert.equal(render(), null);
    owner = "you";
    render();
    needsReconnect = false;
    await focus();
    assert.equal(render(), null);
    dispose();
  }
  const page = readFileSync(join(__dirname, "../../app/v/[ventureId]/page.tsx"), "utf8");
  assert.equal((page.match(/<GmailReconnectBanner /g) || []).length, 1);
  const home = readFileSync(join(__dirname, "../office/CompanyHome.tsx"), "utf8");
  assert.match(home, /tab === "home"[\s\S]*?<GmailReconnectBanner workspaceId=\{data.workspaceId\}/);
});
