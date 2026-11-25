import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { parseBrowserExecutionProfile, browserExecutionProfileDigest, browserReadOnlyAuthority } from "./browserExecutionProfile.ts";
import { generalBrowserOrderAvailability, generalBrowserExecutionMessage } from "./generalBrowserExecutionAvailability.ts";
import { desktopExecutionAvailability, desktopExecutionMessage, isNativeDesktopManifest } from "./desktopExecutionAvailability.ts";
import { browserProfileWireSupported, BROWSER_PROFILE_WIRE_MESSAGE } from "./browserProfileWireAvailability.ts";

const role = (max_output_tokens: number) => ({provider: "openai", model: "gpt-6-sol", service_tier: "default", reasoning: "none", max_output_tokens});
function profile() {
  return {version: "browser_cuai_text_v1", backend: "cuai", policy_version: 1,
    roles: {plan: role(700), actor: role(600), verify: role(300)}, pricing_digest: "a".repeat(64),
    max_provider_calls: 64, max_request_bytes: 65536, completion_policy: "browser_semantic_review_v1"};
}
// Independently computed with Core's Python sorted ensure_ascii serializer.
const digest = "aaf39dea79539023f76b1242e9ce355658380b864f484ca5a49fce4999e36925";
const capabilities = {ok: true, data: {available: true, general_browser_execution_v1: true,
  browser_cuai_text_v1: true, execution_profile_digest: digest}};

test("saved profile is closed and bounded without model defaults", async () => {
  assert.ok(parseBrowserExecutionProfile(profile()));
  const invalid = [undefined, {}, {...profile(), extra: true}, {...profile(), version: "future"},
    {...profile(), pricing_digest: "A".repeat(64)}, {...profile(), max_provider_calls: 129},
    {...profile(), max_request_bytes: 1023}, {...profile(), roles: {...profile().roles, extra: role(1)}},
    {...profile(), roles: {...profile().roles, actor: {...role(600), model: ""}}},
    {...profile(), roles: {...profile().roles, actor: {...role(600), model: "<script>bad</script>"}}},
    {...profile(), roles: {...profile().roles, actor: {...role(600), service_tier: "auto"}}},
    {...profile(), roles: {...profile().roles, actor: {...role(600), max_output_tokens: true}}}];
  for (const item of invalid) { assert.equal(parseBrowserExecutionProfile(item), null); assert.equal(await browserExecutionProfileDigest(item), null); }
});

test("Azure profile is exact, bounded and preserves legacy OpenAI digests", async () => {
  const value = profile();
  for (const phase of ["plan", "actor", "verify"] as const) {
    Object.assign(value.roles[phase], {provider: "azure", model: "gpt-6-luna",
      endpoint: "https://example.openai.azure.com/openai/v1", deployment: "gpt-6-luna"});
  }
  assert.ok(parseBrowserExecutionProfile(value));
  assert.equal(await browserExecutionProfileDigest(profile()), digest);
  assert.notEqual(await browserExecutionProfileDigest(value), digest);
  for (const endpoint of ["https://evil.invalid/openai/v1", "http://example.openai.azure.com/openai/v1",
    "https://key@example.openai.azure.com/openai/v1", "https://example.openai.azure.com/openai/v1?key=secret"]) {
    assert.equal(parseBrowserExecutionProfile({...value, roles: {...value.roles, actor: {...value.roles.actor, endpoint}}}), null);
  }
  assert.equal(parseBrowserExecutionProfile({...value, roles: {...value.roles, actor: {...value.roles.actor, deployment: ""}}}), null);
});

