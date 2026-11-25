const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const { SETTINGS, capabilityView } = require("./capabilities.ts");

const jsx = (type, props) => ({ type, props: props || {} });
const nodes = tree => !tree || typeof tree !== "object" ? [] : [tree, ...[tree.props?.children].flat(Infinity).flatMap(nodes)];
const settle = async () => { for (let index = 0; index < 8; index++) await Promise.resolve(); };

test("you can see scheduled work and its caps before opting in across audiences", async () => {
  for (const audience of ["Business procurement", "Garden planning"]) {
    const initial = items().map(item => item.name === "background_work" ? { ...item, state: "off",
      background_jobs: [{ kind: "suggestions.generate", description: `Suggest next steps for your ${audience}.`,
        schedule: "Every hour", secret: "not-rendered" }],
      next_step: "In Settings > Capabilities, switch on Background work. Set your monthly spending limit in Settings > Keys and connections > Your monthly spend cap." } : item);
    const tree = await harness(() => ({ ok: true, data: { capabilities: initial } })).load();
    const rendered = JSON.stringify(tree);
    assert.ok(rendered.includes(`Suggest next steps for your ${audience}.`));
    assert.ok(rendered.includes("Every hour"));
    assert.ok(rendered.includes("Settings > Keys and connections > Your monthly spend cap"));
    assert.ok(!rendered.includes("suggestions.generate"));
    assert.ok(!rendered.includes("not-rendered"));
    assert.equal(nodes(tree).find(node => node.props["aria-label"] === "Enable background work").props.checked, false);
  }
  assert.equal(capabilityView({ ...items()[0], background_jobs: [{ description: false, schedule: "Every hour" }] }), null);
});

test('Capabilities has matching On and Off labels without a spurious retry across domains', async () => {
  for (const title of ['Business procurement', 'Garden planning']) for (const [state, missing, status] of [
    ['off', [], 'Off'], ['off', ['Search API key'], 'Off'], ['ready', [], 'Ready'], ['needs_setup', ['Search API key'], 'Needs setup'],
  ]) {
    const initial = items().map(item => item.name === 'research' ? {...item, title, state, missing} : item);
    const tree = await harness(() => ({ok: true, data: {capabilities: initial}})).load();
    const heading = nodes(tree).find(node => node.type === 'h1');
    assert.equal(heading.props.children, 'Capabilities');
    assert.equal(tree.props['aria-labelledby'], heading.props.id);
    assert.equal(tree.props.tabIndex, -1);
    assert.equal(nodes(tree).filter(node => node.props.role === 'switch').length, initial.length);
    const label = nodes(tree).find(node => node.type === 'label' && nodes(node).some(child => child.props['aria-label'] === `Enable ${title}`));
    assert.equal(label.props.children.at(-1), state === 'off' ? 'Off' : 'On');
    assert.equal(nodes(label).find(node => node.props.role === 'switch').props.checked, state !== 'off');
    const nextStepId = nodes(label).find(node => node.props.role === 'switch').props['aria-describedby'];
    assert.ok(nodes(tree).some(node => node.props.id === nextStepId && node.props.children === initial.find(item => item.name === 'research').next_step));
    const section = nodes(tree).find(node => node.props['aria-labelledby'] === 'research-title');
    assert.ok(nodes(section).some(node => node.props['aria-label'] === `Status: ${status}`));
    assert.equal(JSON.stringify(section).includes('Search API key'), Boolean(missing.length));
    assert.equal(nodes(tree).some(node => node.type === 'button' && node.props.children === 'Try again'), false);
  }
  const css = readFileSync(join(__dirname, '../local.module.css'), 'utf8');
  assert.match(css, /\.toggle\{appearance:none/);
  assert.match(css, /\.toggle:checked::before\{transform:translateX\(16px\)/);
});
const items = () => Object.keys(SETTINGS).map(name => ({ name, title: name.replaceAll("_", " "),
  state: name === "send_email" ? "off" : "ready", missing: [], next_step: "Choose your work." }));

test("your capabilities screen keeps shipped switches and omits features without a laptop version across audiences", async () => {
  for (const audience of ["Factory scheduling", "Weekend gardening"]) {
    const unavailable = { name: "voice", title: `${audience} voice`, state: "not_in_this_version", missing: [],
      next_step: "Voice is not in this version yet; type your request in chat instead.", surfaces: { settings: ["voice"] } };
    assert.ok(capabilityView(unavailable));
    const tree = await harness(() => ({ ok: true, data: { capabilities: [...items(), unavailable] } })).load();
    assert.equal(nodes(tree).filter(node => node.props.role === "switch").length, items().length);
    assert.ok(!JSON.stringify(tree).includes(unavailable.title));
  }
});

function harness(response, local = true) {
  const exports = {}, states = [], effects = [], calls = [];
  let hook = 0;
  vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, "CapabilitiesView.tsx"), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText, { exports, Set, require(name) {
    if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
    if (name === "react") return {
      useState(initial) { const index = hook++; if (!(index in states)) states[index] = initial;
        return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }]; },
      useRef(initial) { const index = hook++; if (!(index in states)) states[index] = { current: initial }; return states[index]; },
      useEffect(effect) { const index = hook++; if (!(index in states)) { states[index] = true; effects.push(effect); } },
    };
    if (name === "@/lib/client") return { api: async (path, init = {}) => {
      calls.push({ path, init }); return response(path, init);
    } };
    if (name.endsWith("LocalModeBoundary")) return { useLocalMode: () => local };
    if (name.endsWith("ErrorNotice")) return { ErrorNotice: "error" };
    if (name === "./capabilities") return require("./capabilities.ts");
    if (name.endsWith("local.module.css")) return { default: {} };
    throw new Error(name);
  } });
  const render = () => { hook = 0; return exports.Capabilities(); };
  return { render, calls, page: exports.LocalCapabilities, async load() {
    render(); effects.splice(0).forEach(effect => effect()); await settle(); return render();
  } };
}

