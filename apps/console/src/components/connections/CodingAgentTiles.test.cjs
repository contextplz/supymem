const assert = require("node:assert/strict");
const test = require("node:test");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const nodes = node => !node || typeof node !== "object" ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
const jsx = (type, props, key) => typeof type === "function" ? type(props) : React.createElement(type, key === undefined ? props : { ...props, key });
const jsxs = (type, { children, ...props }, key) => React.createElement(type, key === undefined ? props : { ...props, key }, ...children);
const flush = () => new Promise(resolve => setImmediate(resolve));
const tool = (name, fields) => ({ name, label: name, kind: "ide", installed: true, connected: false, signed_in: null,
  coding_available: false, app_path: null, cli_path: `/usr/local/bin/${name}`, ...fields });

test('signed-in tools without build support offer actionable guidance instead of builds', async () => {
  for (const subject of ['usr_couriers', 'usr_gardeners']) {
    const view = await load({ subject, desktop: false, local: true, ui: async path =>
      path === '/api/local-tools/coding-agents' ? { ok: true, data: { tools: ['claude', 'codex', 'cursor', 'opencode'].map(
        name => tool(name, { signed_in: true, coding_available: false })) } }
        : { ok: true, data: { coding_agents: { consent: {} } } } });
    for (const name of ['Cursor']) {
      const article = nodes(view.render()).find(node => node.type === 'article' && node.props['aria-label'] === name);
      const html = renderToStaticMarkup(article);
      assert.match(html, /Builds unavailable/);
      assert.match(html, /Settings &gt; Keys and connections &gt; Coding tools/);
      assert.match(html, /Update Cursor.*Check connection/);
      assert.doesNotMatch(html, /Sign in needed|Ready/);
      assert.equal(nodes(article).find(node => node.type === 'input').props.disabled, true);
      await nodes(article).find(node => node.type === 'button').props.onClick();
      const refreshed = nodes(view.render()).find(node => node.type === 'article' && node.props['aria-label'] === name);
      const refreshedHtml = renderToStaticMarkup(refreshed);
      assert.doesNotMatch(refreshedHtml, /How to sign in below/);
    }
  }
});

test('Cursor and OpenCode with verified connections offer requested builds', async () => {
  for (const subject of ['usr_couriers', 'usr_gardeners']) {
    const view = await load({ subject, desktop: false, local: true, ui: async path =>
      path === '/api/local-tools/coding-agents' ? { ok: true, data: { tools: ['claude', 'codex', 'cursor', 'opencode'].map(
        name => tool(name, { signed_in: true, coding_available: ['cursor', 'opencode'].includes(name) })) } }
        : { ok: true, data: { coding_agents: { consent: {} } } } });
    for (const name of ['Cursor', 'OpenCode']) {
      const article = nodes(view.render()).find(node => node.type === 'article' && node.props['aria-label'] === name);
      const html = renderToStaticMarkup(article);
      assert.match(html, />Ready</);
      assert.doesNotMatch(html, /Builds unavailable|cannot yet build/);
      assert.equal(nodes(article).find(node => node.type === 'input').props.disabled, false);
      assert.match(html, /Check connection/);
      assert.ok(html.includes(`Allow Charlie to use ${name}`));
    }
  }
});

test("coding tool cards name sign-in for each supported tool", async () => {
  for (const subject of ["usr_inventory", "usr_gardening"]) {
    const view = await load({ subject, ui: flagOn({ claude: false, codex: false, cursor: false, opencode: false }),
      native: ["claude", "codex", "cursor", "opencode"].map(name => tool(name, { signed_in: false })) });
    for (const login of ["claude auth login", "codex login", "opencode auth login"]) {
      assert.ok(view.html().includes(`<code>${login}</code>`));
    }
  }
});

