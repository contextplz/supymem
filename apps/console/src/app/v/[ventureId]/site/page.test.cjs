const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function actual(name, context) {
  const tree = ts.createSourceFile("page.tsx", readFileSync(join(__dirname, "page.tsx"), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let source;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) source = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(source, `production ${name} exists`);
  return vm.runInNewContext(ts.transpile(`${source}; ${name}`, { target: ts.ScriptTarget.ES2022 }), context);
}

function fixture(domain, { connected = true, provider = "codex", state = "approved", mode = "connected_tool", failure, sourceFailure, quoteFailure, retireAt, saved = false } = {}) {
  const owner = { venture: domain, owner: "you", epoch: 1, local: true };
  const preview = { appId: "app", buildId: "build", artifactId: "artifact" };
  const baseline = { deployment_id: "deployment", source_sha256: "a".repeat(64) };
  const receipt = { id: `fr_${"b".repeat(32)}`, state: "pending", ...baseline, instruction: `Make the ${domain} page more accessible.`, build_id: preview.buildId, source_artifact_id: preview.artifactId };
  const revision = { baseline, requests: saved ? [receipt] : [] };
  const calls = [], updates = {}, navigations = [];
  let live = true;
  const context = {
    local: true,
    renderOwner: owner, ventureId: domain, acceptedPreview: preview, revision, revisionInstruction: receipt.instruction,
    revisionQuote: { ready: true, ceiling_micro: 100, expires_at: new Date(Date.now() + 60000).toISOString() }, revisionApproved: true,
    acceptedCustody: { current: { scope: owner, value: preview } }, revisionCustody: { current: { scope: owner, value: revision } },
    quoteCustody: { current: null }, revisionApproval: { current: null }, quoteSequence: { current: 0 },
    revisionFlight: { current: null }, pendingLoad: { current: new AbortController() },
    window: { location: { assign: path => navigations.push(path) } },
    renderCurrent: () => live, liveScope: () => owner, sameGodModeScope: () => live,
    owned: (value, custody) => live && custody?.value === value,
    revisionRequestBody: (value, instruction) => ({ ...value, instruction: instruction.trim() }),
    revisionSaveFailure: () => "Refresh Website and retry your revision.", errorCode: value => value?.detail?.code || "failure",
    errorMessage: (value, fallback) => typeof value?.detail === "string" ? value.detail : value?.detail?.message || fallback,
    api: async (path, init = {}) => {
      calls.push({ path, init });
      if (retireAt && path.endsWith(retireAt)) live = false;
      if (path === "/api/local-tools/coding-agents") return { ok: true, data: { tools: [{ name: provider, connected, coding_available: connected }] } };
      if (path.endsWith("/site/revisions")) return init.method === "POST" ? sourceFailure || { ok: true, data: receipt } : { ok: true, data: revision };
      if (path.endsWith("/site/revisions/estimate")) return quoteFailure || { ok: true, data: { quote_id: "bq_revision", input_digest: "d".repeat(64), ready: connected, mode,
        reasons: connected ? [] : ["Open Settings > Keys and connections > Coding tools, connect your tool, then retry."], ceiling_micro: 0 } };
      if (path.endsWith("/builds")) return failure || { ok: true, data: { device_work_order_id: "dwo_revision", build_run_id: "build_revision", state, review_path: `/v/${domain}/device-work-orders/dwo_revision` } };
      return { ok: true, data: {} };
    },
    estimateRevision: async () => calls.push({ path: "internal-estimate" }),
  };
  for (const name of ["setRevisionBusy", "setRevisionError", "setRevisionRun", "setRevisionInstruction", "setRevision", "setRevisionQuote", "setRevisionApproved"]) context[name] = value => { updates[name] = value; };
  try { context.buildRevisionWithTool = actual("buildRevisionWithTool", context); } catch { /* The unchanged route must fail on its old dispatch, not helper discovery. */ }
  context.startRevisionBuild = actual("startRevisionBuild", context);
  return { calls, updates, navigations, context, receipt, async run(entry) { return actual(entry, context)(entry === "estimateRevision" && saved ? receipt.id : undefined); } };
}

for (const venture of ["maple-ledger", "winter-picnic"]) {
  test(`${venture}: legacy local publication opens as an authenticated preview, not a visit link`, () => {
    const source = readFileSync(join(__dirname, "page.tsx"), "utf8");
    const { firstlookPreviewPath } = require("../../../../lib/firstlookPreview.ts");
    const url = `http://127.0.0.1:3300/w/${venture}/site/?v=4`;
    assert.match(source, /const legacyPreviewUrl = local && !deployedUrl && firstlookPreviewPath\(url, ventureId\)/);
    assert.match(source, /const displayedPublished = surface\.displayPublished && !legacyPreviewUrl/);
    assert.match(source, /<LandingPreviewFrame src=\{legacyPreviewUrl\} ventureId=\{ventureId\} sandbox=""/);
    assert.equal(firstlookPreviewPath(url, venture), `/api/workspaces/${venture}/site/preview?v=4&venture_id=${venture}`);
  });
}

for (const domain of ["warehouse-inventory", "family-hiking"]) {
  for (const entry of ["saveRevisionRequest", "estimateRevision", "startRevisionBuild"]) {
    test(`${domain}: ${entry} starts your connected tool with a source-bound brief`, async () => {
      const app = fixture(domain, { saved: entry !== "saveRevisionRequest" });
      await app.run(entry);
      const order = app.calls.find(call => call.path.endsWith("/builds"));
      assert.ok(order, "a device work order is requested instead of an internal build");
      const body = JSON.parse(order.init.body);
      assert.equal(body.quote_id, "bq_revision");
      assert.equal(body.input_digest, "d".repeat(64));
      const brief = app.calls.find(call => call.path.endsWith("/site/revisions/estimate"));
      assert.equal(JSON.parse(brief.init.body).revision_request_id, app.receipt.id);
      assert.equal(app.updates.setRevisionRun, "dwo_revision");
      assert.deepEqual(app.navigations, [`/v/${domain}/device-work-orders/dwo_revision`]);
      assert.ok(!app.calls.some(call => /pagebuilder|internal-estimate/.test(call.path)));
    });
    test(`${domain}: ${entry} with no connected tool gives the connect step without generating anything`, async () => {
      const app = fixture(domain, { connected: false, saved: entry !== "saveRevisionRequest" });
      await app.run(entry);
      assert.match(app.updates.setRevisionError || "", /Settings > Keys and connections > Coding tools/);
      assert.equal(app.calls.filter(call => call.init.method === "POST").length, 0);
      assert.equal(app.updates.setRevisionRun, undefined);
    });
  }
}

test("a retired revision request cannot start a tool or commit its result", async () => {
  for (const retireAt of ["coding-agents", "site/revisions", "site/revisions/estimate", "builds"]) {
    const app = fixture("family-hiking", { retireAt });
    await app.run("saveRevisionRequest");
    assert.equal(app.updates.setRevisionRun, undefined);
    if (retireAt !== "builds") assert.ok(!app.calls.some(call => call.path.endsWith("/builds")));
  }
});

test("a mismatched source receipt cannot authorize a coding order", async () => {
  const app = fixture("warehouse-inventory");
  app.receipt.source_sha256 = "c".repeat(64);
  await app.run("saveRevisionRequest");
  assert.ok(!app.calls.some(call => call.path.endsWith("/builds")));
  assert.match(app.updates.setRevisionError || "", /Refresh Website/);
});

test("an unconfirmed order is never reported as a running revision", async () => {
  const app = fixture("family-hiking", { state: "pending" });
  await app.run("saveRevisionRequest");
  assert.equal(app.updates.setRevisionRun, undefined);
  assert.match(app.updates.setRevisionError || "", /Website|Coding tools/);
});

test("all four connected subscriptions use the same order route", async () => {
  for (const provider of ["claude", "codex", "cursor", "opencode"]) {
    const app = fixture("warehouse-inventory", { provider });
    await app.run("saveRevisionRequest");
    assert.equal(app.updates.setRevisionRun, "dwo_revision");
    assert.equal(app.calls.filter(call => call.path.endsWith("/builds")).length, 1);
  }
});

test("a repeated click cannot create a second revision order", async () => {
  const app = fixture("family-hiking");
  const original = app.context.api;
  let release;
  app.context.api = async (path, init) => {
    if (path === "/api/local-tools/coding-agents") await new Promise(resolve => { release = resolve; });
    return original(path, init);
  };
  const pending = app.run("saveRevisionRequest");
  await app.run("saveRevisionRequest");
  release(); await pending;
  assert.equal(app.calls.filter(call => call.path.endsWith("/builds")).length, 1);
});

test("rejected credentials name the secure settings screen without starting a tool", async () => {
  for (const domain of ["factory-parts", "family-walking"]) {
    const app = fixture(domain, { sourceFailure: { ok: false, status: 400, data: { detail: { code: "device_chat_secret_use_form" } } } });
    await app.run("saveRevisionRequest");
    assert.match(app.updates.setRevisionError, /Remove credentials.*Settings > Keys and connections/);
    assert.ok(!app.calls.some(call => call.path.endsWith("/builds")));
  }
});

for (const domain of ["industrial-scheduling", "home-gardening"]) {
  test(`${domain}: local Website reads do not depend on legacy builder-choice state`, async () => {
    const tree = ts.createSourceFile("page.tsx", readFileSync(join(__dirname, "page.tsx"), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    let expression;
    function visit(node) {
      if (ts.isConditionalExpression(node) && node.whenFalse.getText(tree).includes("/builder/choice")) expression = node.getText(tree);
      ts.forEachChild(node, visit);
    }
    visit(tree);
    assert.ok(expression, "the Website read dispatch distinguishes local page orders from legacy builder state");
    const calls = [], context = { ventureId: domain, signal: new AbortController().signal,
      api: async path => { calls.push(path); return "hosted-choice"; } };
    assert.equal(await vm.runInNewContext(expression, { ...context, renderOwner: { local: true } }), null);
    assert.deepEqual(calls, []);
    assert.equal(await vm.runInNewContext(expression, { ...context, renderOwner: { local: false } }), "hosted-choice");
    assert.deepEqual(calls, [`/api/ventures/${domain}/builder/choice`]);
  });
}

test("a hosted or internal quote never admits a local build fallback", async () => {
  for (const domain of ["factory-parts", "family-walking"]) {
    const app = fixture(domain, { mode: "supymem" });
    await app.run("saveRevisionRequest");
    assert.ok(!app.calls.some(call => call.path.endsWith("/builds")));
    assert.match(app.updates.setRevisionError, /Coding tools/);
  }
});

test("a refused brief keeps the server's exact setting and next step", async () => {
  for (const domain of ["factory-parts", "family-walking"]) {
    const message = "Open Settings > Keys and connections > Spend and update your monthly limit before retrying.";
    const app = fixture(domain, { quoteFailure: { ok: false, status: 402, data: { detail: { message } } } });
    await app.run("saveRevisionRequest");
    assert.equal(app.updates.setRevisionError, message);
    assert.ok(!app.calls.some(call => call.path.endsWith("/builds")));
  }
});
