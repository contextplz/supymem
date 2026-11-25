const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

function load(filename, overrides = {}) {
  const exported = {};
  const setupProviders = filename === "LocalSetup.tsx" ? load("localProviders.ts", { modules: { "@/lib/client": {} } }) : null;
  const source = readFileSync(join(__dirname, filename), "utf8");
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, {
    exports: exported, AbortSignal, AbortController, Error, URL, Response, setTimeout, clearTimeout, process: { env: {} }, ...overrides,
    require: id => id === "./localProviders" && setupProviders ? { ...setupProviders, ...overrides.modules?.[id] } : overrides.modules?.[id] ?? (id === "@/components/chat/CharlieDock" ? {
      CharlieMascot: () => React.createElement("img", { src: "/charlie/poster.png", alt: "Charlie" }),
    } : id === "@/components/primitives/ErrorNotice" ? {
      ErrorNotice: ({ error, id }) => error ? React.createElement("p", { role: "alert", id }, error) : null,
    } : id === "@/components/desktop/CharlieIntro" ? { CharlieIntroWelcome: () => null
    } : id === "next/navigation" ? { usePathname: () => "/", useRouter: () => ({ push() {} })
    } : id === "../settings/local/LocalSettings" ? { LocalSettings: () => null }
      : id === "@/lib/client" ? { api: async () => ({ ok: true, data: { sections: [] } }) }
      : id === "@/lib/chatStop" ? require("../../lib/chatStop.ts") : require(id)),
  });
  return exported;
}

test("saved ventures open with key recovery instead of first-run setup", async () => {
  for (const domain of ["industrial purchasing", "family meal planning"]) for (const failure of [false, true]) {
    let cursor = 0;
    const states = [], effects = [], requests = [];
    const components = load("LocalSetup.tsx", { modules: {
      react: {
        useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = value; }]; },
        useRef(initial) { return { current: initial }; }, useEffect(callback) { effects.push(callback); },
      },
      "./localProviders": { providerConfigured: async () => { if (failure) throw new Error("Your saved settings could not be read."); return false; } },
      "@/lib/client": { api: async path => { requests.push(path); return { ok: true, data: { items: [{ id: "saved", name: domain }] } }; } },
    } });
    const child = React.createElement("main", null, domain);
    const render = () => { cursor = 0; return components.LocalProviderGate({ children: child }); };
    render();
    effects.splice(0).forEach(callback => callback());
    await new Promise(resolve => setImmediate(resolve));
    const view = render();
    assert.equal(view.props.className, "local-sample-workspace");
    const html = renderToStaticMarkup(view);
    assert.match(html, new RegExp(domain));
    assert.match(html, /Keys and connections/);
    assert.match(html, /Your AI provider/);
    assert.match(html, /re-enter your API key/);
    assert.match(html, /href="\/settings\/local"/);
    assert.doesNotMatch(html, /Connect your AI service|Meet Charlie/);
    assert.deepEqual(requests, ["/api/ventures?summary=true"]);
  }
});

test("the workspace waits for saved keys instead of opening setup or recovery", async () => {
  for (const domain of ["fleet-billing.invalid", "bird-feeding.invalid"]) for (const savedVentures of [false, true]) {
    for (const eventuallyReady of [false, true]) {
      let cursor = 0, providerReads = 0, ventureReads = 0;
      const states = [], effects = [], timers = [];
      const providers = load("localProviders.ts", { fetch: async path => {
        assert.equal(path, "/api/local/providers");
        if (++providerReads > 1 && eventuallyReady) return Response.json({ configured: true });
        return Response.json({ detail: { code: "local_credentials_pending" } }, { status: 503 });
      }, modules: { "@/lib/client": {} } });
      const components = load("LocalSetup.tsx", {
        setTimeout(callback) { timers.push(callback); },
        modules: {
          react: {
            useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = value; }]; },
            useRef(initial) { return { current: initial }; }, useEffect(callback) { effects.push(callback); },
          },
          "./localProviders": providers,
          "@/lib/client": { api: async () => {
            ventureReads++;
            return { ok: true, data: { items: savedVentures ? [{ name: domain }] : [] } };
          } },
        },
      });
      const render = () => { cursor = 0; return renderToStaticMarkup(components.LocalProviderGate({children: React.createElement("main", null, domain)})); };
      render();
      const dispose = effects.shift()();
      await new Promise(setImmediate);
      assert.match(render(), /Starting your app/);
      for (let attempt = 0; attempt < (eventuallyReady ? 1 : 5); attempt++) {
        timers.shift()();
        await new Promise(setImmediate);
      }
      const markup = render();
      if (eventuallyReady) assert.ok(markup.includes(domain));
      else assert.match(markup, /system keychain.*<button>Retry<\/button>/);
      assert.doesNotMatch(markup, /re-enter your API key|Connect your AI service/);
      assert.equal(ventureReads, 0);
      dispose();
    }
  }
});

test("your saved ventures wait for startup and do not suggest changing your key", async context => {
  const client = await import("../../lib/client.ts");
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  context.after(() => originalWindow ? Object.defineProperty(globalThis, "window", originalWindow) : delete globalThis.window);
  context.mock.timers.enable({apis: ["setTimeout"]});
  for (const domain of ["industrial purchasing", "family meal planning"]) for (const ready of [true, false]) {
    let cursor = 0, providerReads = 0, warming = true;
    const states = [], effects = [], requests = [];
    const origin = `https://${domain.replaceAll(" ", "-")}.invalid`;
    Object.defineProperty(globalThis, "window", {configurable: true, value: {__SUPYMEM__: {mode: "local"}, location: {origin}, dispatchEvent() {}}});
    const fetch = context.mock.method(globalThis, "fetch", async url => {
      requests.push(url);
      return warming ? Response.json({error: {code: "core_unavailable"}}, {status: 503})
        : Response.json({items: [{name: domain}]});
    });
    const components = load("LocalSetup.tsx", { modules: {
      react: {
        useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = value; }]; },
        useRef(initial) { return { current: initial }; }, useEffect(callback) { effects.push(callback); },
      },
      "./localProviders": { providerConfigured: async () => {
        if (++providerReads === 1) throw new Error("Your AI settings could not be reached.");
        return true;
      } },
      "@/lib/client": client,
    } });
    const render = () => { cursor = 0; return renderToStaticMarkup(components.LocalProviderGate({children: React.createElement("main", null, domain)})); };
    render();
    const dispose = effects.shift()();
    await new Promise(setImmediate);
    assert.match(render(), /role="status"[^>]*>Starting your app…/);
    assert.equal(requests.length, 2);
    context.mock.timers.tick(999);
    await new Promise(setImmediate);
    assert.equal(requests.length, 2, "Do not hammer your app while it starts");
    context.mock.timers.tick(1);
    await new Promise(setImmediate);
    assert.equal(requests.length, 3);
    assert.match(render(), /Starting your app…/);
    warming = !ready;
    context.mock.timers.tick(2000);
    await new Promise(setImmediate);
    if (!ready) {
      assert.match(render(), /Starting your app…/);
      context.mock.timers.tick(4000);
      await new Promise(setImmediate);
    }
    assert.deepEqual(requests, Array(ready ? 4 : 5).fill(`${origin}/api/ventures?summary=true`));
    const markup = render();
    if (ready) assert.ok(markup.includes(domain));
    else assert.match(markup, /<button>Retry<\/button>/);
    assert.doesNotMatch(markup, /Your AI provider|Restart your local app/);
    dispose();
    fetch.mock.restore();
  }
});