test("only the saved 6.1 Sol actor may use low effort; other roles and limits stay exact", async () => {
  for (const [version, policy_version] of [["browser_cuai_text_v1", 1], ["browser_cuai_text_v2", 2]]) {
    const value = {...profile(), version, policy_version};
    value.roles.actor = {...value.roles.actor, model: "gpt-6.1-sol", reasoning: "low"};
    assert.deepEqual(parseBrowserExecutionProfile(value), value);
    assert.notEqual(await browserExecutionProfileDigest(value), await browserExecutionProfileDigest(profile()));
    for (const [phase, model, reasoning] of [["actor", "gpt-6.1-sol", "none"], ["actor", "gpt-6-sol", "low"],
      ["actor", "gpt-6-luna", "low"], ["plan", "gpt-6.1-sol", "low"], ["verify", "gpt-6.1-sol", "low"],
      ["plan", "gpt-6.1-sol", "none"], ["verify", "gpt-6.1-sol", "none"]] as const) {
      const changed = structuredClone(value); changed.roles[phase] = {...changed.roles[phase], model, reasoning};
      assert.equal(parseBrowserExecutionProfile(changed), null);
      assert.equal(await browserExecutionProfileDigest(changed), null);
    }
  }
  assert.equal(await browserExecutionProfileDigest(profile()), digest);
});

test("v2 is an exact separately reviewed policy and cannot borrow v1 capabilities", async () => {
  const v2={...profile(),version:"browser_cuai_text_v2",policy_version:2};
  assert.ok(parseBrowserExecutionProfile(v2));
  assert.equal(parseBrowserExecutionProfile({...v2,policy_version:1}),null);
  assert.equal(parseBrowserExecutionProfile({...profile(),policy_version:2}),null);
  const v2Digest=await browserExecutionProfileDigest(v2);
  assert.notEqual(v2Digest,digest);
  const old={ok:true,data:{...capabilities.data,execution_profile_digest:v2Digest}};
  assert.equal(await generalBrowserOrderAvailability(old,v2),"unknown");
  const current={ok:true,data:{...old.data,browser_cuai_text_v2:true}};
  assert.equal(await generalBrowserOrderAvailability(current,v2),"available");
  for(const action of ["approve","start","continue","handoff"]){
    const f=fixture(current,v2);await actual(action==="approve"?"act":"nativeAction",f.context)(action);
    assert.deepEqual(f.calls,["capabilities"]);
    f.setWire({browser_cuai_text_v1:true,browser_cuai_text_v2:true});
    await actual(action==="approve"?"act":"nativeAction",f.context)(action);
    assert.equal(f.calls.length,3);
  }
});

test("canonical digest matches Core independent serializer and changes with every role or pricing limit", async () => {
  assert.equal(await browserExecutionProfileDigest(profile()), digest);
  assert.equal(await browserExecutionProfileDigest(Object.fromEntries(Object.entries(profile()).reverse())), digest);
  for (const item of [{...profile(), pricing_digest: "b".repeat(64)}, {...profile(), max_provider_calls: 63},
    {...profile(), roles: {...profile().roles, verify: {...role(300), model: "gpt-6-sol-alt"}}}]) {
    assert.notEqual(await browserExecutionProfileDigest(item), digest);
  }
});

test("new profile needs all explicit capabilities and exact digest, legacy grant cannot substitute", async () => {
  assert.equal(await generalBrowserOrderAvailability(capabilities, profile()), "available");
  for (const data of [{available: true, general_browser_execution_v1: true},
    {...capabilities.data, browser_cuai_text_v1: false}, {...capabilities.data, browser_cuai_text_v1: "true"},
    {...capabilities.data, execution_profile_digest: "b".repeat(64)}, {...capabilities.data, general_browser_execution_v1: false}]) {
    assert.notEqual(await generalBrowserOrderAvailability({ok: true, data}, profile()), "available");
  }
  assert.equal(await generalBrowserOrderAvailability(capabilities, {...profile(), provider: "ignored"}), "unavailable");
  assert.equal(await generalBrowserOrderAvailability({ok: true, data: {available: true, general_browser_execution_v1: true}}, null), "available");
});

