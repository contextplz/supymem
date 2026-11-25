const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const jsx = (type, props, key) => ({ type, props: props || {}, key });
const nodes = tree => !tree || typeof tree !== "object" ? [] : [tree, ...[tree.props?.children].flat(Infinity).flatMap(nodes)];
const settle = () => new Promise(resolve => setImmediate(resolve));

test('local settings use the shared workspace shell across domains', () => {
  for (const domain of ['Supply planning', 'Home gardening']) {
    const exported = {};
    vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, 'layout.tsx'), 'utf8'), {
      compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
    }).outputText, {exports: exported, require: name => name === 'react/jsx-runtime' ? {jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})}
      : name.endsWith('LocalModeBoundary') ? {useLocalMode: () => true} : {AppShell: 'hosted-shell'}});
    const tree = exported.default({children: domain});
    assert.equal(tree.type, 'hosted-shell');
    assert.equal(tree.props.children, domain);
    assert.doesNotMatch(JSON.stringify(tree), /Companies|CharlieDock/);
  }
});

test('connections follow your AI, search, mail, publishing, apps, images, spend and data order', async () => {
  for (const domain of ['Fleet coordination', 'Family holidays']) {
    const expected = ['providers', 'search', 'smtp', 'publishing', 'composio', 'images', 'spend', 'data'];
    const sections = expected.slice().reverse().map(id => ({id, title: domain, fields: []}));
    const tree = await harness('LocalSettings.tsx', {ok: true, data: {env_path: '/local/.env', sections}}).load();
    assert.deepEqual(nodes(tree).filter(node => node.props.section).map(node => node.props.section.id), expected);
  }
});

test('unshipped settings sections disappear while your coding tool and keys remain across domains', async () => {
  for (const domain of ['Stockroom scheduling', 'Family recipes']) {
    const sections = ['providers', 'search', 'smtp', 'voice', 'social', 'ads', 'admin', 'billing'].map(id => ({id, title: domain, fields: []}));
    const app = harness('LocalSettings.tsx', {ok: true, data: {env_path: '/local/.env', sections}});
    const tree = await app.load();
    assert.deepEqual(nodes(tree).filter(node => node.props.section).map(node => node.props.section.id), ['providers', 'search', 'smtp']);
    assert.ok(nodes(tree).some(node => node.type === app.CodingAgentTiles));
  }
});
const ErrorNotice = () => null;
const testSections = [
  ["providers", ["provider", "model", "base_url", "api_key"]],
  ["smtp", ["host", "port", "user", "password", "sender_name", "postal_address", "imap_host", "imap_port", "booking_link"]],
  ["publishing", ["provider", "project", "token", "bucket", "region", "prefix", "site_url", "access_key_id", "secret_access_key", "session_token", "waitlist_email"]],
  ["search", ["provider", "api_key", "firecrawl_url", "firecrawl_key"]],
  ["composio", ["api_key", "auth_configs"]], ["spend", ["monthly_spend_cap"]], ["data", ["backup_directory"]],
].map(([id, fields]) => ({ id, title: `Your ${id}`, path: id === "providers" ? "/api/local/providers" : `/api/local/settings/${id}`,
  description: id === "data" ? "Stop your app before replacing your data. Keep your encryption key private."
    : id === "spend" ? "Your app checks this limit before paid calls. Set your provider's limit too." : "Connect your own account.",
  state: id === "search" ? "ready" : "needs_setup", next_step: "Open Keys and connections and add your account details.",
  unlocks: ["Research"], url: "https://accounts.invalid/keys", help_text: id === "smtp" ? "Create an app password" : id === "publishing" ? "Create a token" : "Get an API key", data_sent: "Testing only checks your connection.",
  fields: fields.map(name => ({ name, label: name, secret: ["api_key", "password", "token", "access_key_id", "secret_access_key", "session_token", "firecrawl_key"].includes(name),
    purpose: `Your ${name} setting`, required: false, state: "not set", env: [name.toUpperCase()],
    type: name === "auth_configs" || name === "postal_address" ? "textarea" : name === "monthly_spend_cap" ? "number" : undefined,
    providers: id === "search" ? ({ api_key: ["tavily", "exa"] })[name]
      : id === "publishing" ? ({ project: ["vercel", "netlify"], token: ["vercel", "netlify"], bucket: ["s3"], region: ["s3"], prefix: ["s3"], site_url: ["s3"], access_key_id: ["s3"], secret_access_key: ["s3"], session_token: ["s3"] })[name] : undefined,
    options: ["search", "publishing"].includes(id) && name === "provider" ? (id === "search" ? ["builtin", "tavily", "exa", "openai", "firecrawl"] : ["vercel", "netlify", "s3"]).map(value => ({ value, label: value })) : undefined })),
}));

