import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { acceptedReviewDescription, buildReceiptRoute, checkedReport, currentReviewReady, downloadBuildReceipt,
  reviewFailureDescription, reviewMatches, reviewReadinessDescription, type ReviewBuild } from "./buildReview.ts";
import { resourceReadFailure } from "./apiFailure.ts";

const ready = {schema_version: 1, status: "ready", code: null, error_code: null};
function fixture() {
  const report = {schema_version: 1, qa_run_id: "qa1", build_run_id: "build1", source_artifact_id: "artifact1",
    source_sha256: "a".repeat(64), contract_digest: "c".repeat(64), outcome: "passed", exclusions: [], evidence: [],
    cases: [{id: "render", suite: "render", target: "index.html", status: "passed", observed: "Visible", expected: "Visible"}]};
  const qa = {id: "qa1", runtime_version: 1, state: "passed", verified_report: true, verified_execution: false,
    verified_source_import: true, passed: 49, failed: 0, unsupported: 0, source_artifact_id: "artifact1",
    source_sha256: report.source_sha256, report_digest: ""};
  const build: ReviewBuild = {id: "build1", app_id: "app1", runtime_version: 1, lifecycle_version: 3,
    state: "succeeded", mode: "subscription_static_source_qa", verified_execution: false,
    verified_source_import: true, source_custody_verified: true, completion_status: "closed",
    artifact_id: "artifact1", qa_run_id: "qa1", qa_state: "passed", publication_required: true,
    review: {status: "pending", source_artifact_id: "artifact1", report_digest: "", qa_id: "qa1", contract_digest: report.contract_digest},
    review_readiness: ready};
  return {build, qa, report};
}
function deferred<T>() {let resolve!: (value: T) => void; const promise = new Promise<T>(done => {resolve = done;}); return {promise, resolve};}
async function flush() {for (let i = 0; i < 8; i++) await new Promise(resolve => setTimeout(resolve, 10));}
async function renderer() {
  const f = fixture(), raw = new TextEncoder().encode(JSON.stringify(f.report));
  f.qa.report_digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", raw)), b => b.toString(16).padStart(2, "0")).join("");
  f.build.review!.report_digest = f.qa.report_digest;
  const source = fs.readFileSync(new URL("../components/builds/BuildEvidence.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("BuildEvidence.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find((n): n is ts.FunctionDeclaration => ts.isFunctionDeclaration(n) && n.name?.text === "BuildEvidence")!;
  const code = ts.transpileModule(component.getText(tree).replace(/^export /, "") + ";globalThis.render=BuildEvidence;", {
    compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
  }).outputText;
  const refs: {current: any}[] = [], states: any[] = [], dependencies: any[][] = [], elements: any[] = [], cleanups: (() => void)[] = [];
  let ri = 0, si = 0, ei = 0, effects: (() => void | (() => void))[] = [], refreshed = 0;
  const owner = {actor: "owner1", epoch: 1}, location = {pathname: "/v/venture1/builds", search: "?app=app1&run=build1"};
  const calls: {path: string; options?: any}[] = [];
  let read: () => Promise<any> = async () => ({ok: true, data: f.qa});
  let post: () => Promise<any> = async () => ({ok: true, data: {review: {status: "accepted"}}});
  let fetchReport = async () => new Response(raw, {headers: {"content-type": "application/json"}});
  const context = vm.createContext({JSON, encodeURIComponent, acceptedReviewDescription, buildReceiptRoute, checkedReport,
    currentReviewReady, downloadBuildReceipt, reviewFailureDescription, reviewMatches, reviewReadinessDescription,
    window: {location}, verifiedSessionSubject: () => owner.actor, verifiedSessionEpoch: () => owner.epoch,
    api: (path: string, options?: any) => {calls.push({path, options}); return options?.method === "POST" ? post() : read();},
    apiFailure: () => "Generic review refusal", resourceReadFailure, announceDiagnostic: () => undefined,
    authenticatedFetch: () => fetchReport(),
    useRef: (value: any) => refs[ri++] ||= {current: value},
    useState: (value: any) => {const index = si++; if (!(index in states)) states[index] = value;
      return [states[index], (next: any) => {states[index] = next;}];},
    useEffect: (effect: () => void | (() => void), deps: any[]) => {const index = ei++, prior = dependencies[index];
      if (!prior || deps.some((v, i) => !Object.is(v, prior[i]))) {dependencies[index] = deps; effects.push(effect);}},
    ErrorNotice: () => null, Screenshot: () => null, PrivateCorrection: () => null,
    React: {createElement: (type: any, props: any, ...children: any[]) => {const item = {type, props, children}; elements.push(item); return item;}},
  });
  vm.runInContext(code, context);
  function render(build = f.build) {
    ri = si = ei = 0; elements.length = 0; effects = [];
    context.render({build, onReviewed: () => {refreshed++;}});
    for (const effect of effects) {const cleanup = effect(); if (cleanup) cleanups.push(cleanup);}
    return elements;
  }
  const button = (label: string) => elements.find(e => e.type === "button" && e.children.includes(label)).props;
  return {...f, owner, location, calls, states, render, button, refreshed: () => refreshed,
    setRead: (value: typeof read) => {read = value;}, setPost: (value: typeof post) => {post = value;},
    setFetch: (value: typeof fetchReport) => {fetchReport = value;}};
}

test("a failed QA report read offers a scoped retry without replaying a mutation", async () => {
  const page = await renderer();
  let reads = 0;
  page.setFetch(async () => {
    reads++;
    return new Response(JSON.stringify({error: {code: "keychain_unavailable"}}),
      {status: 503, headers: {"content-type": "application/json"}});
  });
  page.render(); await flush(); page.render();
  assert.ok(page.states.includes("QA report could not be read. Open your venture's Website screen and refresh the saved preview, then try again."));
  const retry = page.button("Retry QA results");
  assert.equal(retry.disabled, false);
  retry.onClick(); await flush(); page.render();
  assert.equal(reads, 2);
  assert.equal(page.calls.some(call => call.options?.method === "POST"), false);
  page.owner.epoch++;
  retry.onClick(); await flush();
  assert.equal(reads, 2);
});

test("saved QA equality never supplies missing, malformed or blocked current readiness", () => {
  const {build, qa, report} = fixture(); qa.report_digest = "d".repeat(64); build.review!.report_digest = qa.report_digest;
  assert.equal(reviewMatches(build, qa, report), true);
  assert.equal(currentReviewReady(build), true);
  for (const check of [undefined, {...ready, schema_version: 2}, {...ready, status: "blocked"},
    {...ready, code: "unknown"}, {...ready, error_code: "unknown"}]) assert.equal(currentReviewReady({...build, review_readiness: check}), false);
  assert.match(reviewReadinessDescription({...build, review_readiness: null}), /could not be checked/);
  assert.equal(reviewFailureDescription({code: "unknown", reasons: ["private"]}), null);
  assert.doesNotMatch(reviewFailureDescription({code: "build_review_not_ready", reasons: ["private provider text"]})!, /private provider/);
});

test("actual blocked review shows saved49 separately and forced callback sends no POST", async () => {
  const p = await renderer(), blocked = {...p.build, review_readiness: {...ready, status: "blocked", code: "subscription_static_approved_inputs_changed", error_code: "build_review_not_ready"}};
  p.render(blocked); await flush(); const nodes = p.render(blocked);
  assert.equal(p.button("Accept source").disabled, true);
  p.button("Accept source").onClick(); await flush();
  assert.equal(p.calls.some(c => c.options?.method === "POST"), false);
  assert.ok(nodes.some(e => e.props?.role === "status" && e.children.some((c: any) => typeof c === "string" && c.includes("Current approved inputs differ"))));
  assert.ok(nodes.some(e => e.type === "p" && e.children.includes(49)));
});

test("actual ready review sends exact receipt once and explains fresh server refusal", async () => {
  const p = await renderer(); p.render(); await flush(); p.render();
  assert.equal(p.button("Accept source").disabled, false);
  p.setPost(async () => ({ok: false, status: 409, data: {detail: {code: "build_review_not_ready", reasons: ["subscription_static_approved_inputs_changed"]}}}));
  const callback = p.button("Accept source").onClick; callback(); callback(); await flush();
  const posts = p.calls.filter(c => c.options?.method === "POST"); assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0].options.body), {source_artifact_id: "artifact1", report_digest: p.qa.report_digest, decision: "accept"});
  assert.ok(p.states.some(v => typeof v === "string" && v.includes("Current approved inputs differ")));
  assert.equal(p.refreshed(), 1);
});