async function load({ ui, native, desktop = true, subject = "usr_bakery", local = false }) {
  const lib = await import("../../lib/codingAgents.ts");
  const guidance = await import("../../lib/localToolGuidance.ts");
  const source = readFileSync(`${__dirname}/CodingAgentTiles.tsx`, "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  let cursor = 0;
  const slots = [], effects = [], calls = [], native_calls = [];
  const hooks = {
    useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial; return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }]; },
    useEffect(callback, deps) { const index = cursor++, prior = slots[index]; if (!prior || deps.some((value, dep) => prior[dep] !== value)) { slots[index] = deps; effects.push(callback); } },
  };
  const exports = {};
  vm.runInNewContext(compiled, { exports, AbortSignal, require: id => id === "react" ? { ...React, ...hooks } : id === "react/jsx-runtime" ? { jsx, jsxs, Fragment: React.Fragment }
    : id === "@/lib/client" ? { api: async (path, init) => {
      calls.push({ path, init });
      if (path.startsWith("/api/local-tools/coding-agents")) {
        if (local && Array.isArray(native)) return { ok: true, data: { tools: native } };
        return ui("/api/local-tools/coding-agents", init);
      }
      return ui(path, init);
    }, verifiedSessionSubject: () => subject }
    : id === "@/lib/desktop" ? { isDesktopShell: () => desktop, desktopInvokeQuiet: async cmd => { native_calls.push(cmd); return native; },
      desktopInvokeOrThrow: async cmd => { native_calls.push(cmd); return typeof native === "function" ? native(cmd) : native; } }
    : id === "@/lib/codingAgents" ? lib : id === "@/lib/localToolGuidance" ? guidance
    : id === "@/lib/localMode" ? { isLocalConsole: () => local }
    : id.includes("ErrorNotice") ? { ErrorNotice: ({ error }) => error ? React.createElement("p", { role: "alert" }, error) : null }
    : { default: new Proxy({}, { get: (_, name) => String(name) }) } });
  const render = () => { cursor = 0; return exports.CodingAgentTiles(); };
  const settle = async () => { while (effects.length) effects.shift()(); await flush(); await flush(); };
  render(); await settle();
  return { render, settle, calls, native_calls, exports, html: () => renderToStaticMarkup(render()) };
}

const flagOn = consent => async path => path === "/api/ui-config"
  ? { ok: true, data: { founder_ui: "chat", coding_agents_v1: true, coding_agents: { consent } } }
  : { ok: true, data: { consent } };

test("the first local check bypasses a saved old verdict and shows Checking until it finishes", async () => {
  for (const subject of ["usr_freight", "usr_gardening"]) {
    let finish;
    const live = new Promise(resolve => { finish = resolve; });
    const claude = tool("claude", { installed: false });
    const old = { ok: true, data: { tools: [claude, tool("codex", { unsafe_location: true }), tool("cursor", { unsafe_location: true })] } };
    const view = await load({ subject, local: true, desktop: false, ui: async (path, init) => {
      if (path === "/api/ui-config") return { ok: true, data: { coding_agents: { consent: { codex: true } } } };
      return init?.cache === "no-store" ? live : old;
    } });
    const card = label => nodes(view.render()).find(node => node.type === "article" && node.props["aria-label"] === label);
    assert.match(renderToStaticMarkup(card("Codex")), />Checking…</);
    assert.equal(nodes(card("Codex")).find(node => node.type === "button").props.disabled, true);
    assert.doesNotMatch(view.html(), /Found in an unsafe location|Sign in needed/);
    finish({ ok: true, data: { tools: [claude, tool("codex", { signed_in: true, coding_available: true }),
      tool("cursor", { candidate: "review-current", candidate_path: "/opt/tools/cursor-agent" })] } });
    await view.settle();
    assert.match(renderToStaticMarkup(card("Codex")), />Ready</);
    assert.match(renderToStaticMarkup(card("Cursor")), />Approval needed</);
    assert.doesNotMatch(view.html(), /Found in an unsafe location|Checking…/);
    assert.equal(view.calls[1].path, "/api/local-tools/coding-agents?refresh=true");
  }
});

test("Check connection verifies a new version without changing Allow", async () => {
  for (const subject of ["per_freight", "per_hiking"]) {
    let checked = false;
    const view = await load({ subject, desktop: false, local: true, ui: async (path, init) => {
      if (path === "/api/ui-config") return { ok: true, data: { coding_agents: { consent: { codex: true } } } };
      if (path === "/api/local-tools/coding-agents") return { ok: true, data: { tools: ["claude", "codex", "cursor", "opencode"].map(
        name => tool(name, { signed_in: checked, coding_available: checked, ...(!checked && name === "codex" && {
          candidate: "reviewed-new-version", candidate_path: "/opt/tools/codex" }) })) } };
      assert.equal(path, "/api/ui-config/coding-agents/check");
      assert.deepEqual(JSON.parse(init.body), { provider: "codex", candidate: "reviewed-new-version" });
      checked = true;
      return { ok: true };
    } });
    const card = nodes(view.render()).find(node => node.type === "article" && node.props["aria-label"] === "Codex");
    await nodes(card).find(node => node.type === "button" && node.props.children === "Check connection").props.onClick();
    assert.equal(checked, true);
    assert.match(view.html(), /Your connection is ready\. Ask Charlie to build/);
  }
});