test("local console bakes its mode and keeps runtime URLs without direct-to-core rewrites", async () => {
  for (const domain of ["procurement.example", "gardening.example"]) {
    const core = `https://api.${domain}`;
    const hosted = load("../../../next.config.ts", { process: { env: { CORE_URL: core } } });
    assert.equal(hosted.default.rewrites, undefined);
    const local = load("../../../next.config.ts", { process: { env: { SUPYMEM_MODE: " LOCAL ", CORE_URL: core + "/", NEXT_PUBLIC_GATEWAY_URL: "http://127.0.0.1:3100" } } });
    assert.equal(local.default.env.NEXT_PUBLIC_SUPYMEM_MODE.trim().toLowerCase(), "local");
    const publicLocal = load("../../../next.config.ts", { process: { env: { NEXT_PUBLIC_SUPYMEM_MODE: " LOCAL ", CORE_URL: core } } });
    assert.equal(publicLocal.default.env.NEXT_PUBLIC_SUPYMEM_MODE, "local");
    assert.equal(local.default.rewrites, undefined);
    assert.equal(publicLocal.default.rewrites, undefined);
    for (const configuration of [local, publicLocal]) {
      const policies = await configuration.default.headers();
      const policy = policies[0].headers.find(header => header.key === "Content-Security-Policy-Report-Only").value;
      assert.match(policy, /report-uri \/api\/csp-report/);
      assert.doesNotMatch(policy, /127\.0\.0\.1:(3100|3200)/);
      assert.doesNotMatch(policy, /https:\/\/(?:api|events)(?:\.staging)?\.supymem\.com/);
    }
  }
});

test("you can inspect key upgrades and delete only your labelled sample venture in one click", async () => {
  for (const domain of ["warehouse planning", "family gardening"]) {
    let cursor = 0;
    const states = [];
    const effects = [];
    const requests = [];
    const redirects = [];
    const components = load("LocalSetup.tsx", { modules: {
      "next/navigation": { useRouter: () => ({ push: path => redirects.push(path) }) },
      react: {
        useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }]; },
        useEffect(callback) { effects.push(callback); },
      },
      "@/lib/client": { api: async (path, options) => {
        requests.push({ path, options });
        return { ok: true, data: { items: [{ id: "demo", name: `Sample: ${domain}`, sample: true }, { id: "live", name: domain, sample: false }] } };
      } },
    } });
    const render = () => { cursor = 0; return components.SampleVentures({ refresh: "/v/demo/site" }); };
    assert.equal(render(), null);
    effects.shift()();
    await new Promise(resolve => setImmediate(resolve));
    const view = render();
    const html = renderToStaticMarkup(view);
    assert.match(html, /not real evidence/);
    assert.match(html, /href="\/settings\/local"/);
    assert.match(html, /href="\/settings\/local\/capabilities"/);
    const list = view.props.children.find(node => node?.type === "ul");
    assert.equal(list.props.children.length, 1);
    const button = list.props.children[0].props.children.find(node => node?.type === "button");
    await button.props.onClick();
    assert.equal(requests.at(-1).path, "/api/ventures/demo/delete");
    assert.equal(requests.at(-1).options.method, "POST");
    assert.deepEqual(JSON.parse(requests.at(-1).options.body), { confirmation: "DELETE" });
    assert.deepEqual(redirects, ["/"]);
    assert.equal(render(), null);
  }
});

test("sample ventures stay inside the bounded All ventures view, never above your conversation", async () => {
  for (const domain of ["equipment procurement", "weekend gardening"]) for (const sample of [false, true]) {
    let cursor = 0;
    const states = [];
    const effects = [];
    const components = load("LocalSetup.tsx", { modules: {
      react: {
        useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = value; }]; },
        useRef(initial) { return { current: initial }; }, useEffect(callback) { effects.push(callback); },
      },
      "./localProviders": { providerConfigured: async callback => { callback(sample); return true; } },
    } });
    const render = () => { cursor = 0; return components.LocalProviderGate({ children: React.createElement("main", null, domain) }); };
    render();
    effects.splice(0).forEach(callback => callback());
    await new Promise(resolve => setImmediate(resolve));
    const gate = render();
    assert.equal(gate.props.style.display, sample ? "grid" : "contents");
    if (sample) assert.equal(gate.props.children[0].type, "p", "Only the sample disclosure occupies the first grid row");
    assert.doesNotMatch(renderToStaticMarkup(gate), /Your sample ventures/);
    const containsSamples = node => !!node && (node.type === components.SampleVentures || React.Children.toArray(node.props?.children).some(containsSamples));
    assert.equal(containsSamples(gate), false, "The provider gate must not mount a list above the shell");

    const source = readFileSync(join(__dirname, "../../components/layout/AppShell.tsx"), "utf8");
    const tree = ts.createSourceFile("AppShell.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const component = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "AppShellInner");
    const placement = component.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(tree) === 'local && pathname === "/"');
    assert.ok(placement, "The list belongs to the All ventures page content");
    const code = ts.transpileModule(`(function () { ${placement.getText(tree)} return children; })`, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
    } }).outputText;
    for (const local of [true, false]) for (const pathname of ["/", "/new?venture=sample", "/v/sample"]) {
      const children = React.createElement("main", null, domain);
      const context = { React, local, pathname, children, SampleVentures: "sample-list" };
      const view = vm.runInNewContext(code, context)();
      if (local && pathname === "/") {
        assert.equal(view.props.style.height, "100%");
        const [samples, content] = view.props.children;
        assert.equal(samples.props.style.overflow, "auto");
        assert.equal(samples.props.style.minHeight, 0);
        assert.equal(samples.props.children.type, "sample-list");
        assert.equal(content.props.children, children);
        assert.equal(content.props.style.flex, "1 0 50%");
      } else assert.equal(view, children);
    }
  }
});