test("you can see readiness, switch a feature on and reach its setup for unrelated audiences", async () => {
  for (const audience of ["Factory scheduling", "Weekend gardening"]) {
    const initial = items();
    const app = harness((path, init) => init.method ? { ok: true, data: { ...initial.find(item => item.name === "send_email"),
      state: "needs_setup", missing: ["App password"], next_step: "Save your App password in Keys and connections > Your outgoing mail." } }
      : { ok: true, data: { capabilities: initial } });
    let tree = await app.load();
    assert.equal(nodes(tree).filter(node => node.type === "section").length, Object.keys(SETTINGS).length, audience);
    const control = nodes(tree).find(node => node.props["aria-label"] === "Enable send email");
    assert.equal(control.props.checked, false);
    assert.equal(control.props.role, "switch");
    assert.equal(nodes(tree).find(node => node.type === "label" && nodes(node).includes(control)).props.children.at(-1), "Off");
    assert.ok(!nodes(tree).some(node => node.type === "button" && node.props.children === "Try again"));
    control.props.onChange({ target: { checked: true } });
    await settle();
    tree = app.render();
    assert.equal(nodes(tree).find(node => node.props["aria-label"] === "Enable send email").props.checked, true);
    assert.ok(nodes(tree).some(node => node.type === "label" && node.props.children.at(-1) === "On"));
    assert.ok(nodes(tree).some(node => node.props["aria-label"] === "Status: Needs setup"));
    assert.match(JSON.stringify(tree), /App password/);
    assert.ok(nodes(tree).some(node => node.type === "a" && node.props.href === "/settings/local#smtp-title"));
    assert.equal(app.calls[1].path, "/api/local/capabilities/send_email");
    assert.deepEqual(JSON.parse(app.calls[1].init.body), { on: true });
    assert.ok(app.calls.every(call => !call.path.includes(audience)));
  }
});

test("a failed save reports your recovery step and keeps your switch unchanged across audiences", async () => {
  for (const audience of ["Factory scheduling", "Weekend gardening"]) {
    const app = harness((path, init) => init.method ? { ok: false, data: { secret: "not-rendered" } }
      : { ok: true, data: { capabilities: items() } });
    const tree = await app.load();
    nodes(tree).find(node => node.props["aria-label"] === "Enable send email").props.onChange({ target: { checked: true } });
    await settle();
    const failed = app.render();
    assert.equal(nodes(failed).find(node => node.props["aria-label"] === "Enable send email").props.checked, false, audience);
    const notice = nodes(failed).find(node => node.type === "error");
    assert.equal(notice.props.source, "settings.local.capabilities");
    assert.match(notice.props.error, /Your data folder and backup/);
    assert.ok(!JSON.stringify(failed).includes("not-rendered"));
  }
});

test("malformed results do not expose values or offer switches, and you can retry", async () => {
  for (const response of [{ ok: false }, { ok: true, data: { capabilities: [{ secret: "not-rendered" }] } },
    { ok: true, data: { capabilities: [items()[0]] } }]) {
    const app = harness(() => response);
    const tree = await app.load();
    assert.ok(!nodes(tree).some(node => node.props.role === "switch"));
    assert.ok(nodes(tree).some(node => node.type === "button" && node.props.children === "Try again"));
    assert.ok(!JSON.stringify(tree).includes("not-rendered"));
    assert.match(JSON.stringify(tree), /Restart your local app/);
  }
  assert.equal(capabilityView({ ...items()[0], name: "unknown" }), null);
  assert.equal(capabilityView({ ...items()[0], missing: [false] }), null);
});

test("the capability screen remains local only", () => {
  const app = harness(() => { throw new Error("Hosted page must not fetch local settings"); }, false);
  assert.equal(app.page(), null);
  assert.equal(app.calls.length, 0);
});

test("you can switch off your coding tool without changing other features across audiences", async () => {
  for (const audience of ["Warehouse inspection", "Music practice"]) {
    const initial = items();
    const build = initial.find(item => item.name === "build_with_my_tool");
    assert.ok(build, audience);
    const app = harness((path, init) => init.method ? { ok: true, data: { ...build, state: "off" } }
      : { ok: true, data: { capabilities: initial } });
    const tree = await app.load();
    const toggle = nodes(tree).find(node => node.props["aria-label"] === "Enable build with my tool");
    assert.equal(toggle.props.checked, true, audience);
    toggle.props.onChange({ target: { checked: false } });
    await settle();
    assert.equal(app.calls[1].path, "/api/local/capabilities/build_with_my_tool");
    assert.deepEqual(JSON.parse(app.calls[1].init.body), { on: false });
    assert.equal(nodes(app.render()).find(node => node.props["aria-label"] === "Enable build with my tool").props.checked, false);
    assert.equal(nodes(app.render()).find(node => node.props["aria-label"] === "Enable research").props.checked, true);
  }
});

test("an older local app tells you to update and return to the exact screen", async () => {
  const app = harness(() => ({ ok: false, status: 404, data: { secret: "not-rendered" } }));
  const tree = await app.load();
  assert.match(JSON.stringify(tree), /Update your local app/);
  assert.match(JSON.stringify(tree), /Settings > Capabilities/);
  assert.ok(nodes(tree).some(node => node.type === "button" && node.props.children === "Try again"));
  assert.ok(!JSON.stringify(tree).includes("not-rendered"));
});