test("local browser checks subscription status and saves allowance without native commands", async () => {
  for (const subject of ["per_logistics", "per_garden"]) {
    let consent = { claude: false, codex: false, cursor: false };
    const tools = [tool("claude", { unsafe_location: true, unsafe_location_message: "Windows permissions must be verifiable." }), tool("cursor", { installed: false })];
    const view = await load({ subject, desktop: false, local: true, ui: async (path, init) => {
      if (path === "/api/ui-config") return { ok: true, data: { coding_agents: { consent } } };
      if (path === "/api/local-tools/coding-agents") return { ok: true, data: { tools: [...tools,
        tool("codex", consent.codex ? { signed_in: true, coding_available: true }
          : { candidate: "reviewed-tool", candidate_path: "/opt/tools/codex" })] } };
      const body = JSON.parse(init.body);
      assert.deepEqual(body, { provider: "codex", allow: true, candidate: "reviewed-tool" });
      consent = { ...consent, [body.provider]: body.allow };
      return { ok: true, data: { consent } };
    } });
    assert.match(view.html(), /Approval needed/);
    assert.match(view.html(), /Found in an unsafe location/);
    assert.match(view.html(), /Windows permissions must be verifiable/);
    assert.match(view.html(), /<code>\/opt\/tools\/codex<\/code>/);
    const box = () => nodes(view.render()).find(node => node.type === "input" && node.props["aria-label"] === "Allow Charlie to use Codex");
    assert.equal(box().props.disabled, false);
    await box().props.onChange({ target: { checked: true } });
    assert.equal(box().props.checked, true);
    assert.match(view.html(), /Ready/);
    await nodes(view.render()).find(node => node.type === "button" && node.props.children === "Re-detect coding tools").props.onClick();
    await flush();
    assert.equal(view.calls.filter(call => call.path.startsWith("/api/local-tools/coding-agents")).length, 3);
    assert.deepEqual(view.native_calls, []);
  }
});

test("each coding card has one status, one action and inline checking feedback across domains", async () => {
  for (const subject of ["usr_fleet", "usr_gardening"]) {
    const native = [tool("claude", { signed_in: true, coding_available: true }), tool("codex", {}),
      tool("cursor", { signed_in: true, coding_available: true })];
    const view = await load({ subject, local: true, ui: flagOn({}), native });
    const cards = () => nodes(view.render()).filter(node => node.type === "article");
    assert.equal(cards().length, (await import("../../lib/codingAgents.ts")).CODING_AGENTS.length);
    for (const card of cards()) {
      assert.equal(nodes(card).filter(node => node.type === "button").length, 1);
      assert.equal(nodes(card).filter(node => node.type === "details").length, 1);
      assert.ok(nodes(card).some(node => node.type === "summary" && node.props.children === "How to sign in"));
    }
    assert.match(view.html(), /Sign in needed/);
    assert.doesNotMatch(view.html(), />Installed<|>Signed in</);
    const cursorCard = () => cards().find(card => card.props["aria-label"] === "Cursor");
    const pending = nodes(cursorCard()).find(node => node.type === "button").props.onClick();
    assert.match(view.html(), /Checking/);
    await pending;
    assert.ok(nodes(cursorCard()).some(node => node.props.role === "status"));
    assert.equal(nodes(view.render()).find(node => node.props.className === "connectionsGrid").props.style?.alignItems, "start");
    assert.match(view.html(), /Your connection is ready/);
    assert.equal(view.calls.filter(call => call.init?.method === "POST").length, 0);
  }
});

test("flag off renders nothing and never asks the Mac app", async () => {
  for (const data of [{ founder_ui: "chat" }, { founder_ui: "chat", coding_agents_v1: false }]) {
    const view = await load({ ui: async () => ({ ok: true, data }), native: [tool("claude", {})] });
    assert.equal(view.render(), null);
    assert.deepEqual(view.native_calls, []);
    assert.equal(view.calls.length, 1);
  }
  const anon = await load({ ui: flagOn({}), native: null, subject: null });
  assert.equal(anon.render(), null); assert.deepEqual(anon.calls, []);
});

