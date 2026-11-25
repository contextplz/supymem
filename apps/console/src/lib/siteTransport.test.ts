import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { createGodModeRequestFence } from "./godModeRequestScope.ts";
import { scopedSiteMatches, ventureSitePath } from "./firstlookPreview.ts";

// Execute the actual production load callback, not a second copy of its logic.
const source = fs.readFileSync(new URL("../app/v/[ventureId]/site/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let callback = "";
function visit(node: ts.Node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(tree) === "load" && node.initializer
      && ts.isCallExpression(node.initializer)) callback = node.initializer.arguments[0].getText(tree);
  ts.forEachChild(node, visit);
}
visit(tree);
assert.ok(callback.startsWith("async"), "production Website load callback must be found");
const compiled = ts.transpile(`(${callback})`, { target: ts.ScriptTarget.ES2022 });

function fixture(dashboard: unknown = { tiles: {}, onboarding: {} }) {
  const updates = new Map<string, unknown>();
  const requests: { path: string; signal?: AbortSignal }[] = [];
  const liveVenture = { current: "ven_a" };
  let scope = { venture: "ven_a", owner: "account_a", epoch: 1 };
  let release: ((value: unknown) => void) | undefined;
  const response = new Promise((resolve) => { release = resolve; });
  const context: Record<string, unknown> = {
    ventureId: "ven_a", liveVenture, AbortController, URL,
    loadFence: { current: createGodModeRequestFence() }, pendingLoad: { current: null },
    renderOwner: scope, ownerRef: {current: scope}, renderCurrent: () => scope.owner === "account_a" && scope.epoch === 1 && scope.venture === "ven_a",
    quoteSequence: {current: 0}, loadedScope: {current: null}, acceptedCustody: {current: null}, revisionCustody: {current: null}, quoteCustody: {current: null},
    publishApproval: {current: null}, revisionApproval: {current: null},
    liveScope: () => scope, ensureSession: async () => undefined,
    ventureSitePath, scopedSiteMatches, loadRevision: () => undefined,
    displayName: (name: string) => name, errorMessage: () => "Synthetic read error",
    buildAwaitingReview: async () => null, acceptedBuildPreview: async () => null,
    liveLandingDeployment: async () => null,
    auditPublishedWebsite: async () => null, publishedPageAuditTarget: () => ({ url: "", binding: null }),
    api: async (path: string, init: { signal?: AbortSignal } = {}) => {
      requests.push({ path, signal: init.signal });
      if (path === "/api/ventures/ven_a") return { ok: true, data: { name: "A", workspace_id: "wsp_shared" } };
      if (path.endsWith("/dashboard")) return { ok: true, data: dashboard };
      if (path.includes("/site?")) return response; // deliberately ignore cancellation to test late receipts
      return { ok: true, data: {} };
    },
  };
  for (const match of source.matchAll(/\b(set[A-Z]\w+)\(/g)) {
    const name = match[1]; context[name] = (value: unknown) => updates.set(name, value);
  }
  const load = vm.runInNewContext(compiled, context) as () => Promise<void>;
  return { load, updates, requests, context, liveVenture,
    change: (next: typeof scope) => { scope = next; },
    release: (value: unknown) => release!(value) };
}

for (const stage of ["Testing", "Deploying"]) {
  test(`a missing legacy site during ${stage} is pending, not an empty Website`, async () => {
    const f = fixture({run_status: {stage, state: "working"}});
    const {run} = await waiting(f);
    f.release({ok: false, status: 404, data: {detail: {error: {code: "no_site"}}}});
    await run;
    assert.equal(f.updates.get("setSitePending"), true);
    assert.equal(f.updates.get("setBusy"), false);
  });
}

test("live landing publication is authoritative even without a legacy site row", async () => {
  const f = fixture({run_status: {state: "ready", stage: "Landing page", landing_page: {available: true, live_url: "https://oximoron.supymem.com/"}}});
  const {run} = await waiting(f);
  f.release({ok: false, status: 404, data: {}}); await run;
  assert.equal(f.updates.get("setLivePageUrl"), "https://oximoron.supymem.com/");
  assert.equal(f.updates.get("setSitePending"), false);
});

async function waiting(f: ReturnType<typeof fixture>) {
  const run = f.load();
  for (let i = 0; i < 8 && !f.requests.some((r) => r.path.includes("/site?")); i++) {
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
  assert.equal(f.requests.find((r) => r.path.includes("/site?"))?.path,
    "/api/workspaces/wsp_shared/site?venture_id=ven_a");
  assert.ok(f.requests.every((r) => r.signal instanceof AbortSignal));
  return { run };
}

test("actual Website transport refuses another venture and historical unbound metadata", async () => {
  for (const venture_id of ["ven_b", null]) {
    const f = fixture(); const { run } = await waiting(f);
    f.release({ ok: true, data: { workspace_id: "wsp_shared", venture_id, id: "site_wrong" } });
    await run;
    assert.equal(f.updates.get("setSite"), null);
    assert.equal(f.updates.get("setSiteMissing"), true);
  }
});

test("actual Website transport accepts its bound draft only in the live account epoch", async () => {
  const f = fixture(); const { run } = await waiting(f);
  const site = { workspace_id: "wsp_shared", venture_id: "ven_a", id: "site_a" };
  f.release({ ok: true, data: site }); await run;
  assert.equal(f.updates.get("setSite"), site);
});

test("actual Website late response cannot cross navigation, account, epoch or abort", async () => {
  for (const change of ["venture", "owner", "epoch", "abort"]) {
    const f = fixture(); const { run } = await waiting(f);
    if (change === "venture") { f.liveVenture.current = "ven_b"; f.change({ venture: "ven_b", owner: "account_a", epoch: 1 }); }
    if (change === "owner") f.change({ venture: "ven_a", owner: "account_b", epoch: 1 });
    if (change === "epoch") f.change({ venture: "ven_a", owner: "account_a", epoch: 2 });
    if (change === "abort") (f.context.pendingLoad as { current: AbortController }).current.abort();
    f.release({ ok: true, data: { workspace_id: "wsp_shared", venture_id: "ven_a", id: "site_a" } });
    await run;
    assert.equal(f.updates.get("setSite"), null);
    assert.equal(f.updates.has("setBuilderFailed"), false);
    assert.equal(f.updates.has("setPausedReason"), false);
  }
});

test("actual Website load sends its independent reads together, not one after another", async () => {
  // Before: venture → import → dashboard → publication → site record → builder choice, each awaited
  // in turn, so the first card waited for the sum of six round trips. Now the first card waits for
  // the slowest of them plus one hop (the site record needs the venture's workspace).
  const f = fixture(); const { run } = await waiting(f);
  const paths = f.requests.map((r) => r.path);
  for (const suffix of ["/ventures/ven_a", "/assets/site", "/dashboard", "/site/publication", "/builder/choice"])
    assert.ok(paths.some((path) => path.endsWith(suffix)), `${suffix} is in flight while the site record is pending`);
  assert.equal(f.updates.get("setBusy"), true, "nothing renders until the reads it needs settle");
  f.release({ ok: true, data: { workspace_id: "wsp_shared", venture_id: "ven_a", id: "site_a" } });
  await run;
  assert.equal(f.updates.get("setBusy"), false);
});