test("you can choose the labelled offline sample before entering a key", () => {
  const components = load("LocalSetup.tsx");
  const html = renderToStaticMarkup(React.createElement(components.LocalSetup, { onConfigured() {} }));
  assert.ok(html.indexOf("Try without a key (sample mode)") < html.indexOf("Your API key"));
  assert.match(html, /no key and no outside connections/);
  assert.match(html, /Samples are labelled and cannot send email/);
});

test("your sample banner switches to live immediately after you save your model key", async () => {
  for (const domain of ["procurement", "gardening"]) {
    let cursor = 0;
    let sample = true;
    const states = [];
    const effects = [];
    const listeners = {};
    const components = load("LocalSetup.tsx", { window: {
      addEventListener(name, callback) { listeners[name] = callback; }, removeEventListener() {},
    }, modules: {
      react: {
        useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }]; },
        useRef(initial) { return { current: initial }; }, useEffect(callback) { effects.push(callback); },
      },
      "./localProviders": { providerConfigured: async callback => { callback(sample); return true; } },
    } });
    const render = () => { cursor = 0; return components.LocalProviderGate({ children: domain }); };
    render();
    effects.splice(0).forEach(callback => callback());
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(render().props.className, "local-sample-workspace");
    effects.length = 0;
    sample = false;
    listeners["supymem-local-settings-changed"]();
    render();
    effects.splice(0).forEach(callback => callback());
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(render().props.className, undefined);
  }
});