const pageSource = fs.readFileSync(new URL("../app/v/[ventureId]/device-work-orders/[orderId]/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("review.tsx", pageSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function actual(name: string, globals: Record<string, unknown>) {
  let callback = "";
  function visit(node: ts.Node) { if (ts.isFunctionDeclaration(node) && node.name?.text === name) callback = node.getText(tree); ts.forEachChild(node, visit); }
  visit(tree); assert.ok(callback);
  const context = vm.createContext({ generalBrowserOrderAvailability, generalBrowserExecutionMessage, parseBrowserExecutionProfile, browserReadOnlyAuthority,
    desktopExecutionAvailability, desktopExecutionMessage, isNativeDesktopManifest,
    productGrantBlocked: false, productCapsDirty: false, isDesktopShell: () => true, isLocalConsole: () => false, featureVisible: () => true, browserBuild: false,
    verifiedSessionSubject: () => "usr_synthetic", verifiedSessionEpoch: () => 0,
    setProductAvailable: () => {}, setProductScope: () => {}, setProductPlan: () => {}, setProductCapsDirty: () => {},
    setLoopIterations: () => {}, setDeployReceipt: () => {}, setFullActivity: () => {}, setCopiedFolder: () => {}, ...globals });
  vm.runInContext(ts.transpileModule(`${callback};globalThis.actual=${name};`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
  return context.actual as (action?: string) => Promise<void>;
}
function fixture(fresh: unknown = capabilities, savedProfile: unknown = profile()) {
  const calls: string[] = [], errors: string[] = [], statuses: string[] = [], bodies: unknown[] = [];
  let localWire: unknown = {browser_cuai_text_v1: true}, wireCalls = 0;
  const context = {order: {manifest: {general_browser_verification: {execution_profile: savedProfile}}, review_digest: "saved"},
    browserProfileWireAvailable: async (version = "browser_cuai_text_v1", readOnly = false, qualification = false) => {wireCalls++; return browserProfileWireSupported(localWire, version, readOnly, qualification);}, BROWSER_PROFILE_WIRE_MESSAGE,
    generalAvailability: "available", desktopGrantBlocked: false, desktopAvailability: "unknown",
    inFlight: {current: false}, alive: {current: true}, serial: {current: 1}, acknowledged: true,
    path: "/api/ventures/ven_a/device-work-orders/dwo_a", ventureId: "ven_a", orderId: "dwo_a",
    setBusy: () => {}, setChecking: () => {}, setError: (x: string) => errors.push(x), setGeneralAvailability: (x: string) => statuses.push(x),
    setDesktopAvailability: () => {}, setNativeReceipt: () => {}, setOrder: () => {}, setAcknowledged: () => {},
    desktopInvokeOrThrow: async (_: string, body: {action: string}) => {calls.push(body.action); return {};},
    api: async (path: string, options?: {body?: string}) => {calls.push(path.endsWith("/capabilities") ? "capabilities" : "approve"); if(options?.body)bodies.push(JSON.parse(options.body)); return fresh;},
    load: async () => {}, window: {requestAnimationFrame: () => {}}, focusActions: () => {}};
  return {context, calls, errors, statuses, bodies, setWire: (value: unknown) => {localWire = value;}, wireCalls: () => wireCalls};
}

test("read-only approval/start needs its compiled marker and only current approval acknowledges parser support", async () => {
  for (const action of ["approve", "start"]) {
    const f = fixture();
    Object.assign(f.context.order.manifest, {actions: ["browser.read"]});
    Object.assign(f.context.order.manifest.general_browser_verification, {allow_type: false, fill_values: {}});
    await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, ["capabilities"]);
    assert.match(f.errors.at(-1)!, /Update the Mac app/);
    f.setWire({browser_cuai_text_v1: true, browser_cuai_read_only_v1: true});
    await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.equal(f.calls.length, 3);
    if (action === "approve") assert.deepEqual(f.bodies.at(-1), {review_digest: "saved", read_only_parser_supported: true});
  }
});

test("actual new-profile approval/start/continue/handoff refuse old or unknown native wire support", async () => {
  for (const action of ["approve", "start", "continue", "handoff"]) for (const marker of [null, {},
    {available: true}, {browser_cuai_text_v1: false}, {browser_cuai_text_v1: "true"}]) {
    const f = fixture(); f.setWire(marker);
    await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, ["capabilities"]); assert.equal(f.wireCalls(), 1);
    assert.equal(f.statuses.at(-1), "unavailable"); assert.match(f.errors.at(-1)!, /Update the Mac app/);
  }
});

test("late local wire response cannot authorize a retired new-profile approval or native effect", async () => {
  for (const action of ["approve", "start", "continue", "handoff"]) {
    const f = fixture(); f.context.browserProfileWireAvailable = async () => {f.context.serial.current++; return true;};
    await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, ["capabilities"]);
  }
});

