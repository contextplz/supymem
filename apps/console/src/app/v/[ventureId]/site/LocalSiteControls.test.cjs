const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const jsx = (type, props) => ({ type, props: props || {} });
const nodes = tree => !tree || typeof tree !== "object" ? [] : [tree, ...[tree.props?.children].flat(Infinity).flatMap(nodes)];
const text = node => typeof node === "string" ? node : [node?.props?.children].flat(Infinity).map(child => typeof child === "object" ? text(child) : child || "").join("");
const settle = () => new Promise(resolve => setImmediate(resolve));
const readyPage = { current: { state: "ready", job_id: "your-job", native_ready: true, candidates: [{ variant: 0 }] } };

function harness(response, props) {
  const states = [], refs = [], effects = [], calls = [], publications = [];
  let hook = 0;
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, "LocalSiteControls.tsx"), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, {
    exports, AbortController, URL, document: { createElement: () => ({ click() {} }) },
    require(name) {
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name.endsWith("ErrorNotice")) return { ErrorNotice: "error" };
      if (name === "react") return {
        useState(initial) { const index = hook++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }]; },
        useRef(initial) { const index = hook++; if (!(index in refs)) refs[index] = { current: initial }; return refs[index]; },
        useEffect(effect, deps) { const index = hook++; const previous = states[index]; if (!previous || deps.some((value, position) => value !== previous.deps[position])) { previous?.cleanup?.(); effects.push(() => { states[index] = { deps, cleanup: effect() }; }); } },
      };
      if (name === "@/lib/client") return { api: async (path, init = {}) => { calls.push({ path, init }); return response(path, init); }, authenticatedFetch: async () => { throw new Error("not needed"); } };
      throw new Error(name);
    },
  });
  const render = next => { if (next) props = next; hook = 0; return exports.LocalSiteControls({ ...props, onPublication: value => publications.push(value) }); };
  return { render, calls, publications, async load(next) { render(next); effects.splice(0).forEach(effect => effect()); await settle(); return render(); } };
}