test("your server proxy forwards sessions, providers, API calls and streams without exposing its credential across domains", async () => {
  const { mkdtempSync, writeFileSync, rmSync, chmodSync } = require("node:fs");
  const { tmpdir } = require("node:os");
  const { randomBytes } = require("node:crypto");
  const directory = mkdtempSync(join(tmpdir(), "local-proxy-"));
  const secret = randomBytes(32).toString("hex"), filename = join(directory, "secret");
  writeFileSync(filename, secret, { mode: 0o600 });
  try {
    for (const domain of ["procurement.example", "gardening.example"]) {
      const calls = [];
      const env = { SUPYMEM_MODE: " LOCAL ", CORE_URL: "http://core:3300", SUPYMEM_LOCAL_PROXY_SECRET_FILE: filename };
      const route = load("../api/[...path]/route.ts", { Request, Headers, Buffer, process: { env, platform: process.platform, geteuid: process.geteuid }, fetch: async (url, init) => {
        const target = new URL(url);
        assert.equal(target.origin, "http://core:3300");
        assert.equal(init.headers.get("x-local-proxy-secret") === secret, true);
        assert.equal(init.headers.get("x-forwarded-host"), "127.0.0.1:3100");
        assert.equal(init.headers.get("x-forwarded-for"), null);
        assert.equal(init.redirect, "error");
        assert.equal(init.cache, "no-store");
        calls.push({ path: target.pathname, query: target.search, method: init.method, body: init.body });
        return new Response(domain, { headers: { "content-type": "text/event-stream", "x-local-proxy-secret": "redacted" } });
      } });
      for (const [path, upstream] of [["/api/auth/session", "/auth/session"], ["/api/local/providers", "/local/providers"],
        ["/api/workspaces", "/workspaces"], ["/api/v1/workspaces", "/api/v1/workspaces"], ["/api/ventures/owner/activity/stream", "/ventures/owner/activity/stream"]]) {
        const request = new Request(`http://127.0.0.1:3100${path}?cursor=2`, { headers: {
          Host: "127.0.0.1:3100", Origin: "http://127.0.0.1:3100", "X-Local-Proxy-Secret": secret,
          "X-Forwarded-Host": domain, "X-Forwarded-For": "127.0.0.1",
        } });
        const response = await route.GET(request);
        assert.equal(response.status, 200);
        assert.equal(response.headers.get("x-local-proxy-secret"), null);
        assert.equal(response.headers.get("cache-control"), "no-store");
        assert.equal(response.headers.get("content-type"), "text/event-stream");
        assert.equal(await response.text(), domain);
        assert.equal(calls.at(-1).path, upstream);
        assert.equal(calls.at(-1).query, "?cursor=2");
      }
      const request = new Request("http://127.0.0.1:3100/api/local/providers", { method: "POST",
        headers: { Host: "127.0.0.1:3100", Origin: "http://127.0.0.1:3100", "Content-Type": "application/json", "X-Local-Proxy-Secret": secret }, body: JSON.stringify({ model: domain }) });
      assert.equal((await route.POST(request)).status, 200);
      assert.equal(calls.at(-1).method, "POST");
      assert.deepEqual(JSON.parse(await new Response(calls.at(-1).body).text()), { model: domain });
      const before = calls.length;
      for (const headers of [{ Host: domain }, { Host: "127.0.0.1:3100", Origin: `https://${domain}` },
        { Host: "127.0.0.1:3100", Origin: "null" }, { Host: "127.0.0.1:3100", "Sec-Fetch-Site": "cross-site" }]) {
        assert.equal((await route.GET(new Request("http://127.0.0.1:3100/api/auth/session", { headers }))).status, 403);
      }
      assert.equal(calls.length, before);
      env.SUPYMEM_MODE = "hosted";
      assert.equal((await route.GET(new Request("http://127.0.0.1:3100/api/auth/session"))).status, 404);
      env.SUPYMEM_MODE = "local";
      chmodSync(filename, 0o666);
      assert.equal((await route.GET(new Request("http://127.0.0.1:3100/api/auth/session", { headers: { Host: "127.0.0.1:3100" } }))).status, 503);
      chmodSync(filename, 0o600);
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("your server selects local mode before startup even when you reuse a hosted build across domains", async () => {
  const names = ["ChunkReloadGuard", "ConsoleBuildUpdate", "DesktopBoot", "DesktopUpdateGate", "DesktopProductBoundary", "CharlieIntro"];
  const modules = Object.fromEntries(names.map(name => [`@/components/desktop/${name}`, { [name]: () => null }]));
  modules["@/styles/tokens.css"] = {};
  modules["@/components/layout/LocalModeBoundary"] = { LocalModeBoundary: ({ children }) => children };
  modules["next/headers"] = { headers: async () => ({ get: () => null }) };
  for (const domain of ["procurement.example", "gardening.example"]) for (const local of [true, false]) {
    const layout = load("../layout.tsx", { process: { env: { SUPYMEM_MODE: local ? " LOCAL " : "hosted" } }, modules });
    assert.equal(layout.dynamic, "force-dynamic");
    const html = renderToStaticMarkup(await layout.default({ children: domain }));
    const script = html.match(/<script\b[^>]*>(.*?)<\/script>/)?.[1];
    if (local) {
      assert.ok(script);
      const window = { __SUPYMEM__: { gatewayUrl: `https://api.${domain}` } };
      vm.runInNewContext(script, { window });
      assert.equal(window.__SUPYMEM__.mode, "local");
      assert.equal(window.__SUPYMEM__.gatewayUrl, `https://api.${domain}`);
    } else assert.equal(script, undefined);
  }
});

test("setup is unavailable when hosted and uses the same local mode normalization", () => {
  for (const mode of [undefined, "hosted", "local", " LOCAL "]) {
    const page = load("page.tsx", { process: { env: { SUPYMEM_MODE: mode } }, modules: {
      "next/navigation": { notFound: () => { throw new Error("not found"); } },
      "./LocalSetup": { LocalProviderGate: () => null },
    } });
    if (mode?.trim().toLowerCase() === "local") assert.ok(page.default());
    else assert.throws(() => page.default(), /not found/);
  }
});

test("the public local flag also enables your setup page for business and consumer workspaces", () => {
  for (const domain of ["dispatch-software.invalid", "garden-club.invalid"]) {
    const page = load("page.tsx", { process: { env: { NEXT_PUBLIC_SUPYMEM_MODE: " LOCAL " } }, modules: {
      "next/navigation": { notFound: () => { throw new Error("not found"); } },
      "./LocalSetup": { LocalProviderGate: () => null },
    } });
    assert.ok(page.default(), domain);
  }
});

test("your local workspace cannot switch to the hosted dark signup screen across domains", () => {
  for (const domain of ["dispatch-software.invalid", "garden-club.invalid"]) {
    const effects = [], marketingCalls = [];
    const page = load("../page.tsx", { window: { location: { host: domain, pathname: "/", search: "?marketing=1" } }, modules: {
      react: { useState: initial => [initial, () => {}], useEffect: callback => effects.push(callback) },
      "@/components/layout/LocalModeBoundary": { useLocalMode: () => true },
      "@/components/layout/AppShell": { AppShell: "workspace" },
      "@/components/ventures/VentureHub": { VentureHub: "ventures" },
      "@/components/marketing/WaitlistLanding": { WaitlistLanding: "signup" },
      "@/lib/desktop": { isDesktopShell: () => false },
      "@/lib/hostMode": { isMarketingSurface: (...args) => { marketingCalls.push(args); return true; } },
    } });
    assert.equal(page.default().type, "workspace");
    effects.forEach(callback => callback());
    assert.equal(marketingCalls.length, 0);
    assert.match(readFileSync(join(__dirname, "../../styles/tokens.css"), "utf8"), /color-scheme:\s*light/);
  }
});

test("provider requests use your current origin or existing desktop bridge without exposing keys across domains", async context => {
  const client = await import("../../lib/client.ts");
  const originals = Object.fromEntries(["window", "fetch", "sessionStorage"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  context.after(() => {
    client.clearVerifiedSession();
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  });
  for (const domain of ["dispatch-software.invalid", "garden-club.invalid"]) for (const transport of ["browser", "desktop"]) {
    const calls = [];
    let status = 200, configured = false;
    const data = () => ({ configured, values: { model: domain }, secrets_set: { api_key: true }, detail: "fixture-private" });
    const origin = `https://${domain}`;
    Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: { getItem: key => key === "cos_gateway_url" ? origin : null, setItem() {} } });
    Object.defineProperty(globalThis, "window", { configurable: true, value: {
      location: { origin }, dispatchEvent: () => true,
      ...(transport === "desktop" ? { __TAURI_INTERNALS__: { invoke: async (command, options) => {
        if (command === "gateway_url") return origin;
        assert.equal(command, "desktop_api_request");
        assert.deepEqual(options.headers, {});
        if (options.path === "/auth/session") return { status: 200, data: { authenticated: true, subject: `usr_${domain}` } };
        calls.push({ path: options.path, options });
        return { status, data: data() };
      } } } : {}),
    } });
    globalThis.fetch = async (path, options) => {
      assert.equal(transport, "browser");
      if (path === `${origin}/auth/session`) return Response.json({ authenticated: true, subject: `usr_${domain}`, csrf: "fixture-csrf" });
      calls.push({ path, options });
      return Response.json(data(), { status });
    };
    client.clearVerifiedSession();
    const api = load("localProviders.ts", { window: { location: { protocol: transport === "browser" ? "http:" : "tauri:" } }, fetch: globalThis.fetch, modules: { "@/lib/client": client } });
    assert.equal(await api.providerConfigured(), false);
    configured = true;
    assert.equal(await api.providerConfigured(), true);
    await api.saveProvider({ provider: "custom", api_key: "fixture-private", model: domain, base_url: `https://${domain}/v1` });
    assert.ok(calls.every(call => call.path === "/api/local/providers"));
    if (transport === "browser") assert.ok(calls.every(call => call.options.credentials === "same-origin"));
    assert.equal(calls.at(-1).options.method, "POST");
    assert.equal((transport === "browser" ? JSON.parse(calls.at(-1).options.body) : calls.at(-1).options.body).model, domain);
    for (const failure of [404, 405, 422, 500]) {
      status = failure;
      await assert.rejects(api.providerConfigured(), error => !error.message.includes("fixture-private") && /try|restart|configure/i.test(error.message));
      await assert.rejects(api.saveProvider({ provider: "custom", api_key: "fixture-private", model: domain }), error => !error.message.includes("fixture-private") && /try|restart|configure/i.test(error.message));
    }
  }
});

test("save reaches POST without hosted login or AbortSignal.timeout for business and consumer models", async () => {
  for (const domain of ["invoice-workflow.invalid", "home-gardening.invalid"]) {
    const calls = [];
    const providers = load("localProviders.ts", {
      window: { location: { protocol: "http:", origin: "http://127.0.0.1:4100" } },
      AbortSignal: {},
      fetch: async (path, options) => { calls.push({ path, options }); return Response.json({ configured: true }); },
      modules: { "@/lib/client": { ensureSession: async () => { throw new Error("No hosted session"); }, api: async () => { throw new Error("Wrong transport"); } } },
    });
    await providers.saveProvider({ provider: "openai", api_key: "test-only-private", model: domain });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, "/api/local/providers");
    assert.equal(calls[0].options.method, "POST");
    assert.equal(calls[0].options.signal.aborted, false);
    assert.equal(JSON.parse(calls[0].options.body).model, domain);
  }
});

test("connection test allows a slow local model without delaying settings saves", async () => {
  for (const model of ["factory-scheduling", "home-gardening"]) {
    const deadlines = [], calls = [];
    const providers = load("localProviders.ts", {
      window: { location: { protocol: "http:" } },
      setTimeout: (_callback, milliseconds) => { deadlines.push(milliseconds); return deadlines.length; },
      clearTimeout() {},
      fetch: async (path, options) => { calls.push({ path, body: JSON.parse(options.body) }); return Response.json({ ok: true }); },
    });
    const settings = { provider: "openai_compatible", base_url: "http://127.0.0.1:1234/v1", model, context_window: 4096 };
    await providers.saveProvider(settings);
    await providers.testProvider(settings);
    assert.deepEqual(deadlines, [15_000, 620_000]);
    assert.deepEqual(calls[1], { path: "/api/local/providers/test", body: settings });
  }
});

function setupHarness(domain, overrides = {}) {
  let cursor = 0;
  const states = [], effects = [];
  const providers = load("localProviders.ts", { modules: { "@/lib/client": {} } });
  const components = load("LocalSetup.tsx", { modules: {
    react: { useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }]; }, useRef(initial) { return states[cursor++] ??= { current: initial }; }, useEffect(effect) { effects.push(effect); } },
    "./localProviders": { ...providers, ...overrides },
    "@/lib/client": {api: async path => ({ok: true, data: path.endsWith('/settings/catalog') ? {sections: []}
      : path.endsWith('/llm-catalog') ? {default_models: Object.fromEntries(providers.localProviders.map(option => [option.id, [`${domain} model`]]))}
      : {configured: false, values: {}, secrets_set: {}}})},
    "@/components/chat/CharlieDock": { CharlieMascot: () => React.createElement("img", { src: "/charlie/poster.png", alt: "Charlie" }) },
  } });
  const render = () => { cursor = 0; return components.LocalSetup({ onConfigured: overrides.onConfigured ?? (() => {}), sampleMode: overrides.sampleMode ?? false }); };
  const form = () => render().props.children.find(node => node?.type === "form");
  const nodes = () => form().props.children.flat(Infinity).filter(Boolean);
  const field = name => nodes().find(node => node?.props?.id === name);
  const change = (name, value) => field(name).props.onChange({ target: { value } });
  return { render, form, nodes, field, change, domain, async load() {render(); effects.splice(0).forEach(effect => effect()); await new Promise(resolve => setImmediate(resolve));} };
}