test("actual fresh-review renewal refuses unavailable or retired native wire, while legacy renewal stays separate", async () => {
  for (const marker of [null, {}, {browser_cuai_text_v1: false}, {browser_cuai_text_v1: "true"}, {browser_cuai_text_v1: true}]) {
    const f = fixture(); const opened: string[] = []; f.setWire(marker);
    Object.assign(f.context, {router: {push: (path: string) => opened.push(path)},
      api: async () => {f.calls.push("renew"); return {ok: true, data: {review_path: "/v/ven_a/device-work-orders/dwo_next"}};}});
    await actual("renew", f.context)();
    assert.deepEqual(f.calls, marker?.browser_cuai_text_v1 === true ? ["renew"] : []);
    assert.deepEqual(opened, marker?.browser_cuai_text_v1 === true ? ["/v/ven_a/device-work-orders/dwo_next"] : []);
  }
  const retired = fixture(); retired.context.browserProfileWireAvailable = async () => {retired.context.serial.current++; return true;};
  await actual("renew", retired.context)(); assert.deepEqual(retired.calls, []);
  const lateError = fixture(); lateError.context.api = async () => {lateError.context.serial.current++; throw new Error("retired renewal transport");};
  await actual("renew", lateError.context)(); assert.ok(lateError.errors.every(error => !error));
  const legacy = fixture(capabilities, null); legacy.setWire(null);
  Object.assign(legacy.context, {router: {push: () => {}}, api: async () => {legacy.calls.push("renew"); return {ok: true, data: {review_path: "/v/ven_a/device-work-orders/dwo_next"}};}});
  await actual("renew", legacy.context)(); assert.deepEqual(legacy.calls, ["renew"]); assert.equal(legacy.wireCalls(), 0);
});

test("legacy profile and cancellation/reconciliation do not require the new parser marker", async () => {
  for (const action of ["approve", "start", "continue", "handoff"]) {
    const f = fixture(capabilities, null); f.setWire(null);
    await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, ["capabilities", action]); assert.equal(f.wireCalls(), 0);
  }
  for (const action of ["cancel", "reconcile"]) {
    const f = fixture(); f.setWire(null);
    await actual("nativeAction", f.context)(action);
    assert.deepEqual(f.calls, [action]); assert.equal(f.wireCalls(), 0);
  }
});

test("actual approve/start/continue/handoff refuse legacy, malformed and mismatched fresh profiles before effects", async () => {
  for (const action of ["approve", "start", "continue", "handoff"]) {
    for (const fresh of [{ok: true, data: {available: true, general_browser_execution_v1: true}},
      {...capabilities, data: {...capabilities.data, execution_profile_digest: "b".repeat(64)}}]) {
      const f = fixture(fresh); await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
      assert.deepEqual(f.calls, ["capabilities"]); assert.match(f.errors.at(-1)!, /No other model is substituted/);
    }
    const f = fixture(capabilities, {...profile(), backend: "unknown"});
    await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, ["capabilities"]);
  }
});