function harness(file, response, props = {}, mode = "", local = false) {
  const states = [], effects = [], refs = [], calls = [];
  let hook = 0;
  const exports = {};
  const LocalSettings = () => null;
  const SpendSection = () => null;
  const CodingAgentTiles = () => null;
  vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, file), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, {
    exports, Error, AbortController, process: { env: { NEXT_PUBLIC_SUPYMEM_MODE: mode } }, navigator: { clipboard: { writeText: async text => calls.push({ copied: text }) } },
    require(name) {
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name === "react") return {
        Fragment: "fragment",
        useState(initial) { const index = hook++; if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial; return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }]; },
        useRef(initial) { const index = hook++; if (!(index in refs)) refs[index] = { current: initial }; return refs[index]; },
        useEffect(effect, deps) { const index = hook++; if (!states[index] || deps.some((value, position) => value !== states[index][position])) { states[index] = deps; effects.push(effect); } },
      };
      if (name === "@/lib/client") return { api: async (path, init = {}) => { calls.push({ path, init }); return typeof response === "function" ? response(path, init) : response; } };
      if (name === "@/components/primitives/ErrorNotice") return { ErrorNotice };
      if (name === "../../setup/localProviders") {
        const providerExports = {};
        vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, "../../setup/localProviders.ts"), "utf8"), {
          compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
        }).outputText, { exports: providerExports, Error, AbortController, AbortSignal, setTimeout, clearTimeout,
          window: { location: { protocol: "tauri:" } }, require(dependency) {
            if (dependency === "@/lib/client") return { api: async (path, init = {}) => { calls.push({ path, init }); return typeof response === "function" ? response(path, init) : response; } };
            if (dependency === "@/lib/chatStop") return { waitForChatRequest: promise => promise };
            throw new Error(dependency);
          } });
        return providerExports;
      }
      if (name === "./LocalSettings") return { LocalSettings };
      if (name.endsWith("/LocalModeBoundary")) return { useLocalMode: () => local || mode === "local" || response?.ok && response.data?.local_mode === true,
        useFeatureVisibility: () => (surface, id) => !["voice", "ads", "social", "admin", "billing"].includes(id) };
      if (name === "./local.module.css") return { default: { actions: "actions" } };
      if (name === "./sections") return require(join(__dirname, "sections.ts"));
      if (name === "./SettingsSection") return { SettingsSection: () => null };
      if (name === "./DataSettings") return { DataSettings: () => null };
      if (name === "@/components/credits/SpendSection") return { SpendSection };
      if (name === "@/components/connections/CodingAgentTiles") return { CodingAgentTiles };
      throw new Error(name);
    },
  });
  const render = () => { hook = 0; return (exports.default || exports.SettingsSection || exports.LocalSettings)(props); };
  return { render, calls, LocalSettings, SpendSection, CodingAgentTiles, async load() { render(); effects.splice(0).forEach(effect => effect()); await settle(); return render(); } };
}

test("you can connect your coding tool from the local Keys and connections screen", async () => {
  for (const domain of ["Warehouse inventory", "Home fitness"]) {
    const app = harness("LocalSettings.tsx", { ok: true, data: { env_path: "/private/.env", sections: [], title: domain } });
    assert.ok(nodes(await app.load()).some(node => node.type === app.CodingAgentTiles), domain);
  }
});

test("laptop presets send optional keys to their own service for Test and Save", async () => {
  for (const [domain, preset, address] of [["mill-dispatch", "ollama", "http://localhost:11434/v1"],
    ["balcony-herbs", "lmstudio", "http://localhost:1234/v1"]]) {
    const section = testSections[0];
    const values = { provider: "openai_compatible", model: domain, base_url: address };
    const app = harness("SettingsSection.tsx", (path) => ({ ok: true, data: path.endsWith("/test") ? { ok: true }
      : path.endsWith("/catalog") ? { sections: [section] } : { values, secrets_set: {} } }), { section });
    await app.load();
    const node = id => nodes(app.render()).find(item => item.props.id === `providers-${id}`);
    assert.equal(node("provider").props.value, preset);
    const key = node("api_key");
    assert.equal(key?.props.type, "password");
    const testConnection = () => nodes(app.render()).find(item => item.type === "button" && item.props.children === "Test your connection").props.onClick();
    await testConnection();
    assert.equal(JSON.parse(app.calls.at(-1).init.body).api_key, undefined);
    node("provider").props.onChange({ target: { value: "openai_compatible" } });
    node("base_url").props.onChange({ target: { value: address } });
    assert.equal(node("provider").props.value, "openai_compatible");
    node("provider").props.onChange({ target: { value: preset } });
    node("model").props.onChange({ target: { value: domain } });
    node("api_key").props.onChange({ target: { value: `private-key-${domain}` } });
    await testConnection();
    const tested = JSON.parse(app.calls.at(-1).init.body);
    await nodes(app.render()).find(item => item.type === "form").props.onSubmit({ preventDefault() {} });
    const saved = JSON.parse(app.calls.find(call => call.init.method === "PUT").init.body);
    for (const body of [tested, saved]) {
      assert.equal(body.api_key, `private-key-${domain}`);
      assert.equal(body.base_url, address);
    }
  }
});

test("provider tests show safe recovery steps and installed model choices", async () => {
  for (const domain of ["mill-dispatch", "balcony-herbs"]) {
    const section = testSections[0];
    const upstream = `private upstream diagnostic for ${domain}`;
    const responses = [{ ok: false, status: 502, data: { detail: { code: "model_not_found", message: upstream, models: [domain] } } },
      { ok: false, status: 409, data: { detail: { code: "local_spend_limit", message: upstream } } },
      { ok: true, data: { ok: true, models: [domain, "not a model ID", 81] } }];
    const app = harness("SettingsSection.tsx", path => path.endsWith("/test") ? responses.shift()
      : { ok: true, data: { values: { provider: "openai_compatible", base_url: "http://localhost:19621/v1", model: `${domain}-missing` }, secrets_set: {}, configured: true } }, { section });
    await app.load();
    for (const copy of [/Chat model.*Keys and connections|Keys and connections.*Chat model/, /Monthly limit \(USD\)/, new RegExp(`Installed models: ${domain}`)]) {
      await nodes(app.render()).find(item => item.type === "button" && item.props.children === "Test your connection").props.onClick();
      const tree = JSON.stringify(app.render());
      assert.match(tree, copy);
      assert.doesNotMatch(tree, /private upstream diagnostic|not a model ID/);
    }
  }
});