test('configured models skip the intro and connection form across saved and env-imported domains', async () => {
  for (const domain of ['Warehouse planning', 'Family gardening']) for (const source of ['app', 'env']) {
    let cursor = 0;
    const states = [], effects = [];
    const providers = load('localProviders.ts', {window: {location: {protocol: 'http:'}}, fetch: async () => ({ok: true, status: 200,
      json: async () => ({configured: true, values: {provider: 'anthropic', model: domain}, sources: {model: source}})})});
    const components = load('LocalSetup.tsx', {modules: {
      './localProviders': providers,
      react: {
        useState(initial) {const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => {states[index] = value;}];},
        useRef(initial) {return {current: initial};}, useEffect(callback) {effects.push(callback);},
      },
    }});
    const render = () => {cursor = 0; return components.LocalProviderGate({children: React.createElement('main', null, domain)});};
    render(); effects.splice(0).forEach(callback => callback());
    await new Promise(resolve => setImmediate(resolve));
    const markup = renderToStaticMarkup(render());
    assert.match(markup, new RegExp(domain));
    assert.doesNotMatch(markup, /Connect a model|Connect your AI service|Meet Charlie/);
  }
});

test('first-run form prefills saved choices and uses backend catalog models across domains', async () => {
  for (const domain of ['Warehouse planning', 'Family gardening']) for (const saved of [true, false]) for (const selected of ['anthropic', 'azure_openai']) {
    let cursor = 0;
    const states = [], effects = [], tested = [];
    const components = load('LocalSetup.tsx', {modules: {
      react: {
        useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value;}]; },
        useRef(initial) { return states[cursor++] ??= { current: initial }; },
        useEffect(callback) { effects.push(callback); },
      },
      '@/lib/client': {api: async path => ({ok: true, data: path.endsWith('/settings/catalog') ? {sections: [{id: 'providers', unlocks: [], fields: []}]}
        : path.endsWith('/llm-catalog') ? {providers: {openai: {models: {chat: `${domain} catalog model`}}, anthropic: {models: {chat: `${domain} other model`}}}}
        : {configured: false, values: saved ? {provider: selected, model: `${domain} saved model`, base_url: 'https://model.example/v1'} : {}, secrets_set: {api_key: saved}}})},
      './localProviders': {testProvider: async value => tested.push(value)},
    }});
    const render = () => {cursor = 0; return components.LocalSetup({onConfigured() {}});};
    render(); effects.splice(0).forEach(callback => callback());
    await new Promise(resolve => setImmediate(resolve));
    const form = render().props.children.find(node => node?.type === 'form');
    const field = id => form.props.children.flat(Infinity).find(node => node?.props?.id === id);
    assert.equal(field('provider').props.value, saved ? selected : 'openai');
    assert.equal(field('model').props.value, `${domain} ${saved ? 'saved' : 'catalog'} model`);
    if (saved) {
      assert.equal(field('api_key').props.required, false);
      if (selected === 'azure_openai') assert.equal(field('base_url').props.value, 'https://model.example/v1');
      await form.props.children.flat(Infinity).find(node => node?.type === 'button' && node.props.children === 'Test connection').props.onClick();
      assert.equal(tested[0].base_url, 'https://model.example/v1');
      assert.equal('api_key' in tested[0], false, 'Your saved key is kept, never returned to the form');
    }
    field('provider').props.onChange({target: {value: 'anthropic'}});
    assert.equal(render().props.children.find(node => node?.type === 'form').props.children.flat(Infinity).find(node => node?.props?.id === 'model').props.value, `${domain} other model`);
  }
});