test("API-only and unknown sign-in never claim a subscription; saved revocation survives re-detection", async () => {
  const lib = await import("../../lib/codingAgents.ts");
  for (const subject of ["usr_warehouse", "usr_recipes"]) {
    const native = [tool("claude", {}), tool("codex", { signed_in: true }), tool("cursor", {})];
    assert.equal(lib.codingAgentState(native[1]), "Signed in");
    assert.equal(lib.codingAgentState({ ...native[1], coding_available: true, cli_path: null }), "Signed in");
    let saved = { claude: false, codex: true, cursor: false };
    const view = await load({ subject, native, ui: async (path, init) => {
      if (path === "/api/ui-config") return { ok: true, data: { coding_agents_v1: true, coding_agents: { consent: saved } } };
      const body = JSON.parse(init.body);
      saved = { ...saved, [body.provider]: body.allow };
      return { ok: true, data: { consent: saved } };
    } });
    const inputs = () => nodes(view.render()).filter(node => node.type === "input");
    assert.equal(inputs()[0].props.disabled, true);
    assert.equal(inputs()[1].props.disabled, false);
    await inputs()[1].props.onChange({ target: { checked: false } });
    const refresh = nodes(view.render()).find(node => node.type === "button" && node.props.children === "Re-detect coding tools");
    assert.ok(refresh);
    await refresh.props.onClick();
    assert.equal(inputs()[1].props.checked, false);
    assert.equal(view.calls.filter(call => call.init?.method === "POST").length, 1);
  }
});

test("local connections load saved consent without a hosted feature flag", async () => {
  for (const subject of ["usr_warehouse", "usr_recipes"]) {
    const view = await load({ subject, local: true, native: null,
      ui: async () => ({ ok: true, data: { coding_agents: { consent: { claude: false, codex: false, cursor: false } } } }) });
    assert.match(view.html(), /Allow Charlie to use Codex/);
    assert.doesNotMatch(view.html(), /approve each coding task|SDK credit/);
  }
});

test("checking a connected CLI uses existing native commands without granting consent", async () => {
  for (const subject of ["usr_inventory", "usr_fitness"]) {
    const native = [tool("claude", {}), tool("codex", { signed_in: true, coding_available: true }), tool("cursor", {})];
    const view = await load({ subject, native, ui: flagOn({ claude: false, codex: false, cursor: false }) });
    const card = nodes(view.render()).find(node => node.type === "article" && node.props["aria-label"] === "Codex");
    const connect = nodes(card).find(node => node.type === "button" && node.props.children === "Check connection");
    assert.ok(connect);
    await connect.props.onClick(); await flush();
    assert.deepEqual(view.native_calls, ["list_local_tools", "connect_local_tool", "list_local_tools"]);
    assert.equal(view.calls.filter(call => call.init?.method === "POST").length, 0);
    assert.equal(nodes(view.render()).filter(node => node.type === "input")[1].props.checked, false);
  }
});

test("malformed native sign-in status cannot verify a subscription", async () => {
  const lib = await import("../../lib/codingAgents.ts");
  for (const subject of ["usr_inventory", "usr_fitness"]) {
    const native = [tool("claude", {}), tool("codex", { signed_in: "true", coding_available: true }), tool("cursor", {})];
    assert.equal(lib.macReportedTools(native), null);
    const view = await load({ subject, native, ui: flagOn({ claude: false, codex: false, cursor: false }) });
    assert.match(view.html(), /Open or update your Supymem desktop app/);
    assert.doesNotMatch(view.html(), />Ready</);
  }
});

test("local coding tool choices can be retried without exposing unverified Allow switches", async () => {
  for (const [subject, rejects] of [["usr_couriers", false], ["usr_gardeners", true]]) {
    let attempts = 0, finish;
    const waiting = new Promise(resolve => { finish = resolve; });
    const view = await load({ subject, local: true,
      native: [tool("claude", { signed_in: true, coding_available: true }), tool("codex", {}), tool("cursor", {})],
      ui: async () => {
        if (++attempts > 1) return waiting;
        if (rejects) throw new Error("Network failure");
        return { ok: false, data: null };
      },
    });
    assert.match(view.html(), /Keys and connections &gt; Coding tools/);
    assert.doesNotMatch(view.html(), /Allow Charlie/);
    const retry = nodes(view.render()).find(node => node.type === "button" && node.props.children === "Try again");
    assert.ok(retry, "Show an in-place retry");
    retry.props.onClick();
    assert.doesNotMatch(view.html(), /Allow Charlie|Try again/);
    await view.settle();
    assert.equal(attempts, 2);
    assert.deepEqual(view.native_calls, []);
    assert.match(view.html(), /Loading your coding tools/);
    finish({ ok: true, data: { coding_agents: { consent: { claude: false } } } });
    await view.settle();
    const inputs = nodes(view.render()).filter(node => node.type === "input");
    assert.equal(inputs.find(node => node.props["aria-label"].endsWith("Claude Code")).props.disabled, false);
    assert.equal(inputs.find(node => node.props["aria-label"].endsWith("Codex")).props.disabled, true);
    assert.deepEqual(view.calls.map(call => call.path), ["/api/ui-config", "/api/ui-config", "/api/local-tools/coding-agents?refresh=true"]);
  }
});