test("your capabilities link is rendered inside the connections screen", async () => {
  for (const domain of ["Industrial procurement", "Family cooking"]) {
    const app = harness("LocalSettings.tsx", { ok: true, data: { env_path: "/private/.env", sections: [], title: domain } });
    const tree = await app.load();
    assert.ok(nodes(tree).some(node => node.type === "a" && node.props.href === "/settings/local/capabilities"));
  }
});

test("your mail sender fields explain the postal requirement and save your profile across domains", async () => {
  for (const domain of ["Industrial procurement", "Family cooking"]) {
    const section = testSections.find(item => item.id === "smtp");
    const values = { sender_name: domain, postal_address: `${domain} mailing address\n123 Main Street` };
    const app = harness("SettingsSection.tsx", { ok: true, data: { values, secrets_set: {} } }, { section });
    let tree = await app.load();
    assert.equal(nodes(tree).find(node => node.props.id === "smtp-sender_name").props.value, values.sender_name);
    const address = nodes(tree).find(node => node.props.id === "smtp-postal_address");
    assert.equal(address.type, "textarea");
    assert.equal(address.props.value, values.postal_address);
    assert.equal(address.props["aria-describedby"], "smtp-postal_address-help");
    assert.match(JSON.stringify(nodes(tree).find(node => node.props.id === "smtp-postal_address-help")), /cold email laws require your valid postal address/i);
    nodes(tree).find(node => node.props.id === "smtp-sender_name").props.onChange({ target: { value: domain + " owner" } });
    address.props.onChange({ target: { value: values.postal_address + "\nSuite 2" } });
    tree = app.render();
    await nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    const saved = app.calls.find(call => call.init.method === "PUT");
    assert.equal(saved.path, "/api/local/settings/smtp");
    const body = JSON.parse(saved.init.body);
    assert.equal(body.sender_name, domain + " owner");
    assert.equal(body.postal_address, values.postal_address + "\nSuite 2");
  }
});

test("image credentials link to their own services without an unsupported probe", async () => {
  for (const domain of ["Industrial procurement", "Family cooking"]) {
    const section = { ...testSections[0], id: "images", title: domain, path: "/api/local/settings/images",
      fields: ["bfl", "gemini", "pexels", "pixabay"].map(provider => ({ name: provider + "_api_key", secret: true,
        label: provider, purpose: "Your image account key", env: [], state: "not set", url: `https://${provider}.invalid/keys` })) };
    const app = harness("SettingsSection.tsx", { ok: true, data: { values: {}, secrets_set: {} } }, { section });
    const tree = await app.load();
    for (const field of section.fields) {
      assert.ok(nodes(tree).some(node => node.type === "a" && node.props.href === field.url));
    }
    assert.ok(!nodes(tree).some(node => node.type === "button" && node.props.children === "Test your connection"));
  }
});

test("connection loading and env import failures use the shared error notice", async () => {
  for (const domain of ["Industrial procurement", "Family cooking"]) {
    for (const response of [{ ok: false, data: { detail: domain + " unavailable" } },
      { ok: true, data: { env_path: "/private/.env", sections: [], import_error: domain + " invalid file" } }]) {
      const app = harness("LocalSettings.tsx", response);
      const tree = await app.load();
      const notice = nodes(tree).find(node => node.type === ErrorNotice && node.props.error);
      assert.ok(notice, domain);
      assert.equal(notice.props.source, "settings.local.connections");
      assert.equal(notice.props.intent, response.ok ? "connections.import" : "connections.load");
      assert.match(notice.props.error, response.ok ? /edit your connection here/ : /Try again/);
      assert.ok(!nodes(tree).some(node => node.props.role === "alert"));
    }
  }
});

test("you can leave sample mode with guided provider details and a connection test across domains", async () => {
  const SECTIONS = testSections;
  for (const domain of ["Industrial procurement", "Family cooking"]) {
    const app = harness("SettingsSection.tsx", (path, init) => path.endsWith("/test") ? { ok: true, data: { ok: true } }
      : path === '/api/local/providers' ? {ok: true, data: {values: {}, secrets_set: {}, default_models: {anthropic: [`${domain} model`]}}}
      : {ok: false, status: 404}, { section: SECTIONS[0] });
    let tree = await app.load();
    assert.deepEqual(app.calls.map(call => call.path), ['/api/local/providers']);
    const select = nodes(tree).find(node => node.type === "select" && node.props.id === "providers-provider");
    assert.ok(select, domain);
    select.props.onChange({ target: { value: "anthropic" } });
    tree = app.render();
    assert.equal(nodes(tree).find(node => node.props.id === "providers-model").props.value, `${domain} model`);
    assert.ok(!nodes(tree).some(node => node.props.id === "providers-base_url"));
    assert.doesNotMatch(JSON.stringify(tree), /compatible endpoint/i);
    nodes(tree).find(node => node.props.id === "providers-api_key").props.onChange({ target: { value: domain + " private" } });
    await nodes(app.render()).find(node => node.type === "button" && node.props.children === "Test your connection").props.onClick();
    assert.equal(app.calls.at(-1).path, "/api/local/providers/test");
    assert.equal(JSON.parse(app.calls.at(-1).init.body).provider, "anthropic");
    assert.equal(JSON.parse(app.calls.at(-1).init.body).base_url, "https://api.anthropic.com/v1");
    assert.match(JSON.stringify(app.render()), /connection.*working/i);
    const saved = harness("SettingsSection.tsx", path => path.endsWith("/test") ? { ok: true, data: { ok: true } }
      : { ok: true, data: { values: { provider: "anthropic", model: "claude-sonnet-5-5", base_url: "https://api.anthropic.com/v1" }, secrets_set: { api_key: true } } }, { section: SECTIONS[0] });
    await saved.load();
    await nodes(saved.render()).find(node => node.type === "button" && node.props.children === "Test your connection").props.onClick();
    assert.ok(!("api_key" in JSON.parse(saved.calls.at(-1).init.body)), domain);
    assert.match(JSON.stringify(saved.render()), /connection.*working/i);
  }
});