test("your completed setup hands focus to your workspace and announces invalid fields across domains", async () => {
  const source = readFileSync(join(__dirname, "LocalSetup.tsx"), "utf8");
  const tree = ts.createSourceFile("LocalSetup.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let handoff;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === "useEffect" && node.arguments[0].getText(tree).includes("workspaceRef.current")) handoff = node.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  for (const domain of ["invoice-workflow.invalid", "home-gardening.invalid"]) {
    assert.ok(handoff, domain);
    const focus = [], target = { focus() { focus.push(domain); } };
    const context = { configured: false, finishedSetup: { current: true }, workspaceRef: { current: { querySelector: () => target } } };
    const update = vm.runInNewContext(ts.transpileModule(`(${handoff})`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
    update();
    assert.deepEqual(focus, []);
    context.configured = true;
    context.finishedSetup.current = false;
    update();
    assert.deepEqual(focus, []);
    context.finishedSetup.current = true;
    update(); update();
    assert.deepEqual(focus, [domain]);
    assert.equal(target.tabIndex, -1);

    let requests = 0;
    const setup = setupHarness(domain, { testProvider: async () => { requests++; } });
    await setup.form().props.onSubmit({ preventDefault() {} });
    assert.equal(setup.field("api_key").props["aria-invalid"], true);
    assert.equal(setup.field("model").props["aria-invalid"], true);
    assert.equal(setup.field("api_key").props["aria-describedby"], "key-help local-setup-error");
    setup.change("provider", "azure_openai");
    assert.equal(setup.field("api_key").props["aria-invalid"], false);
    setup.change("api_key", "test-only-private");
    setup.change("model", " ");
    await setup.form().props.onSubmit({ preventDefault() {} });
    assert.equal(setup.field("model").props["aria-invalid"], true);
    assert.equal(setup.field("model").props["aria-describedby"], "local-setup-error");
    assert.equal(setup.field("base_url").props["aria-invalid"], true);
    assert.equal(setup.field("api_key").props["aria-invalid"], false);
    setup.change("model", domain);
    setup.change("base_url", `https://${domain}/v1?extra=1`);
    const testConnection = () => setup.nodes().find(node => node?.type === "button" && node.props.type === "button").props.onClick();
    await testConnection();
    assert.equal(requests, 0);
    assert.equal(setup.field("base_url").props["aria-invalid"], true);
    assert.equal(setup.field("base_url").props["aria-describedby"], "local-setup-error");
    assert.equal(setup.field("model").props["aria-invalid"], false);
    assert.match(renderToStaticMarkup(setup.render()), /role="alert" id="local-setup-error"/);
    for (let attempt = 0; attempt < 2; attempt++) setup.form().props.onInvalid({ target: { name: "api_key" } });
    assert.equal(setup.field("api_key").props["aria-invalid"], true);
    assert.match(renderToStaticMarkup(setup.render()), /Check your required service details, then try again/);
    setup.change("base_url", `https://${domain}/v1`);
    await testConnection();
    assert.equal(requests, 1);
    for (const field of ["api_key", "model", "base_url"]) assert.equal(setup.field(field).props["aria-invalid"], false);
    assert.doesNotMatch(renderToStaticMarkup(setup.render()), /local-setup-error|role="alert"/);
  }
});

test('your local setup error points to the required details without claiming hidden fields are below', async () => {
  for (const domain of ['warehouse teams', 'weekend walkers']) {
    const setup = setupHarness(domain);
    await setup.form().props.onSubmit({preventDefault() {}});
    const error = setup.nodes().find(node => node?.props?.source === 'local_setup');
    assert.match(error.props.error, /Enter your model.*then try again/);
    assert.doesNotMatch(error.props.error, /shown below/, domain);
  }
});

test('your Tauri local stack renders and starts server-authorized sample mode across domains', async () => {
  for (const domain of ['warehouse-software.invalid', 'weekend-walks.invalid']) {
    const requests = [];
    const providers = load('localProviders.ts', {window: {__TAURI__: {core: {}}, __SUPYMEM__: {mode: 'local'}, location: {protocol: 'http:'}},
      modules: {'@/lib/client': {api: async () => assert.fail('Your local stack uses its same-origin proxy')}},
      fetch: async (path, init) => {
        requests.push({path, init});
        return {ok: true, status: 200, json: async () => ({configured: requests.some(request => request.path.endsWith('/sample')), sample_mode: true})};
      }, setTimeout: () => 1, clearTimeout() {}});
    let allowed, continued = 0;
    assert.equal(await providers.providerConfigured(value => {allowed = value;}), false);
    const setup = setupHarness(domain, {...providers, sampleMode: allowed, onConfigured: () => {continued++;}});
    const button = setup.render().props.children.find(node => node?.type === 'button' && node.props.children === 'Try without a key (sample mode)');
    assert.ok(button, domain); await button.props.onClick();
    assert.equal(continued, 1);
    assert.deepEqual(requests.map(request => request.path), ['/api/local/providers', '/api/local/providers/sample', '/api/local/providers']);
  }
});

test("your picker uses supported providers, catalog defaults and optional laptop keys across domains", async () => {
  for (const domain of ["invoice-workflow.invalid", "home-gardening.invalid"]) {
    const setup = setupHarness(domain);
    await setup.load();
    assert.equal(setup.field("provider").type, "select");
    assert.deepEqual(Array.from(setup.field("provider").props.children, node => node.props.value), ["openai", "azure_openai", "anthropic", "openrouter", "ollama", "lmstudio", "openai_compatible"]);
    for (const provider of ["openai", "azure_openai", "anthropic", "openrouter", "ollama", "lmstudio"]) {
      setup.change("provider", provider);
      assert.ok(setup.field("model").props.value);
      assert.equal(setup.field("api_key").props.required, !["ollama", "lmstudio"].includes(provider));
      assert.equal(!!setup.field("base_url"), ["azure_openai", "ollama", "lmstudio"].includes(provider));
      setup.change("model", domain);
      assert.equal(setup.field("model").props.value, domain);
    }
    const html = renderToStaticMarkup(setup.render());
    assert.match(html, /charlie\/poster.png/);
    assert.match(readFileSync(join(__dirname, "LocalSetup.tsx"), "utf8"), /CharlieMascot/);
    assert.doesNotMatch(html, /\.env|LLM_|\u2014/);
  }
});

test("your Anthropic setup uses the native service and catalog model across domains", async () => {
  for (const domain of ["warehouse-routing.invalid", "weekend-cooking.invalid"]) {
    const requests = [];
    const setup = setupHarness(domain, { testProvider: async value => requests.push(value) });
    await setup.load();
    setup.change("provider", "anthropic");
    assert.ok(!setup.field("base_url"));
    assert.equal(setup.field("model").props.value, `${domain} model`);
    assert.doesNotMatch(renderToStaticMarkup(setup.render()), /compatible endpoint/i);
    await setup.nodes().find(node => node.type === "button" && node.props.children === "Test connection").props.onClick();
    assert.equal(requests.length, 0);
    setup.change("api_key", "synthetic-key");
    setup.change("model", domain);
    await setup.nodes().find(node => node.type === "button" && node.props.children === "Test connection").props.onClick();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].base_url, "https://api.anthropic.com/v1");
    assert.equal(requests[0].model, domain);
  }
});