for (const ventureId of ["inventory", "gardening"]) {
  test(`${ventureId}: publishing reads can be retried and an empty page cannot be published or exported`, async () => {
    let failedRead = "profile";
    const profile = ventureId === "inventory" ? { current: null }
      : { current: { ...readyPage.current, native_ready: false, candidates: [] } };
    const app = harness(path => path.endsWith(failedRead) ? { ok: false }
      : { ok: true, data: path.endsWith("profile") ? profile : { ready: true } }, { ventureId });
    let tree = await app.load();
    const button = (label) => nodes(tree).find(node => node.type === "button" && text(node) === label);
    assert.match(text(tree), /saved page.*Try again/i);
    for (const label of ["Publish", "Export HTML", "Export ZIP"]) {
      assert.equal(button(label).props.disabled, true);
      button(label).props.onClick();
    }
    assert.equal(app.calls.length, 2);
    failedRead = "/local";
    button("Try again").props.onClick(); tree = await app.load();
    assert.equal(app.calls.length, 4);
    assert.ok(button("Try again"));
    failedRead = "none";
    button("Try again").props.onClick(); tree = await app.load();
    assert.equal(app.calls.length, 6);
    assert.equal(button("Try again"), undefined);
    assert.match(text(tree), /Build with your coding tool/);
    assert.equal(button("Publish").props.disabled, true);
    tree = app.render({ ventureId, savedPage: true });
    assert.equal(button("Publish").props.disabled, false);
    assert.equal(button("Export ZIP").props.disabled, false);
    tree = app.render({ ventureId, appId: "verified-app", buildId: "verified-build" });
    assert.equal(button("Publish").props.disabled, false);
    assert.equal(button("Export ZIP").props.disabled, true);
  });
  test(`${ventureId}: publishing keeps your selected page variant`, async () => {
    const app = harness((path, init) => ({ ok: true, data: init.method === "POST" ? { state: "live", url: "https://your-site.test/" }
      : path.endsWith("profile") ? readyPage : { ready: true } }),
      { ventureId, selection: { job_id: "selected-job", variant: 2 } });
    const tree = await app.load();
    nodes(tree).find(node => node.type === "button" && text(node) === "Publish").props.onClick();
    await settle();
    assert.deepEqual(JSON.parse(app.calls.find(call => call.init.method === "POST").init.body), { job_id: "selected-job", variant: 2 });
  });
  test(`${ventureId}: Publish uses your saved account without a confirmation`, async () => {
    const app = harness((path, init) => ({ ok: true, data: init.method === "POST" ? { state: "live", url: "https://your-site.test/" } : path.endsWith("profile") ? readyPage : { ready: true, next_step: "", publication: null } }), { ventureId });
    const tree = await app.load();
    const publish = nodes(tree).find(node => node.type === "button" && text(node) === "Publish");
    publish.props.onClick(); publish.props.onClick();
    await settle();
    const sent = app.calls.find(call => call.init.method === "POST");
    assert.equal(sent.path, `/api/ventures/${ventureId}/site/local/publish`);
    assert.deepEqual(JSON.parse(sent.init.body), { job_id: "your-job" });
    assert.equal(app.calls.filter(call => call.init.method === "POST").length, 1);
    assert.equal(app.publications[0].state, "live");
    assert.ok(!JSON.stringify(tree).includes("confirm"));
  });

  test(`${ventureId}: missing account leaves exports and exact setup step available`, async () => {
    const app = harness(path => ({ ok: true, data: path.endsWith("profile") ? readyPage
      : { ready: false, next_step: "Open Settings > Keys and connections > Publishing and save your token." } }), { ventureId });
    const tree = await app.load();
    const buttons = nodes(tree).filter(node => node.type === "button");
    assert.equal(buttons.find(node => text(node) === "Publish").props.disabled, true);
    assert.equal(buttons.find(node => text(node) === "Export ZIP").props.disabled, false);
    assert.equal(buttons.find(node => text(node) === "Export HTML").props.disabled, false);
    assert.ok(text(tree).includes("Keys and connections > Publishing"));
  });

  test(`${ventureId}: a failed publish reports the setup screen without exposing response data`, async () => {
    const app = harness((path, init) => init.method === "POST" ? { ok: false, data: { secret: "not-rendered" } }
      : { ok: true, data: path.endsWith("profile") ? readyPage : { ready: true } }, { ventureId });
    const tree = await app.load();
    nodes(tree).find(node => node.type === "button" && text(node) === "Publish").props.onClick();
    await settle();
    const failed = app.render(), notice = nodes(failed).find(node => node.type === "error");
    assert.equal(notice.props.source, "site.local.publication");
    assert.match(notice.props.error, /Settings > Keys and connections > Publishing/);
    assert.ok(!JSON.stringify(failed).includes("not-rendered"));
    assert.deepEqual(app.publications, []);
  });

  test(`${ventureId}: a pending replacement keeps the live page manageable when the replacement is rejected`, async () => {
    const publication = { state: "live", url: `https://${ventureId}.test/` };
    let pending = true;
    const app = harness((path, init) => {
      if (init.method === "POST" && path.endsWith("verify")) {
        pending = false;
        return { ok: true, data: { state: "failed", notice: "Check your hosting account, then publish again on Website." } };
      }
      return { ok: true, data: path.endsWith("profile") ? readyPage : { ready: true, publication, verification_pending: pending } };
    }, { ventureId });
    const tree = await app.load();
    const button = (name, view = tree) => nodes(view).find(node => node.type === "button" && text(node) === name);
    assert.equal(button("Publish").props.disabled, true);
    assert.equal(button("Take offline").props.disabled, false);
    button("Check public URL").props.onClick();
    await settle();
    const checked = app.render();
    assert.equal(button("Publish", checked).props.disabled, false);
    assert.equal(button("Take offline", checked).props.disabled, false);
    assert.deepEqual(app.publications.at(-1), publication);
    assert.match(text(checked), /check your hosting account/i);
  });

  test(`${ventureId}: consented imports and offline controls use local routes`, async () => {
    const url = ventureId === "inventory" ? "https://warehouse-maps.example/" : "https://planting-notes.test/";
    let partial = false;
    const app = harness((path, init) => {
      if (path.endsWith("profile")) return { ok: true, data: readyPage };
      if (path.endsWith("signups")) return { ok: true, data: { note: "Your signups are saved locally." } };
      if (init.method !== "POST") return { ok: true, data: { ready: true, can_take_offline: true, publication: { state: partial ? "takedown_pending" : "live", url } } };
      if (partial) return { ok: true, data: { state: "offline", url } };
      partial = true;
      return { ok: false, data: { detail: "Check your hosting account, then try again from Website." } };
    }, { ventureId });
    let tree = await app.load();
    nodes(tree).find(node => node.type === "textarea").props.onChange({ target: { value: "you@your-site.test, other@your-site.test" } });
    nodes(tree).find(node => node.type === "input").props.onChange({ target: { checked: true } });
    tree = app.render();
    nodes(tree).find(node => node.type === "button" && text(node) === "Import signups").props.onClick();
    await settle();
    const imported = app.calls.find(call => call.path.endsWith("signups"));
    assert.deepEqual(JSON.parse(imported.init.body), { emails: ["you@your-site.test", "other@your-site.test"], consent: true });
    tree = app.render();
    nodes(tree).find(node => node.type === "button" && text(node) === "Take offline").props.onClick();
    await settle();
    assert.ok(app.calls.some(call => call.path === `/api/ventures/${ventureId}/site/local/unpublish`));
    tree = app.render();
    assert.match(nodes(tree).find(node => node.type === "error").props.error, /check your hosting account/i);
    assert.doesNotMatch(text(tree), /confirmed offline/i);
    nodes(tree).find(node => node.type === "button" && text(node) === "Take offline").props.onClick();
    await settle();
    assert.equal(app.publications.at(-1).state, "offline");
    assert.match(text(app.render()), /confirmed offline/i);
  });
}

test("changing your venture discards an earlier publication response", async () => {
  let finish;
  const app = harness((path, init) => init.method === "POST" ? new Promise(resolve => { finish = resolve; }) : { ok: true, data: path.endsWith("profile") ? readyPage : { ready: true } }, { ventureId: "inventory" });
  const tree = await app.load();
  nodes(tree).find(node => node.type === "button" && text(node) === "Publish").props.onClick();
  await settle();
  await app.load({ ventureId: "gardening" });
  finish({ ok: true, data: { state: "live", url: "https://earlier.test/" } });
  await settle();
  assert.equal(app.publications.length, 0);
});