test("setup guidance names fields below without sending you back to this screen", async () => {
  for (const domain of ["Warehouse planning", "Home fitness"]) {
    const section = { ...testSections[1], title: domain, missing: ["host", "password"], next_step: "Open Keys and connections, then add your host, password." };
    const app = harness("SettingsSection.tsx", { ok: true, data: { values: {}, secrets_set: {}, state: "needs_setup", missing: ["host", "password"] } }, { section });
    const statuses = nodes(await app.load()).filter(node => node.props.role === "status");
    assert.match(JSON.stringify(statuses), /Needs setup: add host, password below\./);
    assert.doesNotMatch(JSON.stringify(statuses), /Open Keys and connections/);
  }
});

test("help is per connection with meaningful hints only for credentials", async () => {
  for (const domain of ["Warehouse planning", "Home fitness"]) {
    const section = { ...testSections[1], title: domain, fields: testSections[1].fields.map(field => ({ ...field, url: "https://accounts.invalid/mail" })) };
    const app = harness("SettingsSection.tsx", { ok: true, data: { values: {}, secrets_set: {} } }, { section });
    const links = nodes(await app.load()).filter(node => node.type === "a");
    assert.equal(links.filter(node => node.props.children === "How to get this").length, 1);
    assert.equal(links.filter(node => node.props.children === "Create an app password").length, 1);
    assert.equal(links.length, 2);
    assert.doesNotMatch(JSON.stringify(links), /Where to get your|Get a key/);
  }
});

test("credential provenance and a collapsed env guide show only paths and variable names", async () => {
  for (const domain of ["Warehouse planning", "Home fitness"]) {
    for (const source of ["from .env", "set"]) {
      const section = testSections[0];
      const app = harness("SettingsSection.tsx", { ok: true, data: { values: { provider: "openai" }, secrets_set: { api_key: true }, field_states: { api_key: source } } }, { section });
      const status = nodes(await app.load()).find(node => node.props.id === "providers-api_key-status");
      assert.match(JSON.stringify(status), source === "from .env" ? /Set from your \.env file/ : /Saved in the app/);
      const imported = harness("SettingsSection.tsx", { ok: true, data: { values: { provider: "openai" }, secrets_set: { api_key: true } } }, {
        section: { ...section, fields: section.fields.map(field => ({ ...field, state: field.name === "api_key" ? source : "not set" })) },
      });
      assert.match(JSON.stringify(nodes(await imported.load()).find(node => node.props.id === "providers-api_key-status")), source === "from .env" ? /Set from your \.env file/ : /Saved in the app/);
    }
    const catalog = { env_path: `/private/${domain}/.env`, sections: testSections };
    const app = harness("LocalSettings.tsx", { ok: true, data: catalog });
    const tree = await app.load();
    const guide = nodes(tree).find(node => node.type === "details");
    assert.ok(guide);
    assert.ok(!guide.props.open);
    assert.equal(nodes(guide).find(node => node.type === "summary").props.children, "Use a .env file instead");
    assert.ok(JSON.stringify(tree).includes(catalog.env_path));
    assert.match(JSON.stringify(guide), /PASSWORD/);
    await nodes(tree).find(node => node.type === "button" && node.props.children === "Copy").props.onClick();
    assert.equal(app.calls.at(-1).copied, catalog.env_path);
    assert.match(JSON.stringify(app.render()), /Copied/);
  }
});

test("provider choices hide unrelated fields but keep the independent reader visible", async () => {
  for (const domain of ["Warehouse planning", "Home fitness"]) {
    for (const [sectionId, choices] of [["search", { builtin: [], tavily: ["api_key"], exa: ["api_key"], openai: [], firecrawl: [] }],
      ["publishing", { vercel: ["project", "token"], netlify: ["project", "token"], s3: ["bucket", "region", "prefix", "site_url", "access_key_id", "secret_access_key", "session_token"] }]]) {
      const section = testSections.find(item => item.id === sectionId);
      for (const [provider, expected] of Object.entries(choices)) {
        const values = Object.fromEntries(section.fields.filter(field => !field.secret).map(field => [field.name, field.name === "provider" ? provider : domain]));
        const app = harness("SettingsSection.tsx", { ok: true, data: { values, secrets_set: {} } }, { section });
        const tree = await app.load();
        if (sectionId === "search") assert.ok(["firecrawl_url", "firecrawl_key"].every(name =>
          nodes(tree).some(node => node.props.id === `search-${name}`)));
        for (const field of section.fields.filter(field => field.providers)) {
          assert.equal(nodes(tree).some(node => node.props.id === `${sectionId}-${field.name}`), expected.includes(field.name), `${provider}: ${field.name}`);
        }
        await nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
        const body = JSON.parse(app.calls.find(call => call.init.method === "PUT").init.body);
        for (const field of section.fields.filter(field => field.providers && !expected.includes(field.name))) assert.ok(!(field.name in body));
      }
    }
    for (const provider of ["openai", "anthropic", "azure_openai", "openrouter", "ollama", "lmstudio"]) {
      const app = harness("SettingsSection.tsx", { ok: true, data: { values: {}, secrets_set: {} } }, { section: testSections[0] });
      let tree = await app.load();
      nodes(tree).find(node => node.props.id === "providers-provider").props.onChange({ target: { value: provider } });
      tree = app.render();
      assert.equal(nodes(tree).some(node => node.props.id === "providers-base_url"), ["azure_openai", "ollama", "lmstudio"].includes(provider));
      assert.ok(nodes(tree).some(node => node.props.id === "providers-api_key"));
    }
  }
});

