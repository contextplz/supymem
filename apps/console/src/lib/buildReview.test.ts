import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { acceptedReviewDescription, attentionChecks, buildReceiptRoute, checkedReport, currentReviewReady, downloadBuildReceipt, reviewFailureDescription, reviewMatches, reviewReadinessDescription, type ReviewBuild, type QaReceipt, type QaReport } from "./buildReview.ts";

test("page checks use founder English and collapse desktop/mobile repeats", () => {
  const cases = ["desktop", "mobile"].flatMap(viewport => [
    {id: `${viewport}:action:hero_cta:click`, suite: "actions", target: "hero_cta", status: "failed",
      observed: "Expected accessible control is absent, ambiguous, hidden or disabled", expected: "Declared meaningful behavior after actual activation"},
    {id: `${viewport}:control:4`, suite: "coverage", target: "control 4", status: "unsupported",
      observed: "Visible control has no declared meaningful action", expected: "Every visible control maps to an executed typed expectation"},
  ]);
  assert.deepEqual(attentionChecks(cases), [
    {label: "Main button", message: "we couldn't find or click it.", count: 2},
    {label: "A button or link", message: "doesn't do anything yet.", count: 2},
  ]);
  assert.deepEqual(attentionChecks([{...cases[0], status: "passed"}]), []);
});

test("Website review accepts only its scoped landing build, never a product run", () => {
  const route = {pathname: "/v/venture1/site", search: "?app=app1&run=build1"};
  const build = {id: "build1", app_id: "app1", state: "succeeded", artifact_profile: "static_landing_v1"};
  assert.ok(buildReceiptRoute(route, build));
  assert.equal(buildReceiptRoute(route, {...build, artifact_profile: "founder_product_v1"}), null);
  assert.equal(buildReceiptRoute({...route, search: "?app=app1&run=foreign"}, build), null);
});