test("actual exact-profile actions proceed once and retired async digest checks cannot dispatch", async () => {
  for (const action of ["approve", "start", "continue", "handoff"]) {
    const f = fixture(); await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, ["capabilities", action]);
    const retired = fixture(); retired.context.api = async () => {retired.calls.push("capabilities"); queueMicrotask(() => retired.context.serial.current++); return capabilities;};
    await actual(action === "approve" ? "act" : "nativeAction", retired.context)(action);
    assert.deepEqual(retired.calls, ["capabilities"]); assert.deepEqual(retired.statuses, []);
  }
});

test("new-profile cancellation and reconciliation do not need provider configuration", async () => {
  for (const action of ["cancel", "reconcile"]) {
    const f = fixture({ok: false}); f.context.generalAvailability = "unavailable";
    await actual("nativeAction", f.context)(action); assert.deepEqual(f.calls, [action]);
  }
});

test("actual load binds capabilities to the saved profile and ignores retired digest completion", async () => {
  for (const retire of [false, true]) {
    const f = fixture(); let read = 0;
    Object.assign(f.context, {setNow: () => {}, setSourceReceipt: () => {}, setPreflight: () => {}, ensureSession: async () => {},
      isDesktopShell: () => false,
      api: async () => {read++; if (read === 1) return {ok: true, data: {id: "dwo_a", venture_id: "ven_a", review_digest: "a".repeat(64), manifest: f.context.order.manifest}};
        if (retire) queueMicrotask(() => f.context.serial.current++); return capabilities;}});
    await actual("load", f.context)();
    assert.deepEqual(f.statuses, retire ? ["unknown"] : ["unknown", "available"]);
  }
});

test("actual saved API review read remains visible while local wire support grants nothing", async () => {
  const f = fixture(); let read = 0; const orders: unknown[] = []; f.setWire(null);
  Object.assign(f.context, {setNow: () => {}, setSourceReceipt: () => {}, setPreflight: () => {}, ensureSession: async () => {},
    setOrder: (order: unknown) => orders.push(order), isDesktopShell: () => false,
    api: async () => ++read === 1 ? {ok: true, data: {id: "dwo_a", venture_id: "ven_a", review_digest: "a".repeat(64), manifest: f.context.order.manifest}} : capabilities});
  await actual("load", f.context)();
  assert.equal(orders.length, 2); assert.deepEqual(f.statuses, ["unknown", "unavailable"]);
  assert.match(f.errors.at(-1)!, /Update the Mac app/); assert.equal(f.wireCalls(), 1);
});

const require = createRequire(import.meta.url);
const noticeExports: Record<string, unknown> = {};
const noticeSource = fs.readFileSync(new URL("../components/primitives/ErrorNotice.tsx", import.meta.url), "utf8");
vm.runInNewContext(ts.transpileModule(noticeSource, {compilerOptions: {jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS}}).outputText,
  {exports: noticeExports, require: (id: string) => id === "@/lib/userError" ? {reportUserError: () => {}} : id === "@/lib/credits" ? {USAGE_LIMIT_MESSAGE: "limit"}
    : id.endsWith("/LocalModeBoundary") ? {useLocalMode: () => false} : require(id)});
const componentSource = fs.readFileSync(new URL("../components/desktop/BrowserExecutionProfileReview.tsx", import.meta.url), "utf8");
const exports: Record<string, unknown> = {};
vm.runInNewContext(ts.transpileModule(componentSource, {compilerOptions: {jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS}}).outputText,
  {exports, require: (id: string) => id === "@/lib/browserExecutionProfile" ? {parseBrowserExecutionProfile}
    : id === "@/components/primitives/ErrorNotice" ? noticeExports
    : id === "../../lib/localMode" ? require("./localMode.ts") : require(id)});