test("switching to built-in search leaves your saved reader visible and you can clear it", async () => {
  const section = testSections.find(item => item.id === "search");
  for (const domain of ["inventory.example", "camping.example"]) {
    let values = { provider: "firecrawl", firecrawl_url: `https://${domain}` };
    const app = harness("SettingsSection.tsx", (_path, init) => {
      if (init?.method === "PUT") values = { ...values, ...JSON.parse(init.body) };
      return { ok: true, data: { values, secrets_set: { firecrawl_key: true } } };
    }, { section });
    await app.load();
    const field = name => nodes(app.render()).find(node => node.props.id === `search-${name}`);
    const save = () => nodes(app.render()).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    field("provider").props.onChange({ target: { value: "builtin" } });
    await save();
    assert.equal(field("firecrawl_url").props.value, `https://${domain}`);
    assert.match(JSON.stringify(app.render()), /saved Firecrawl reader is on/);
    field("firecrawl_url").props.onChange({ target: { value: "" } });
    await save();
    const saved = JSON.parse(app.calls.filter(call => call.init?.method === "PUT").at(-1).init.body);
    assert.equal(saved.firecrawl_url, "");
    assert.ok(!("firecrawl_key" in saved));
    assert.match(JSON.stringify(app.render()), /built-in page reader is on/);
  }
});

test("mail presets fill SMTP and IMAP details without overwriting your identity or password", async () => {
  for (const domain of ["Warehouse planning", "Home fitness"]) {
    const section = { ...testSections[1], provider_urls: { gmail: "https://accounts.invalid/gmail", outlook: "https://accounts.invalid/outlook" } };
    const app = harness("SettingsSection.tsx", { ok: true, data: { values: { user: domain, host: "custom.invalid" }, secrets_set: { password: true } } }, { section });
    let tree = await app.load();
    for (const [preset, host, imapHost] of [["gmail", "smtp.gmail.com", "imap.gmail.com"], ["outlook", "smtp-mail.outlook.com", "outlook.office365.com"]]) {
      nodes(tree).find(node => node.props.id === "smtp-preset").props.onChange({ target: { value: preset } });
      tree = app.render();
      for (const [name, value] of [["host", host], ["port", "587"], ["imap_host", imapHost], ["imap_port", "993"], ["user", domain]]) {
        assert.equal(nodes(tree).find(node => node.props.id === `smtp-${name}`).props.value, value);
      }
      assert.match(JSON.stringify(nodes(tree).find(node => node.props.id === "smtp-password-status")), /Saved in the app/);
      assert.equal(nodes(tree).find(node => node.type === "a" && node.props.children === "How to get this").props.href, section.provider_urls[preset]);
    }
    nodes(tree).find(node => node.props.id === "smtp-preset").props.onChange({ target: { value: "other" } });
    assert.equal(nodes(app.render()).find(node => node.props.id === "smtp-host").props.value, "smtp-mail.outlook.com");
  }
});

test("readiness requires a successful test of saved settings and results appear next to the test button", async () => {
  for (const domain of ["Warehouse planning", "Home fitness"]) {
    const section = testSections[0];
    const readySection = { ...section, state: "ready", fields: section.fields.map(field => ({ ...field, state: "set" })) };
    let passed = false;
    const app = harness("SettingsSection.tsx", (path, init) => path.endsWith("/catalog") ? { ok: true, data: { env_path: `/private/${domain}/.env`, sections: [readySection] } }
      : path.endsWith("/test") ? passed ? { ok: true, data: { ok: true, verified: JSON.parse(init.body).model === domain } }
        : { ok: false, status: 502, data: { detail: { code: "bad_api_key" } } }
      : { ok: true, data: { values: { provider: "openai", model: domain, base_url: "https://api.openai.com/v1" }, secrets_set: { api_key: true }, configured: !!init.method } }, { section });
    let tree = await app.load();
    await nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    tree = app.render();
    assert.ok(app.calls.some(call => call.path.endsWith("/catalog")));
    assert.match(JSON.stringify(tree), /You can use your saved provider. To check it, choose Test your connection below\./);
    assert.equal(nodes(tree).find(node => node.type === "button" && node.props.type === "submit").props.children, "Saved");
    const testConnection = () => nodes(app.render()).find(node => node.type === "button" && node.props.children === "Test your connection").props.onClick();
    await testConnection();
    assert.match(JSON.stringify(app.render()), /You can use your saved provider\./);
    passed = true;
    await testConnection();
    assert.ok(nodes(app.render()).some(node => node.props.role === "status" && node.props.children === "Ready"));
    const actions = nodes(app.render()).find(node => nodes(node).some(child => child.type === "button" && child.props.children === "Test your connection") && node.props.className === "actions");
    assert.ok(actions);
    assert.match(JSON.stringify(actions), /connection is working/);
    nodes(app.render()).find(node => node.props.id === "providers-model").props.onChange({ target: { value: `${domain}-draft` } });
    await testConnection();
    assert.match(JSON.stringify(app.render()), /Unsaved changes/);
    assert.ok(!nodes(app.render()).some(node => node.props.role === "status" && node.props.children === "Ready"));
    let catalogReads = 0;
    const screen = harness("LocalSettings.tsx", () => ({ ok: true, data: { env_path: `/private/${domain}/.env`, sections: [++catalogReads > 1 ? readySection : section] } }));
    const initial = await screen.load();
    nodes(initial).find(node => node.props.section).props.onSaved();
    const refreshed = await screen.load();
    assert.equal(catalogReads, 2);
    assert.equal(nodes(refreshed).find(node => node.props.section).props.section.state, "ready");
    const draftProps = { section };
    const draft = harness("SettingsSection.tsx", { ok: true, data: { values: { provider: "openai", model: domain }, secrets_set: {} } }, draftProps);
    const draftTree = await draft.load();
    nodes(draftTree).find(node => node.props.id === "providers-model").props.onChange({ target: { value: domain + " draft" } });
    draftProps.section = readySection;
    assert.equal(nodes(await draft.load()).find(node => node.props.id === "providers-model").props.value, domain + " draft");
  }
});