test("tiles show three plain states with sign-in help behind a disclosure", async () => {
  const none = { claude: false, codex: false, cursor: false };
  const native = [tool("hermes", {}), tool("claude", { coding_available: true, signed_in: true }),
    tool("codex", { signed_in: true }), tool("cursor", { installed: false, cli_path: null })];
  const view = await load({ ui: flagOn(none), native });
  assert.deepEqual(view.native_calls, ["list_local_tools"]);
  const html = view.html();
  for (const label of ["Claude Code", "Codex", "Cursor"]) assert.match(html, new RegExp(`Allow Charlie to use ${label}`));
  assert.match(html, /Ready/); assert.match(html, /Sign in needed/); assert.match(html, /Not installed/);
  assert.match(html, /Install OpenCode, then re-detect/);
  assert.match(html, /Use your Claude subscription for builds you request/);
  assert.match(html, /How to sign in/);
  assert.match(html, /Ask Charlie to build your app/);
  assert.doesNotMatch(html, /SDK credit|approve each coding task|Signed in with your subscription/);
  assert.doesNotMatch(html, /Open the Supymem Mac app/);
  const installed = await load({ ui: flagOn(none), native: [tool("claude", {}), tool("codex", {}), tool("cursor", { cli_path: null })] });
  assert.equal((installed.html().match(/>Sign in needed</g) || []).length, 2);
  assert.match(installed.html(), /Builds unavailable/);
});

test("web and older Mac apps fall back to opening the Mac app", async () => {
  const none = { claude: false, codex: false, cursor: false };
  const old = [{ name: "claude", label: "Claude Code", kind: "ide", installed: true, connected: false, app_path: null, cli_path: "/bin/claude" },
    { name: "codex", label: "Codex", kind: "ide", installed: true, connected: false, app_path: null, cli_path: null },
    { name: "cursor", label: "Cursor", kind: "ide", installed: false, connected: false, app_path: null, cli_path: null }];
  for (const [desktop, native] of [[false, null], [true, null], [true, old], [true, "unknown command"]]) {
    const view = await load({ ui: flagOn(none), native, desktop });
    const html = view.html();
    assert.match(html, /Open or update your Supymem desktop app/);
    assert.doesNotMatch(html, /Not installed|Ready|Signed in with/);
    assert.deepEqual(view.native_calls, desktop ? ["list_local_tools"] : []);
  }
});

test("consent and revoke post per provider and show the saved server state", async () => {
  for (const subject of ["usr_bakery", "usr_trails"]) {
    let saved = { claude: false, codex: true, cursor: false };
    const view = await load({ subject, native: null, ui: async (path, init) => {
      if (path === "/api/ui-config") return { ok: true, data: { founder_ui: "chat", coding_agents_v1: true, coding_agents: { consent: saved } } };
      const body = JSON.parse(init.body); saved = { ...saved, [body.provider]: body.allow }; return { ok: true, data: { consent: saved } };
    } });
    const box = label => nodes(view.render()).find(node => node.type === "input" && node.props["aria-label"] === `Allow Charlie to use ${label}`);
    assert.equal(box("Codex").props.checked, true); assert.equal(box("Claude Code").props.checked, false);
    await box("Claude Code").props.onChange({ target: { checked: true } });
    await box("Codex").props.onChange({ target: { checked: false } });
    assert.deepEqual(view.calls.slice(1).map(call => [call.path, call.init.method, JSON.parse(call.init.body)]), [
      ["/api/ui-config/coding-agents/consent", "POST", { provider: "claude", allow: true }],
      ["/api/ui-config/coding-agents/consent", "POST", { provider: "codex", allow: false }],
    ]);
    assert.equal(box("Claude Code").props.checked, true); assert.equal(box("Codex").props.checked, false);
  }
  const failing = await load({ native: null, ui: async path => path === "/api/ui-config" ? flagOn({ claude: false, codex: false, cursor: false })(path) : { ok: false, status: 500, data: null } });
  await nodes(failing.render()).find(node => node.type === "input").props.onChange({ target: { checked: true } });
  assert.match(failing.html(), /could not be saved/);
  assert.equal(nodes(failing.render()).find(node => node.type === "input").props.checked, false);
});