const Review = exports.BrowserExecutionProfileReview as (props: {value: unknown; qualificationGrant?: unknown; trialBinding?: unknown}) => ReturnType<typeof createElement>;
test("real profile renderer exposes saved roles, limits, data and review boundary without defaults", () => {
  const html = renderToStaticMarkup(createElement(Review, {value: profile()}));
  for (const text of ["Planner", "Actor", "Verifier", "gpt-6-sol", "default tier", "reasoning none", "700 output tokens", "600 output tokens", "300 output tokens", "64 attempts", "65536 bytes", "retained history", "private information", "Pricing table digest", "a".repeat(64), "do not verify the whole task"]) assert.ok(html.includes(text), text);
  assert.ok(pageSource.indexOf("<BrowserExecutionProfileReview") < pageSource.indexOf("I reviewed this task"));
  const bad = renderToStaticMarkup(createElement(Review, {value: {...profile(), roles: {...profile().roles, actor: {...role(1), model: "<script>"}}}}));
  assert.match(bad, /unsupported or incomplete/); assert.doesNotMatch(bad, /<script>|gpt-6-sol/);
  assert.match(renderToStaticMarkup(createElement(Review, {value: null})), /legacy decision profile/);
});

test("real review displays the saved candidate actor effort without changing planner or verifier", () => {
  const value = profile(); value.roles.actor = {...value.roles.actor, model: "gpt-6.1-sol", reasoning: "low"};
  const html = renderToStaticMarkup(createElement(Review, {value}));
  assert.match(html, /gpt-6\.1-sol.*reasoning low.*600 output tokens/);
  assert.equal((html.match(/reasoning none/g) || []).length, 2);
  assert.match(html, /64 attempts/);
});


test("actual qualification approval/start requires the new compiled marker before any dispatch", async () => {
  for (const action of ["approve", "start"]) {
    const f=fixture(); Object.assign(f.context.order,{browser_qualification_trial_binding:{trial_id:"bqt_one"}});
    await actual(action==="approve"?"act":"nativeAction",f.context)(action);
    assert.deepEqual(f.calls,["capabilities"]); assert.match(f.errors.at(-1)!,/Update the Mac app/);
    f.setWire({browser_cuai_text_v1:true,browser_qualification_v1:true});
    await actual(action==="approve"?"act":"nativeAction",f.context)(action);
    assert.equal(f.calls.length,3);
    if(action==="approve") assert.deepEqual(f.bodies.at(-1),{review_digest:"saved",browser_qualification_parser_supported:true});
    const retired=fixture(); Object.assign(retired.context.order,{browser_qualification_grant:{grant_digest:"a".repeat(64)}});
    retired.context.browserProfileWireAvailable=async()=>{retired.context.serial.current++;return true;};
    await actual(action==="approve"?"act":"nativeAction",retired.context)(action);
    assert.deepEqual(retired.calls,["capabilities"]);
  }
});