test("settings use shell spacing and remain light across workspace widths", () => {
  const css = readFileSync(join(__dirname, "local.module.css"), "utf8");
  assert.match(css, /supymem-chat-panel/);
  assert.match(css, /--chat-panel-width/);
  assert.doesNotMatch(css, /workspace-fixed-header.*min-height:156px/);
  assert.match(css, /min-height:100%/);
  assert.match(css, /color-scheme:light/);
  for (const domain of ["Warehouse planning", "Home fitness"]) assert.ok(!css.includes(domain));
});

test("your connection fields offer real saved-setting probes across domains", async () => {
  const SECTIONS = testSections;
  for (const domain of ["warehouse-routing.invalid", "weekend-cooking.invalid"]) {
    for (const section of SECTIONS.filter(item => ["search", "publishing", "composio"].includes(item.id))) {
      const app = harness("SettingsSection.tsx", path => path.endsWith("/test") ? { ok: true, data: { ok: true, next_step: "Your connection works." } }
        : { ok: true, data: { values: { firecrawl_url: `https://${domain}` }, secrets_set: {} } }, { section });
      const tree = JSON.stringify(await app.load());
      assert.doesNotMatch(tree, /not used.*yet/i);
      assert.match(tree, /How to get this/);
      assert.ok(app.calls.every(call => !call.init.method));
      await nodes(app.render()).find(node => node.type === "button" && node.props.children === "Test your connection").props.onClick();
      assert.equal(app.calls.at(-1).path, `${section.path}/test`);
      assert.equal(app.calls.at(-1).init.method, "POST");
      assert.match(JSON.stringify(app.render()), /connection works/);
    }
  }
});

test("your monthly cap describes enforcement and provider limits across domains", async () => {
  const SECTIONS = testSections;
  for (const domain of ["Warehouse routing", "Weekend cooking"]) {
    const section = SECTIONS.find(item => item.id === "spend");
    const app = harness("SettingsSection.tsx", { ok: true, data: { values: { monthly_spend_cap: 10 }, secrets_set: {} } }, { section });
    const tree = JSON.stringify(await app.load());
    assert.match(tree, /checks this limit before/i, domain);
    assert.match(tree, /provider.*limit/i, domain);
    assert.doesNotMatch(tree, /does not enforce/i, domain);
    assert.ok(app.calls.every(call => !call.init.method));
  }
});

test("your provider failure identifies the next step without echoing private entries across domains", async () => {
  const SECTIONS = testSections;
  for (const domain of ["Industrial procurement", "Family cooking"]) {
    for (const [detail, expected] of [
      ["Your system keychain could not be opened or updated. Unlock it, then restart your app and retry.", /unlock.*restart/i],
      [{ code: "bad_api_key", message: domain + " private" }, /key that matches your service/i],
      [{ code: "provider_down", message: domain + " private" }, /service address/i],
    ]) {
      const app = harness("SettingsSection.tsx", (path, init) => init.method ? { ok: false, status: 422, data: { detail } }
        : { ok: true, data: { values: {}, secrets_set: {} } }, { section: SECTIONS[0] });
      await app.load();
      await nodes(app.render()).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
      assert.match(JSON.stringify(app.render()), expected);
      assert.ok(!JSON.stringify(app.render()).includes(domain + " private"));
    }
  }
});

test("your server-selected local settings open without a second mode request across domains", () => {
  for (const domain of ["Industrial procurement", "Family cooking"]) {
    const app = harness("page.tsx", { ok: false, status: 404 }, {}, "", true);
    assert.ok(nodes(app.render()).some(node => node.type === app.LocalSettings), domain);
    assert.equal(app.calls.length, 0);
  }
});