test("test connection sends your draft once and offers a next step without saving across domains", async () => {
  for (const domain of ["invoice-workflow.invalid", "home-gardening.invalid"]) {
    const drafts = [];
    let finish;
    const setup = setupHarness(domain, { testProvider: draft => { drafts.push(draft); return new Promise(resolve => { finish = resolve; }); }, saveProvider() { assert.fail("Testing must not save"); } });
    setup.change("api_key", "test-only-private");
    setup.change("model", domain);
    const button = () => setup.nodes().find(node => node?.type === "button" && /Test connection|Testing/.test(node.props.children));
    assert.equal(button().props.type, "button");
    const pending = button().props.onClick();
    await button().props.onClick();
    assert.equal(drafts.length, 1);
    assert.equal(drafts[0].model, domain);
    finish();
    await pending;
    assert.match(renderToStaticMarkup(setup.render()), /Save and continue/);
    assert.match(renderToStaticMarkup(setup.render()), /connection works/i);
    const feedback = setup.nodes().find(node => node?.props?.role === "status");
    assert.doesNotMatch(feedback.props.children, /test-only-private/);
  }
});

test("save and continue needs a successful answer for the current model fields", async () => {
  for (const domain of ["cargo-planning.invalid", "home-pottery.invalid"]) {
    const tested = [], saved = [];
    const setup = setupHarness(domain, {
      testProvider: async draft => { tested.push(draft.model); if (draft.model === domain) throw new Error("Load your model, then try again."); },
      saveProvider: async draft => saved.push(draft.model), providerConfigured: async () => true,
    });
    setup.change("provider", "ollama");
    setup.change("model", domain);
    await setup.form().props.onSubmit({ preventDefault() {} });
    assert.deepEqual(tested, [domain]);
    assert.deepEqual(saved, []);
    assert.match(JSON.stringify(setup.render()), /Load your model/);
    setup.change("model", `${domain}-working`);
    await setup.nodes().find(node => node.props.children === "Test connection").props.onClick();
    setup.change("model", domain);
    await setup.form().props.onSubmit({ preventDefault() {} });
    assert.deepEqual(tested, [domain, `${domain}-working`, domain]);
    assert.deepEqual(saved, []);
  }
});

test("you can cancel testing without losing fields or accepting a late result", async () => {
  for (const domain of ["fleet-coordination.invalid", "family-reading.invalid"]) {
    let finish;
    const setup = setupHarness(domain, { testProvider: () => new Promise(resolve => { finish = resolve; }) });
    setup.change("provider", "ollama");
    setup.change("model", domain);
    const pending = setup.nodes().find(node => node.props.children === "Test connection").props.onClick();
    const cancel = setup.nodes().find(node => node.props.children === "Cancel test");
    assert.ok(cancel, domain);
    cancel.props.onClick();
    assert.equal(setup.field("model").props.value, domain);
    assert.equal(setup.field("model").props.disabled, false);
    assert.equal(setup.render().props.children.find(node => node?.props?.children === "Try without a key (sample mode)").props.disabled, false);
    finish();
    await pending;
    assert.doesNotMatch(JSON.stringify(setup.render()), /connection works/);
  }
});

test("sample mode is available in a live local install and checks your saved readiness across domains", async () => {
  for (const domain of ["invoice-workflow.invalid", "home-gardening.invalid"]) {
    let sampled = 0, continued = 0;
    for (const sampleMode of [false, true]) {
      const setup = setupHarness(domain, { sampleMode, trySampleMode: async () => { sampled++; }, providerConfigured: async () => true, onConfigured: () => { continued++; } });
      const button = setup.render().props.children.find(node => node?.type === "button" && node.props.children === "Try without a key (sample mode)");
      assert.equal(!!button, true);
      assert.match(renderToStaticMarkup(setup.render()), /sample data/i);
      assert.match(renderToStaticMarkup(setup.render()), /Settings/);
      if (button) await button.props.onClick();
    }
    assert.equal(sampled, 2);
    assert.equal(continued, 2);
  }
});

