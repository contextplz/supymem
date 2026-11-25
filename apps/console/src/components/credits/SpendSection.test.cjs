const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const jsx = (type, props) => ({ type, props: props || {} });
const nodes = tree => !tree || typeof tree !== "object" ? [] : [tree, ...[tree.props?.children].flat(Infinity).flatMap(nodes)];
const text = tree => tree == null || typeof tree === "boolean" ? "" : Array.isArray(tree) ? tree.map(text).join(" ")
  : typeof tree === "object" ? text(tree.props?.children) : String(tree);

function harness(initial, props = {}, local = true) {
  let cursor = 0, response = initial, epoch = 1, deferred;
  const slots = [], effects = [], requests = [], listeners = new Map();
  const exports = {};
  const react = {
    useState(initialValue) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initialValue;
      return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }];
    },
    useEffect(callback, deps) {
      const index = cursor++;
      if (!slots[index] || !deps.every((value, position) => Object.is(value, slots[index].deps[position]))) {
        slots[index]?.cleanup?.(); slots[index] = { deps };
        effects.push(() => { slots[index].cleanup = callback(); });
      }
    },
  };
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + "/SpendSection.tsx", "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, { exports, window: {
    addEventListener: (name, listener) => listeners.set(name, listener),
    removeEventListener: name => listeners.delete(name),
  }, require(name) {
    if (name === "react") return react;
    if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
    if (name === "@/lib/client") return { verifiedSessionEpoch: () => epoch,
      api: async path => { requests.push(path); if (deferred) { const pending = deferred; deferred = null; return pending; } if (response instanceof Error) throw response; return response; } };
    if (name === "@/components/primitives/ErrorNotice") return { ErrorNotice: "error" };
    if (name === "@/components/layout/LocalModeBoundary") return { useLocalMode: () => local };
    throw new Error(name);
  } });
  return {
    requests,
    render() { cursor = 0; const tree = exports.SpendSection(props); effects.splice(0).forEach(effect => effect()); return tree; },
    async flush() { for (let index = 0; index < 8; index++) await Promise.resolve(); },
    respond(value) { response = value; },
    changeVenture(value) { props = { ventureId: value }; },
    changeSession() { epoch++; listeners.get("supymem-session-changed")?.(); },
    pause() { let finish; deferred = new Promise(resolve => { finish = resolve; }); return value => { deferred = null; finish(value); }; },
  };
}

const summary = (cap = 20) => ({ ok: true, data: { month: "2026-11", month_cost_usd_micro: 3_500_000,
  monthly_cap_usd_micro: cap === null ? null : cap * 1_000_000, by_feature_usd_micro: { research: 3_500_000 } } });

test("hosted screens do not fetch local spending", async () => {
  const app = harness(summary(), {}, false);
  assert.equal(app.render(), null);
  await app.flush();
  assert.deepEqual(app.requests, []);
});

test("your monthly spending is global in settings and detailed only for your selected venture across domains", async () => {
  for (const ventureId of ["ven_procurement", "ven_dancing"]) {
    for (const props of [{}, { ventureId }]) {
      const app = harness(summary(), props);
      assert.match(text(app.render()), /Loading your spending/);
      await app.flush();
      const tree = app.render();
      assert.deepEqual(app.requests, [props.ventureId ? `/api/ventures/${ventureId}/spend` : "/api/local/spend"]);
      assert.match(text(tree), /\$3\.50/);
      assert.match(text(tree), /\$20\.00/);
      assert.match(text(tree), props.ventureId ? /this venture/ : /across all your ventures/);
      assert.equal(text(tree).includes("research"), Boolean(props.ventureId));
      assert.ok(nodes(tree).some(node => node.props.href === "/settings/local"));
      assert.doesNotMatch(text(tree), /credits|tokens/i);
    }
  }
});

test("your spending keeps zero caps distinct from no limit across domains", async () => {
  for (const ventureId of ["ven_procurement", "ven_dancing"]) {
    for (const cap of [0, null]) {
      const app = harness(summary(cap), { ventureId });
      app.render(); await app.flush();
      assert.match(text(app.render()), cap === null ? /No monthly limit/ : /\$0\.00/);
    }
  }
});

test("spending failures give you a private actionable retry instead of invented totals across domains", async () => {
  for (const ventureId of ["ven_procurement", "ven_dancing"]) {
    for (const failure of [{ ok: false, data: { detail: "private provider key" } }, new Error("private provider key"),
      { ok: true, data: { ...summary().data, month_cost_usd_micro: -1 } }]) {
      const app = harness(failure, { ventureId });
      app.render(); await app.flush();
      let tree = app.render();
      const notice = nodes(tree).find(node => node.type === "error");
      assert.match(notice.props.error, /Keys and connections/);
      assert.match(notice.props.error, /Refresh spending/);
      assert.doesNotMatch(JSON.stringify(tree), /private provider key|\$0\.00/);
      app.respond(summary());
      nodes(tree).find(node => node.type === "button").props.onClick();
      app.render(); await app.flush(); tree = app.render();
      assert.match(text(tree), /\$3\.50/);
    }
  }
});

test("late spending results cannot restore totals from another venture or session across domains", async () => {
  for (const ventureId of ["ven_procurement", "ven_dancing"]) {
    for (const change of ["venture", "session"]) {
      const app = harness(summary(), { ventureId });
      const finish = app.pause();
      app.render();
      app.respond({ ok: false });
      if (change === "venture") app.changeVenture(ventureId + "_other");
      else app.changeSession();
      app.render();
      finish(summary()); await app.flush();
      assert.doesNotMatch(text(app.render()), /\$3\.50/);
    }
  }
});