test("local settings stay hidden unless your server or build enables local mode", async () => {
  assert.match(readFileSync(join(__dirname, "layout.tsx"), "utf8"), /<AppShell title="Settings">\{children\}<\/AppShell>/);
  for (const response of [{ ok: true, data: { local_mode: false } }, { ok: false, status: 404 }, { ok: true, data: { local_mode: "local" } }]) {
    const app = harness("page.tsx", response);
    assert.equal(app.render(), null);
    assert.equal(await app.load(), null);
    assert.ok(app.calls.every(call => call.path === "/api/local/settings/mode"));
  }
  const server = harness("page.tsx", { ok: true, data: { local_mode: true } });
  assert.ok(nodes(await server.load()).some(node => node.type === server.LocalSettings));
  const build = harness("page.tsx", { ok: false, status: 404 }, {}, "local");
  assert.ok(nodes(await build.load()).some(node => node.type === build.LocalSettings));
});

test("each requested section uses your local API and missing endpoints offer a next step", async () => {
  const SECTIONS = testSections;
  assert.deepEqual(SECTIONS.map(section => section.id), ["providers", "smtp", "publishing", "search", "composio", "spend", "data"]);
  for (const section of SECTIONS) {
    assert.equal(section.path, section.id === "providers" ? "/api/local/providers" : `/api/local/settings/${section.id}`);
    const app = harness("SettingsSection.tsx", { ok: false, status: 404 }, { section });
    const tree = await app.load();
    assert.equal(app.calls[0].path, section.path);
    assert.match(JSON.stringify(tree), /not available/);
    assert.match(JSON.stringify(tree), /update your local app/i);
    assert.ok(!nodes(tree).some(node => node.type === "form"));
  }
});

test("your secrets stay empty and only explicit replacement keys are sent in unrelated domains", async () => {
  const SECTIONS = testSections;
  for (const domain of ["Industrial procurement", "Family cooking"]) {
    for (const section of SECTIONS.filter(item => item.fields.some(field => field.secret))) {
      const secret = section.fields.find(field => field.secret);
      const app = harness("SettingsSection.tsx", (path, init) => path.endsWith("/catalog")
        ? { ok: true, data: { env_path: `/private/${domain}/.env`, sections: [section] } }
        : { ok: true, data: { values: { provider: section.id === "publishing" ? "vercel" : section.id === "search" ? "tavily" : "openai" }, secrets_set: { [secret.name]: true }, [secret.name]: domain + " server secret" } }, { section });
      let tree = await app.load();
      assert.ok(!JSON.stringify(tree).includes(domain + " server secret"));
      let input = nodes(tree).find(node => node.props.id === `${section.id}-${secret.name}`);
      assert.equal(input.props.type, "password");
      assert.equal(input.props.value, "");
      assert.match(JSON.stringify(tree), /Saved in the app/);
      await nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
      assert.ok(!(secret.name in JSON.parse(app.calls.filter(call => call.init.method === "PUT").at(-1).init.body)));
      tree = app.render();
      input = nodes(tree).find(node => node.props.id === `${section.id}-${secret.name}`);
      input.props.onChange({ target: { value: domain + " replacement" } });
      await nodes(app.render()).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
      assert.equal(JSON.parse(app.calls.filter(call => call.init.method === "PUT").at(-1).init.body)[secret.name], domain + " replacement");
      assert.equal(nodes(app.render()).find(node => node.props.id === input.props.id).props.value, "");
      assert.equal(app.calls.filter(call => call.init.method === "PUT").length, 2);
    }
  }
});

test("your save errors never echo private payloads and keep a retry step", async () => {
  const SECTIONS = testSections;
  for (const domain of ["Industrial procurement", "Family cooking"]) {
    const app = harness("SettingsSection.tsx", (path, init) => init.method ? { ok: false, status: 422, error: domain + " private", data: { detail: domain + " private" } } : { ok: true, data: { values: {}, secrets_set: {} } }, { section: SECTIONS[1] });
    await app.load();
    nodes(app.render()).find(node => node.props.id === "smtp-password").props.onChange({ target: { value: domain + " private" } });
    await nodes(app.render()).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    const tree = app.render();
    assert.ok(!JSON.stringify(tree).includes(domain + " private"));
    assert.match(JSON.stringify(tree), /try again/i);
    assert.ok(nodes(tree).some(node => node.props.role === "status"));
    const invalid = harness("SettingsSection.tsx", (path, init) => init.method
      ? { ok: false, status: 422, data: { detail: { code: "invalid_local_settings", fields: ["password", domain + " private"] } } }
      : { ok: true, data: { values: {}, secrets_set: {} } }, { section: SECTIONS[1] });
    await invalid.load();
    await nodes(invalid.render()).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    assert.match(JSON.stringify(invalid.render()), /password.*below.*Your smtp/);
    assert.ok(!JSON.stringify(invalid.render()).includes(domain + " private"));
  }
});

test("your data folder and backup guidance are visible without starting unrequested work", async () => {
  const SECTIONS = testSections;
  const app = harness("SettingsSection.tsx", { ok: true, data: { values: {}, secrets_set: {}, data_folder: "/private/data" } }, { section: SECTIONS.at(-1) });
  const tree = await app.load();
  assert.match(JSON.stringify(tree), /\/private\/data/);
  assert.match(SECTIONS.at(-1).description, /stop your app/i);
  assert.match(SECTIONS.at(-1).description, /encryption key/i);
  assert.ok(app.calls.every(call => !call.init.method));
});

test("unexpected settings responses do not invent key statuses or expose response values", async () => {
  const SECTIONS = testSections;
  for (const domain of ["Industrial procurement", "Family cooking"]) {
    for (const data of [{ api_key: domain }, { values: [domain], secrets_set: {} }, { values: {}, secrets_set: [domain] }]) {
      const app = harness("SettingsSection.tsx", { ok: true, data }, { section: SECTIONS[0] });
      const tree = await app.load();
      assert.ok(!JSON.stringify(tree).includes(domain));
      assert.match(JSON.stringify(tree), /update your local app/i);
      assert.ok(!nodes(tree).some(node => node.type === "form"));
    }
  }
});