test("same-id equal fresh receipt does not strand inspect busy, while changed readiness retires old callback", async () => {
  const p = await renderer(), pending = deferred<any>(); p.setRead(() => pending.promise);
  p.render(); const fresh = structuredClone(p.build); p.render(fresh);
  pending.resolve({ok: true, data: p.qa}); await flush(); p.render(fresh);
  assert.equal(p.button("Accept source").disabled, false);
  const retained = p.button("Accept source").onClick;
  const changed = {...fresh, review_readiness: {...ready, status: "check_unavailable", code: "build_review_check_unavailable", error_code: "build_review_not_ready"}};
  p.render(changed); retained(); await flush(); p.render(changed);
  assert.equal(p.calls.some(c => c.options?.method === "POST"), false);
  assert.equal(p.button("Reload QA results").disabled, false);
});

test("retained ready callback refuses actor, epoch and live route retirement", async () => {
  for (const retire of [(p: any) => {p.owner.actor = "owner2";}, (p: any) => {p.owner.epoch++;},
    (p: any) => {p.location.pathname = "/v/other/builds";}, (p: any) => {p.location.search = "?run=other";}]) {
    const p = await renderer(); p.render(); await flush(); p.render(); const click = p.button("Accept source").onClick;
    retire(p); click(); await flush(); assert.equal(p.calls.some(c => c.options?.method === "POST"), false);
  }
});

test("pending inspect retires on same-build session change without leaving busy stranded", async () => {
  const p = await renderer(), old = deferred<any>(); p.setRead(() => old.promise); p.render();
  p.owner.epoch++; p.setRead(async () => ({ok: true, data: p.qa})); p.render(); await flush(); p.render();
  assert.equal(p.button("Accept source").disabled, false);
  old.resolve({ok: false, data: null}); await flush(); p.render();
  assert.equal(p.button("Accept source").disabled, false);
  assert.equal(p.calls.some(c => c.options?.method === "POST"), false);
});