const qualificationFixture = {"id":"dwo_one","actor_id":"usr_synthetic","device_id":"dev_synthetic","venture_id":"ven_one","workspace_id":"ws_one","review_digest":"3333333333333333333333333333333333333333333333333333333333333333","state":"approved","browser_qualification_grant":{"schema_version":1,"actor_id":"usr_synthetic","device_id":"dev_synthetic","workspace_id":"ws_one","venture_id":"ven_one","trial_id":"bqt_one","catalog_digest":"1111111111111111111111111111111111111111111111111111111111111111","task_id":"form_valid","seed_digest":"2222222222222222222222222222222222222222222222222222222222222222","order_id":"dwo_one","review_digest":"3333333333333333333333333333333333333333333333333333333333333333","profile_digest":"83fc3faf8461bc761f0cbc3a514629de88afa8573c2b0c7010fc76bcf99bc501","expires_at":2000000000,"nonce":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","origin":"https://api.supymem.com","rules":[{"rule_id":"read_form","method":"GET","path":"/public/browser-qualification/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/form","query":"","document_path":"/public/browser-qualification/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/form","document_id":"document-form","form_id":"","control_id":"","fields":[],"max_requests":4,"max_commits":0,"prerequisites":[],"rejected_retry_limit":0,"body_kind":"form"},{"rule_id":"submit_form","method":"POST","path":"/public/browser-qualification/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/form","query":"","document_path":"/public/browser-qualification/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/form","document_id":"document-form","form_id":"form-project","control_id":"form-submit","fields":[{"name":"project","value":"Atlas","control_id":"project","kind":"text","input_value":"Atlas","input_type":"text"},{"name":"email","value":"qa@example.invalid","control_id":"email","kind":"email","input_value":"qa@example.invalid","input_type":"text"},{"name":"code","value":"ABC123","control_id":"code","kind":"synthetic_code","input_value":"ABC123","input_type":"text"}],"max_requests":1,"max_commits":1,"prerequisites":["read_form"],"rejected_retry_limit":0,"body_kind":"form"}],"allowed_operations":["navigate","type","click","extract"],"max_tabs":1,"frames":[],"downloads":[],"grant_digest":"5760753356c9d2e3850e2da87bc1271759ba5ea7f9fe763d060c3a2a48fbb262"},"browser_qualification_trial_binding":{"schema_version":1,"actor_id":"usr_synthetic","device_id":"dev_synthetic","workspace_id":"ws_one","venture_id":"ven_one","trial_id":"bqt_one","catalog_digest":"1111111111111111111111111111111111111111111111111111111111111111","task_id":"form_valid","seed_digest":"2222222222222222222222222222222222222222222222222222222222222222","order_id":"dwo_one","review_digest":"3333333333333333333333333333333333333333333333333333333333333333","profile_digest":"83fc3faf8461bc761f0cbc3a514629de88afa8573c2b0c7010fc76bcf99bc501","expires_at":2000000000,"binding_digest":"a6dd18d2046a63f2c6cd073427ae68ff3658c4cb4ca77f2b701bf9dd3de02bc1"},"manifest":{"general_browser_verification":{"execution_profile":{"backend":"cuai","completion_policy":"browser_semantic_review_v1","max_provider_calls":64,"max_request_bytes":65536,"policy_version":1,"pricing_digest":"deb0f50f682218f1462632140b85e7d1764c72ab760a1dda51c880d81b286e90","roles":{"actor":{"max_output_tokens":700,"model":"gpt-5.6-luna","provider":"openai","reasoning":"none","service_tier":"default"},"plan":{"max_output_tokens":700,"model":"gpt-5.6-luna","provider":"openai","reasoning":"none","service_tier":"default"},"verify":{"max_output_tokens":700,"model":"gpt-5.6-luna","provider":"openai","reasoning":"none","service_tier":"default"}},"version":"browser_cuai_text_v1"}}}};
test("actual qualification review shows exact effects, synthetic inputs and aggregate budget honestly", () => {
  const html=renderToStaticMarkup(createElement(Review,{value:qualificationFixture.manifest.general_browser_verification.execution_profile,
    qualificationGrant:qualificationFixture.browser_qualification_grant,trialBinding:qualificationFixture.browser_qualification_trial_binding}));
  for(const text of ["Qualification task and effects","bqt_one","form_valid","shared trial ceiling is $10","known and uncertain paid attempts","POST", "up to 1 requests and 1 mutations","Atlas","qa@example.invalid","ABC123","cannot be replayed","do not prove the task succeeded"]){assert.ok(html.includes(text),text);}
  assert.doesNotMatch(html,/task verified|acceptance verified|qualification passed/i);
  const budget=renderToStaticMarkup(createElement(Review,{value:profile(),trialBinding:qualificationFixture.browser_qualification_trial_binding}));
  assert.match(budget,/trial budget membership only/);assert.match(budget,/grants no fixture, form input, download, tab or network exception/);assert.doesNotMatch(budget,/POST|ABC123/);
  for(const bad of [{}, {...qualificationFixture.browser_qualification_grant,owned:true},
    {...qualificationFixture.browser_qualification_grant,origin:"https://foreign.invalid"},
    {...qualificationFixture.browser_qualification_grant,grant_digest:"invalid"}]){
    const refused=renderToStaticMarkup(createElement(Review,{value:profile(),qualificationGrant:bad}));
    assert.match(refused,/saved qualification scope is incomplete/);assert.doesNotMatch(refused,/exact input/);
  }
});