test("your settings include spending and capabilities without adding them to optional setup across domains", async () => {
  for (const domain of ["Warehouse planning", "Home fitness"]) {
    const catalog = { env_path: `/private/${domain}/.env`, sections: testSections };
    const app = harness("LocalSettings.tsx", { ok: true, data: catalog });
    const tree = await app.load();
    assert.equal(nodes(tree).filter(node => node.type === app.SpendSection).length, 1);
    const spending = nodes(tree).find(node => node.type === app.SpendSection);
    assert.equal(spending.props.ventureId, undefined);
    assert.equal(nodes(tree).filter(node => node.props.href === "/settings/local/capabilities").length, 1);
    const section = nodes(tree).find(node => node.props.section?.id === "spend");
    section.props.onSaved();
    const refreshed = nodes(await app.load()).filter(node => node.type === app.SpendSection);
    assert.equal(refreshed.length, 1);
    assert.notEqual(refreshed[0].key, spending.key);
    const optional = harness("LocalSettings.tsx", { ok: true, data: catalog }, { optional: true });
    const setup = await optional.load();
    assert.ok(!nodes(setup).some(node => node.type === optional.SpendSection));
    assert.ok(!nodes(setup).some(node => node.props.href === "/settings/local/capabilities"));
    assert.ok(app.calls.every(call => !call.init.method));
  }
});

test("your screen renders the server catalog and optional setup without requesting work across domains", async () => {
  for (const domain of ["Warehouse planning", "Home fitness"]) {
    const catalog = { env_path: `/private/${domain}/.env`, sections: testSections.map(section => ({ ...section, title: domain + " " + section.title })) };
    const app = harness("LocalSettings.tsx", { ok: true, data: catalog });
    const tree = await app.load();
    assert.equal(app.calls[0].path, "/api/local/settings/catalog");
    assert.match(JSON.stringify(tree), /Keys and connections/);
    assert.ok(JSON.stringify(tree).includes(catalog.env_path));
    const sections = nodes(tree).filter(node => node.props.section);
    assert.equal(sections.length, 7);
    assert.ok(sections.every(node => node.props.section.title.startsWith(domain)));
    assert.ok(app.calls.every(call => !call.init.method));
    const optional = harness("LocalSettings.tsx", { ok: true, data: catalog }, { optional: true });
    const setup = await optional.load();
    assert.match(JSON.stringify(setup), /You can add these later/);
    assert.deepEqual(nodes(setup).filter(node => node.props.section).map(node => node.props.section.id), ["search", "smtp", "publishing", "composio"]);
    const failed = harness("LocalSettings.tsx", { ok: false, data: { detail: domain + " private" } });
    const failure = JSON.stringify(await failed.load());
    assert.match(failure, /Keys and connections.*Try again/);
    assert.ok(!failure.includes(domain + " private"));
  }
});

test("your AI provider comes first, then connections, images, spend and data across audiences", async () => {
  for (const domain of ["Warehouse maintenance", "Home gardening"]) {
    const images = { ...testSections[0], id: "images", title: `Your ${domain} images` };
    const catalog = { env_path: "/private/.env", sections: [images, ...testSections.toReversed()] };
    const app = harness("LocalSettings.tsx", { ok: true, data: catalog });
    const tree = await app.load();
    assert.deepEqual(nodes(tree).filter(node => node.props.section).map(node => node.props.section.id),
      ["providers", "search", "smtp", "publishing", "composio", "images", "spend", "data"], domain);
    assert.deepEqual(nodes(tree).filter(node => node.props.section || node.type === app.SpendSection)
      .map(node => node.props.section?.id || "spending"),
    ["providers", "search", "smtp", "publishing", "composio", "images", "spend", "spending", "data"], domain);
    assert.equal(app.calls.length, 1);
  }
});

test("your authorization IDs stay structured and saved changes must precede a read-only test across domains", async () => {
  for (const domain of ["Warehouse planning", "Home fitness"]) {
    const section = testSections.find(item => item.id === "composio");
    const view = { values: { auth_configs: { gmail: domain } }, secrets_set: { api_key: true }, state: "needs_setup" };
    const app = harness("SettingsSection.tsx", (path, init) => path.endsWith("/test") ? { ok: true, data: { ok: true, next_step: "Your connection works." } }
      : { ok: true, data: init.method ? { ...view, state: "ready" } : view }, { section });
    let tree = await app.load();
    const input = nodes(tree).find(node => node.props.id === "composio-auth_configs");
    assert.equal(input.type, "textarea");
    assert.equal(JSON.parse(input.props.value).gmail, domain);
    input.props.onChange({ target: { value: JSON.stringify({ gmail: domain + " edited" }) } });
    await nodes(app.render()).find(node => node.type === "button" && node.props.children === "Test your connection").props.onClick();
    assert.equal(app.calls.length, 1);
    assert.match(JSON.stringify(app.render()), /Save your changes/);
    await nodes(app.render()).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    assert.equal(JSON.parse(app.calls.at(-1).init.body).auth_configs.gmail, domain + " edited");
    assert.match(JSON.stringify(app.render()), /Ready/);
    await nodes(app.render()).find(node => node.type === "button" && node.props.children === "Test your connection").props.onClick();
    assert.equal(app.calls.at(-1).path, "/api/local/settings/composio/test");
  }
});