test("a hanging provider check times out with Retry and ignores late results across domains", async () => {
  for (const domain of ["dispatch-software.invalid", "garden-club.invalid"]) for (const sample of [false, true]) {
    let cursor = 0, abort, finishRequest, hanging = true, requests = 0;
    const states = [], effects = [], dependencies = [], cleanups = [];
    const request = async () => {
      requests++;
      if (hanging) return new Promise(resolve => { finishRequest = resolve; });
      return Response.json({ configured: true, sample_selected: sample });
    };
    const providers = load("localProviders.ts", {
      setTimeout(callback, milliseconds) {
        assert.equal(milliseconds, 15_000);
        abort = callback;
        return 1;
      }, clearTimeout() {},
      fetch: request, modules: { "@/lib/client": { api: request } },
    });
    const components = load("LocalSetup.tsx", { modules: {
      "./localProviders": providers,
      react: {
        useState(initial) {
          const index = cursor++;
          if (!(index in states)) states[index] = initial;
          return [states[index], value => { states[index] = value; }];
        },
        useRef(initial) {
          const index = cursor++;
          if (!(index in states)) states[index] = { current: initial };
          return states[index];
        },
        useEffect(callback, deps) {
          const index = cursor++;
          if (!dependencies[index] || deps.some((value, position) => value !== dependencies[index][position])) {
            dependencies[index] = deps;
            effects.push(() => { cleanups[index]?.(); cleanups[index] = callback(); });
          }
        },
      },
    } });
    const child = React.createElement("div", null, domain);
    const render = () => { cursor = 0; return components.LocalProviderGate({ children: child }); };
    const settle = () => new Promise(resolve => setImmediate(resolve));
    assert.equal(render().props.role, "status");
    effects.splice(0).forEach(effect => effect());
    abort();
    await settle();
    const failed = render();
    const html = renderToStaticMarkup(failed);
    assert.match(html, /role="alert"/);
    assert.match(html, /try again/i);
    assert.doesNotMatch(html, /Starting your app/);
    const retry = failed.props.children.find(node => node?.type === "button");
    assert.equal(retry.props.children, "Retry");
    finishRequest(Response.json({ configured: true }));
    await settle();
    assert.notEqual(render(), child);
    hanging = false;
    retry.props.onClick();
    render();
    effects.splice(0).forEach(effect => effect());
    await settle();
    const ready = render();
    assert.equal(ready.props.style.display, sample ? "grid" : "contents");
    if (sample) {
      assert.equal(ready.props.className, "local-sample-workspace");
      assert.equal(ready.props.children.at(-1), child);
      assert.match(renderToStaticMarkup(ready), /You are viewing sample data and answers/);
      assert.match(readFileSync(join(__dirname, "../../styles/tokens.css"), "utf8"), /\.local-sample-workspace \.app-shell \{ height: 100%; min-height: 0;/);
    } else assert.equal(ready.props.children.at(-1), child);
    assert.equal(requests, 2);
    cleanups.forEach(cleanup => cleanup?.());
  }
});

test("unconfigured setup has accessible inputs, actionable errors, and no native bridge calls", () => {
  const component = load("LocalSetup.tsx", { modules: { "./localProviders": {} } });
  const html = renderToStaticMarkup(React.createElement(component.LocalSetup, { onConfigured() {} }));
  assert.match(html, /<form/);
  assert.match(html, /type="password"/);
  for (const name of ["provider", "api_key", "model"]) {
    assert.match(html, new RegExp(`id="${name}"`));
    assert.match(html, new RegExp(`for="${name}"`));
  }
  const source = readFileSync(join(__dirname, "LocalSetup.tsx"), "utf8");
  assert.doesNotMatch(source, /desktopInvoke|__TAURI|localStorage|sessionStorage|\u2014/);
  assert.match(source, /setApiKey\(""\)/);
});

test("your setup errors use the shared notice across business and consumer services", () => {
  for (const domain of ["dispatch-software.invalid", "garden-club.invalid"]) {
    const notices = [], error = `Your service at ${domain} is not ready. Check your settings, then try again.`;
    const components = load("LocalSetup.tsx", { modules: {
      "./localProviders": {},
      "@/components/primitives/ErrorNotice": { ErrorNotice({ error, source }) {
        notices.push({ error, source });
        return React.createElement("p", { role: "alert" }, error);
      } },
    } });
    const html = renderToStaticMarkup(React.createElement(components.LocalSetup, { error, onConfigured() {} }));
    assert.deepEqual(notices, [{ error, source: "local_setup" }]);
    assert.match(html, /role="alert"/);
    assert.ok(html.includes(error));
  }
});

test("first run checks configuration, saves once, clears the key, and continues across domains", async () => {
  for (const domain of ["dispatch-software.invalid", "garden-club.invalid"]) {
    let cursor = 0, configured = false, saved, finishSave, continued = 0;
    const states = [], effects = [];
    const react = {
      useState(initial) {
        const index = cursor++;
        if (!(index in states)) states[index] = initial;
        return [states[index], value => { states[index] = value; }];
      },
      useRef(initial) {
        const index = cursor++;
        if (!(index in states)) states[index] = { current: initial };
        return states[index];
      },
      useEffect(callback) { effects.push(callback); },
    };
    const components = load("LocalSetup.tsx", { modules: {
      react,
      "./localProviders": {
        providerConfigured: async () => configured,
        testProvider: async () => {},
        saveProvider: details => { saved = details; return new Promise(resolve => { finishSave = resolve; }); },
      },
    } });
    const render = component => { cursor = 0; return component(); };
    const child = React.createElement("div", null, domain);
    let view = render(() => components.LocalProviderGate({ children: child }));
    assert.equal(view.props.role, "status");
    effects.shift()();
    await new Promise(resolve => setImmediate(resolve));
    view = render(() => components.LocalProviderGate({ children: child }));
    assert.equal(view.type.name, "CharlieIntroWelcome");
    assert.equal(view.props.sampleEntry.props.children, "Try without a key (sample mode)");
    assert.equal(configured, false);
    assert.equal(saved, undefined);
    view.props.onConnect();
    view = render(() => components.LocalProviderGate({ children: child }));
    assert.equal(view.type, components.LocalSetup);
    states.length = 0;
    const setup = () => components.LocalSetup({ onConfigured: () => { continued++; } });
    const form = () => render(setup).props.children.find(node => node?.type === "form");
    for (const [name, value] of [["provider", "azure_openai"], ["api_key", "fixture-private"], ["model", domain], ["base_url", `https://${domain}/v1`]]) {
      form().props.children.find(node => node?.props?.id === name).props.onChange({ target: { value } });
    }
    const pending = form().props.onSubmit({ preventDefault() {} });
    assert.equal(form().props["aria-busy"], true);
    await form().props.onSubmit({ preventDefault() {} });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(saved.model, domain);
    assert.equal(saved.api_key, "fixture-private");
    assert.equal(form().props["aria-busy"], true);
    configured = true;
    finishSave();
    await pending;
    assert.equal(continued, 0);
    const optional = render(setup);
    assert.equal(optional.props.children[0].props.optional, true);
    assert.ok(!JSON.stringify(optional).includes("fixture-private"));
    optional.props.children[1].props.onClick();
    assert.equal(continued, 1);
  }
});

test("your first-run model guide comes from the catalog and does not require optional accounts across domains", async () => {
  for (const domain of ["Dispatch planning", "Garden lessons"]) {
    const states = [], effects = [], calls = [];
    let cursor = 0, available = true;
    const guide = { id: "providers", description: domain + " model connection", unlocks: ["Create content"],
      data_sent: "Your prompts go to the service you choose.", url: "https://accounts.invalid/keys", help_text: "Get an API key",
      provider_urls: { azure_openai: "https://accounts.invalid/azure", openai: "https://accounts.invalid/openai" } };
    const components = load("LocalSetup.tsx", { modules: {
      react: {
        useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = value; }]; },
        useEffect(effect) { effects.push(effect); }, useRef: initial => ({ current: initial }),
      },
      "@/lib/client": { api: async path => { calls.push(path); return { ok: available, data: { sections: [guide] } }; } },
    } });
    const render = () => { cursor = 0; return components.LocalSetup({ onConfigured() {} }); };
    render();
    effects.shift()();
    await new Promise(resolve => setImmediate(resolve));
    const tree = render();
    assert.deepEqual(calls, ["/api/local/settings/catalog", "/api/local/providers", "/api/workspaces/local/llm-catalog"]);
    assert.ok(JSON.stringify(tree).includes(guide.description));
    assert.ok(JSON.stringify(tree).includes(guide.data_sent));
    assert.match(renderToStaticMarkup(tree), /<a[^>]+>Get an API key<\/a>/);
    assert.equal(tree.props.children.filter(node => node?.type === "form").length, 1);
    assert.ok(!JSON.stringify(tree).includes("smtp-host"));
    states.length = 0;
    effects.length = 0;
    available = false;
    render();
    effects.shift()();
    await new Promise(resolve => setImmediate(resolve));
    assert.match(JSON.stringify(render()), /Restart your local app.*Keys and connections/);
  }
});