test("the actual Builds formatter cannot throw on missing cleanup or malformed error metadata", () => {
  const source = fs.readFileSync(new URL("../app/v/[ventureId]/builds/page.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let initializer = "";
  function visit(node: ts.Node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === "human" && node.initializer) initializer = node.initializer.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  const human = vm.runInNewContext(ts.transpile(`(${initializer})`, {target: ts.ScriptTarget.ES2022}));
  for (const value of [null, undefined, 1, {}]) assert.equal(human(value), "Not recorded");
  assert.equal(human("qa_checks.failed"), "qa checks failed");
});

function fixture() {
  const report: QaReport = { schema_version: 1, qa_run_id: "qa1", build_run_id: "build1", source_artifact_id: "artifact1",
    source_sha256: "a".repeat(64), contract_digest: "c".repeat(64), outcome: "passed", exclusions: ["Static site only"],
    cases: [{ id: "render", suite: "render", target: "index.html", status: "passed", observed: "Product title is visible", expected: "Product title" }], evidence: [] };
  const qa: QaReceipt = { id: "qa1", runtime_version: 1, state: "passed", verified_report: true, verified_execution: true,
    passed: 1, failed: 0, unsupported: 0, source_artifact_id: "artifact1", source_sha256: "a".repeat(64), report_digest: "d".repeat(64) };
  const build: ReviewBuild = { id: "build1", runtime_version: 1, lifecycle_version: 2, state: "succeeded", verified_execution: true,
    completion_status: "closed", artifact_id: "artifact1", qa_run_id: "qa1", qa_state: "passed",
    review: { status: "pending", source_artifact_id: "artifact1", report_digest: qa.report_digest!, qa_id: "qa1", contract_digest: report.contract_digest } };
  return { build, qa, report };
}

test("milestone acceptance requires the same finalized source, report and QA", () => {
  const { build, qa, report } = fixture();
  assert.equal(reviewMatches(build, qa, report), true);
  for (const changed of [{ completion_status: "reconciling" }, { verified_execution: false }, { lifecycle_version: 1 },
    { artifact_id: "foreign" }, { qa_run_id: "other" }, { state: "failed" }, { review: { ...build.review!, status: "accepted" } }]) {
    assert.equal(reviewMatches({ ...build, ...changed }, qa, report), false);
  }
  assert.equal(reviewMatches(build, { ...qa, verified_execution: false }, report), false);
  assert.equal(reviewMatches(build, { ...qa, report_digest: "changed" }, report), false);
  assert.equal(reviewMatches(build, qa, { ...report, source_artifact_id: "foreign" }), false);
  assert.equal(reviewMatches(build, qa, { ...report, contract_digest: "changed" }), false);
  assert.equal(reviewMatches(build, qa, { ...report, cases: [{ ...report.cases[0], status: "unsupported" }] }), false);
  assert.equal(reviewMatches(build, qa, null), false);
});

test("raw report bytes must match their server receipt and selected build", async () => {
  const { build, qa, report } = fixture();
  const bytes = new TextEncoder().encode(JSON.stringify(report)).buffer;
  qa.report_digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
  assert.deepEqual(await checkedReport(bytes, build, qa), report);
  await assert.rejects(checkedReport(new TextEncoder().encode(JSON.stringify({ ...report, outcome: "failed" })).buffer, build, qa), /does not match/);
  await assert.rejects(checkedReport(bytes, { ...build, id: "foreign" }, qa), /does not belong/);
  await assert.rejects(checkedReport(bytes, build, { ...qa, verified_report: false }), /not verified/);
});


test("subscription source review requires its distinct closed QA proof without builder claims", () => {
  const { build, qa, report } = fixture();
  const imported = { ...build, lifecycle_version: 3, mode: "subscription_static_source_qa",
    verified_execution: false, verified_source_import: true, source_custody_verified: true };
  const independent = { ...qa, verified_execution: false, verified_source_import: true };
  assert.equal(reviewMatches(imported, independent, report), true);
  for (const changed of [{ mode: "managed_builder" }, { source_custody_verified: false },
    { verified_source_import: false }, { verified_execution: true }, { lifecycle_version: 2 }]) {
    assert.equal(reviewMatches({ ...imported, ...changed }, independent, report), false);
  }
  assert.equal(reviewMatches(imported, { ...independent, verified_execution: true }, report), false);
  assert.equal(reviewMatches(imported, { ...independent, verified_source_import: false }, report), false);
  assert.equal(reviewMatches(imported, independent, { ...report, source_artifact_id: "foreign" }), false);
});

test("server candidate QA keeps the same independent proof and rejects invented coding execution", () => {
  const {build, qa, report} = fixture();
  const source = {...build, lifecycle_version: 3, mode: "pagebuilder_source_qa", verified_execution: false,
    verified_source_import: true, source_custody_verified: true};
  const independent = {...qa, verified_execution: false, verified_source_import: true};
  assert.equal(reviewMatches(source, independent, report), true);
  for (const change of [{mode: "pagebuilder"}, {verified_execution: true}, {source_custody_verified: false},
    {verified_source_import: false}, {completion_status: "active"}, {qa_state: "failed"}])
    assert.equal(reviewMatches({...source, ...change}, independent, report), false);
  assert.equal(reviewMatches(source, {...independent, verified_execution: true}, report), false);
  assert.equal(reviewMatches(source, independent, {...report, contract_digest: "foreign"}), false);
});

test("saved review attribution does not invent a historical founder decision", () => {
  assert.match(acceptedReviewDescription({status: "accepted", review_origin: "founder"}), /^You accepted/);
  assert.match(acceptedReviewDescription({status: "accepted", review_origin: "godmode"}), /^Godmode accepted/);
  assert.match(acceptedReviewDescription({status: "accepted"}), /provenance is not recorded/);
});

test("accepted public static source stays incomplete until its exact publication completion receipt", () => {
  const { build } = fixture();
  const review = { status: "accepted", review_origin: "founder" as const };
  const publicSource = { ...build, publication_required: true };
  assert.match(acceptedReviewDescription(review, publicSource), /still requires verified publication/);
  assert.match(acceptedReviewDescription(review, { ...publicSource, task_completion: { state: "pending_publication" } }), /still requires/);
  assert.match(acceptedReviewDescription(review, { ...publicSource, task_completion: { state: "completed" } }), /publication and public waitlist submission are verified/);
  assert.match(acceptedReviewDescription(review, { ...build, publication_required: false }), /plan records it as complete/);
});

test("actual review renderer labels public source acceptance without promising whole-task completion", () => {
  const source = fs.readFileSync(new URL("../components/builds/BuildEvidence.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("BuildEvidence.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "BuildEvidence")!;
  const code = ts.transpileModule(component.getText(tree).replace(/^export /, "") + ";globalThis.render=BuildEvidence;", {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  const elements: any[] = [];
  const context = vm.createContext({ encodeURIComponent, reviewMatches, currentReviewReady, reviewFailureDescription, reviewReadinessDescription, acceptedReviewDescription, buildReceiptRoute, downloadBuildReceipt,
    verifiedSessionSubject: () => "owner1", verifiedSessionEpoch: () => 1,
    useRef: (value: unknown) => ({ current: value }), useState: (value: unknown) => [value, () => undefined], useEffect: () => undefined,
    ErrorNotice: () => null, Screenshot: () => null, PrivateCorrection: () => null,
    React: { createElement: (type: any, props: any, ...children: any[]) => { const element = { type, props, children }; elements.push(element); return element; } } });
  vm.runInContext(code, context);
  const { build } = fixture();
  context.render({ build: { ...build, publication_required: true }, onReviewed: () => undefined });
  assert.ok(elements.some(e => e.type === "button" && e.children.includes("Accept source")));
  assert.ok(elements.some(e => e.type === "p" && e.children.some((c: unknown) => typeof c === "string" && c.includes("this plan task stays incomplete"))));
  assert.equal(elements.some(e => e.children.includes("Accept milestone")), false);
  elements.length = 0;
  context.render({ build: { ...build, publication_required: true, review: { status: "accepted", review_origin: "founder" } }, onReviewed: () => undefined });
  assert.ok(elements.some(e => e.props?.role === "status" && e.children.some((c: unknown) => typeof c === "string" && c.includes("still requires verified publication"))));
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => {resolve = done;});
  return {promise, resolve};
}

async function withDownloadUi(work: (ui: {blobs: Blob[]; clicked: string[]; revoked: string[]; beforeUrl?: () => void; beforeAnchor?: () => void}) => Promise<void>) {
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const originalCreate = URL.createObjectURL, originalRevoke = URL.revokeObjectURL, originalTimeout = globalThis.setTimeout;
  const ui = {blobs: [] as Blob[], clicked: [] as string[], revoked: [] as string[], beforeUrl: undefined as (() => void) | undefined, beforeAnchor: undefined as (() => void) | undefined};
  URL.createObjectURL = blob => {if (!(blob instanceof Blob)) throw new Error("Expected file bytes."); ui.blobs.push(blob); ui.beforeUrl?.(); return "blob:private-build";};
  URL.revokeObjectURL = url => {ui.revoked.push(url);};
  globalThis.setTimeout = (() => 0) as unknown as typeof setTimeout;
  Object.defineProperty(globalThis, "document", {configurable: true, value: {createElement: () => {
    ui.beforeAnchor?.(); return {href: "", download: "", click() {ui.clicked.push(this.download);}};
  }}});
  try {await work(ui);}
  finally {
    URL.createObjectURL = originalCreate; URL.revokeObjectURL = originalRevoke; globalThis.setTimeout = originalTimeout;
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument); else Reflect.deleteProperty(globalThis, "document");
  }
}

function receiptResponse(id = "build1", app = "app1") {
  return new Response(JSON.stringify({id, app_id: app, runtime_version: 1, plan_id: "plan-current", plan_task_id: "task-current",
    state: "succeeded", review: {status: "accepted"}, task_completion: {state: "pending_publication"}}), {headers: {"Content-Type": "application/json"}});
}

test("build receipt route binds exact venture, selected app, build and task selectors", () => {
  const build = {...fixture().build, app_id: "app1"};
  const route = {pathname: "/v/venture1/builds", search: "?app=app1&run=build1&task=task1"};
  assert.ok(buildReceiptRoute(route, build));
  assert.notEqual(buildReceiptRoute({...route, pathname: "/v/venture2/builds"}, build), buildReceiptRoute(route, build));
  assert.notEqual(buildReceiptRoute({...route, search: "?app=app1&run=build1&task=task2"}, build), buildReceiptRoute(route, build));
  for (const search of ["?app=other", "?run=other"]) assert.equal(buildReceiptRoute({...route, search}, build), null);
  assert.equal(buildReceiptRoute({pathname: "/v/venture1/plan", search: ""}, build), null);
  assert.equal(buildReceiptRoute({pathname: "/v/%2F/builds", search: ""}, build), null);
  assert.equal(buildReceiptRoute(route, {...build, app_id: undefined}), null);
});

test("receipt download preserves the fresh GET bytes and actual pending publication binding", async () => {
  await withDownloadUi(async ui => {
    const build = {...fixture().build, app_id: "app1"};
    const raw = '{ "id":"build1", "app_id":"app1", "runtime_version":1, "plan_id":"actual-plan", "review":{"status":"accepted"}, "task_completion":{"state":"pending_publication"} }\n';
    assert.equal(await downloadBuildReceipt(build, async () => new Response(raw, {headers: {"Content-Type": "application/json; charset=utf-8"}}), () => true), true);
    assert.deepEqual(ui.clicked, ["build-build1.json"]);
    assert.equal(await ui.blobs[0].text(), raw);
  });
});

test("foreign, malformed, oversized and non-JSON build responses never download", async () => {
  await withDownloadUi(async ui => {
    const build = {...fixture().build, app_id: "app1"};
    const responses = [receiptResponse("other"), receiptResponse("build1", "other"), new Response('{"id":"build1","app_id":"app1","runtime_version":0}', {headers: {"Content-Type": "application/json"}}),
      new Response("{}", {status: 403, headers: {"Content-Type": "application/json"}}), new Response("login", {headers: {"Content-Type": "text/html"}}),
      new Response("[", {headers: {"Content-Type": "application/json"}}), new Response(" ".repeat(1024 * 1024 + 1), {headers: {"Content-Type": "application/json"}})];
    for (const response of responses) assert.equal(await downloadBuildReceipt(build, async () => response, () => true), false);
    assert.equal(ui.blobs.length, 0); assert.equal(ui.clicked.length, 0);
  });
});

test("receipt handoff rechecks custody after response and body reads and local preparation", async () => {
  await withDownloadUi(async ui => {
    const build = {...fixture().build, app_id: "app1"};
    let current = false, reads = 0;
    assert.equal(await downloadBuildReceipt(build, async () => {reads++; return receiptResponse();}, () => current), false);
    assert.equal(reads, 0);
    current = true;
    assert.equal(await downloadBuildReceipt(build, async () => {current = false; return receiptResponse();}, () => current), false);
    current = true;
    const body = deferred<ArrayBuffer>(), response = receiptResponse();
    response.arrayBuffer = () => body.promise;
    const pending = downloadBuildReceipt(build, async () => response, () => current);
    await Promise.resolve(); current = false; body.resolve(new TextEncoder().encode(await receiptResponse().text()).buffer);
    assert.equal(await pending, false);
    for (const stage of ["beforeUrl", "beforeAnchor"] as const) {
      current = true; ui[stage] = () => {current = false;};
      assert.equal(await downloadBuildReceipt(build, async () => receiptResponse(), () => current), false);
      ui[stage] = undefined;
    }
    assert.equal(ui.clicked.length, 0); assert.equal(ui.revoked.length, 2);
  });
});

function receiptRenderer() {
  const source = fs.readFileSync(new URL("../components/builds/BuildEvidence.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("BuildEvidence.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "BuildEvidence")!;
  const code = ts.transpileModule(component.getText(tree).replace(/^export /, "") + ";globalThis.render=BuildEvidence;", {
    compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
  }).outputText;
  const refs: {current: any}[] = [], states: unknown[] = [], elements: any[] = [];
  let refIndex = 0, stateIndex = 0, mounted = false;
  const effects: (() => void | (() => void))[] = [], cleanups: (() => void)[] = [];
  const owner = {actor: "owner1", epoch: 1}, location = {pathname: "/v/venture1/builds", search: "?app=app1&run=build1"};
  const reads: string[] = [];
  let read = async () => receiptResponse();
  const context = vm.createContext({JSON, encodeURIComponent, buildReceiptRoute, downloadBuildReceipt, reviewMatches, currentReviewReady, reviewFailureDescription, reviewReadinessDescription, acceptedReviewDescription,
    window: {location}, verifiedSessionSubject: () => owner.actor, verifiedSessionEpoch: () => owner.epoch,
    authenticatedFetch: (path: string) => {reads.push(path); return read();},
    useRef: (value: unknown) => refs[refIndex++] ||= {current: value},
    useState: (value: unknown) => {const index = stateIndex++; if (!(index in states)) states[index] = value; return [states[index], (next: unknown) => {states[index] = next;}];},
    useEffect: (effect: () => void | (() => void)) => {if (!mounted) effects.push(effect);},
    ErrorNotice: () => null, Screenshot: () => null, PrivateCorrection: () => null,
    React: {createElement: (type: any, props: any, ...children: any[]) => {const element = {type, props, children}; elements.push(element); return element;}},
  });
  vm.runInContext(code, context);
  const build = {...fixture().build, app_id: "app1", review: {status: "accepted"}};
  function render(selected = build) {
    refIndex = 0; stateIndex = 0; elements.length = 0;
    context.render({build: selected, onReviewed: () => undefined});
    if (!mounted) {mounted = true; for (const effect of effects) {const cleanup = effect(); if (cleanup) cleanups.push(cleanup);}}
    return elements.find(element => element.type === "button" && element.children.includes("Download build receipt")).props.onClick as () => void;
  }
  return {owner, location, reads, states, build, render, setRead: (value: typeof read) => {read = value;}, unmount: () => cleanups.forEach(cleanup => cleanup())};
}

test("actual receipt callback fetches the current build once and rejects retained actor, epoch and route changes", async () => {
  await withDownloadUi(async ui => {
    const page = receiptRenderer(), click = page.render();
    click(); click(); await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(page.reads, ["/api/builds/build1"]); assert.equal(ui.clicked.length, 1);
    for (const change of [() => {page.owner.actor = "owner2";}, () => {page.owner.actor = "owner1"; page.owner.epoch++;},
      () => {page.location.pathname = "/v/venture2/builds";}, () => {page.location.search = "?run=other";}]) {
      change(); click(); await new Promise(resolve => setImmediate(resolve));
      assert.equal(page.reads.length, 1);
    }
  });
});

test("actual callback withholds delayed bytes after build selection, route retirement or unmount", async () => {
  await withDownloadUi(async ui => {
    for (const retire of ["build", "route", "unmount", "actorABA"] as const) {
      const page = receiptRenderer(), response = deferred<Response>(); page.setRead(() => response.promise);
      const oldClick = page.render(); oldClick();
      if (retire === "build") {page.location.search = "?app=app1&run=build2"; page.render({...page.build, id: "build2"});}
      if (retire === "route") page.location.pathname = "/v/venture2/builds";
      if (retire === "unmount") page.unmount();
      if (retire === "actorABA") {page.owner.epoch += 2;}
      response.resolve(receiptResponse()); await new Promise(resolve => setImmediate(resolve));
      oldClick(); await new Promise(resolve => setImmediate(resolve));
      assert.equal(page.reads.length, 1);
    }
    assert.equal(ui.clicked.length, 0); assert.equal(ui.blobs.length, 0);
  });
});

test("a fresh selected build can download while an older build read is still outstanding", async () => {
  await withDownloadUi(async ui => {
    const page = receiptRenderer(), oldResponse = deferred<Response>(); page.setRead(() => oldResponse.promise);
    page.render()();
    page.location.search = "?app=app1&run=build2";
    page.setRead(async () => receiptResponse("build2"));
    page.render({...page.build, id: "build2"})();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(page.reads, ["/api/builds/build1", "/api/builds/build2"]);
    assert.deepEqual(ui.clicked, ["build-build2.json"]);
    oldResponse.resolve(receiptResponse()); await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(ui.clicked, ["build-build2.json"]);
  });
});
