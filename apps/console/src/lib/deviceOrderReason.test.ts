import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { deviceOrderStatus } from "./deviceOrderStatus.ts";
import { isNativeDesktopManifest, desktopExecutionMessage } from "./desktopExecutionAvailability.ts";
import { generalBrowserExecutionMessage } from "./generalBrowserExecutionAvailability.ts";
import { isVerifiedMacTask, macResultHeadline } from "./macResult.ts";
import { formatMicroCreditUsage } from "./credits.ts";
import { browserReadOnlyAuthority } from "./browserExecutionProfile.ts";
import { codingToolLabel, productBuildStatus, safeChangedFiles, safeLoopText, loopIterationLabel, type LoopIteration } from "./loopProgress.ts";
import { isLocalConsole } from "./localMode.ts";
import { errorCodeCopy } from "./errorCopy.ts";
import { mergeProductOrderDetails, parseProductBuildCaps, parseProductBuildLoop, prepareProductBuildCaps, productBuildActorMatches } from "./productBuildCaps.ts";

const source = fs.readFileSync(new URL("../app/v/[ventureId]/device-work-orders/[orderId]/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("review.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const body = tree.statements.filter(node => !ts.isImportDeclaration(node)
  && !(ts.isFunctionDeclaration(node) && node.name?.text === "DeviceOrderPage")).map(node => node.getText(tree)).join("\n");
const readUrlsSource = fs.readFileSync(new URL("../components/desktop/BrowserReadUrlsReview.tsx", import.meta.url), "utf8");
const readUrlsTree = ts.createSourceFile("read-urls.tsx", readUrlsSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const readUrlsBody = readUrlsTree.statements.filter(node => !ts.isImportDeclaration(node))
  .map(node => node.getText(readUrlsTree).replace(/^export\s+/, "")).join("\n");
const productBodies = ["ProductBuildReview", "LoopProgress", "ProductBuildCapsEditor"].map(name => {
  const source = fs.readFileSync(new URL(`../components/desktop/${name}.tsx`, import.meta.url), "utf8");
  const parsed = ts.createSourceFile(`${name}.tsx`, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  return parsed.statements.filter(node => !ts.isImportDeclaration(node))
    .map(node => node.getText(parsed).replace(/^export\s+/, "")).join("\n");
}).join("\n");
const code = ts.transpileModule(`${readUrlsBody};${productBodies};${body};globalThis.render=Review;`, {
  compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
}).outputText;

function render(receipt: Record<string, unknown>, readOnly = false, readUrls: string[] = [], productPhase?: "plan" | "implement",
  orderOverrides: {state?: string; claimed_at?: string | null; expires_at?: string} = {}, iterations: LoopIteration[] = [],
  buttonRows: Array<{label: string; className?: string; disabled: boolean}> = []) {
  const order = {id: "dwo_owned", venture_id: "ven_owned", state: "stopped", claimed_at: "2026-10-05T00:00:00Z",
    review_digest: "a".repeat(64), expires_at: "2026-10-06T00:00:00Z", manifest: {task_id: "task_owned",
      title: "Owned browser check", instruction: "Read the reviewed website", applications: ["com.google.Chrome"],
      origins: ["https://fixture.invalid"], actions: ["browser.read"], credit_ceiling_micro: 500000, duration_seconds: 900,
      ...(receipt.tool === "general_browser" ? {general_browser_verification: {operation: "general_goal_v1",
        start_url: "https://fixture.invalid", max_steps: 32,
        ...(readUrls.length ? {read_urls: readUrls} : {}),
        ...(readOnly ? {allow_type: false, fill_values: {}, execution_profile: JSON.parse(fs.readFileSync(
          new URL("../../../../config/browser_cuai_profile.json", import.meta.url), "utf8"))} : {})}} : {}),
      ...(productPhase ? {coding_loop_verification: {provider: "codex", workspace_slug: "dinner",
        profile: "web_app_vercel_v1", phase: productPhase, max_iterations: 2,
        max_wall_seconds: 1440, per_call_seconds: 720, max_turns: null}} : {})}, ...orderOverrides};
  if (productPhase) Object.assign(order.manifest, {
    task_id: "product_build_ven_owned", title: "Plan the reviewed product", instruction: "Plan only this venture's product.",
    applications: ["com.companyos.app"], origins: [], actions: ["editor.edit", "terminal.execute"],
    credit_ceiling_micro: 0, duration_seconds: 1440, coding_loop_verification: {
      operation: "workspace_loop_v1", provider: "codex", workspace_slug: "dinner", profile: "web_app_vercel_v1",
      check_recipe: "web_app_vercel_checks_v1", phase: productPhase, network: "none",
      read_scope: "venture_workspace", write_scope: "venture_workspace", max_iterations: 2,
      max_wall_seconds: 1440, per_call_seconds: 720, max_turns: null,
      plan_order_id: productPhase === "implement" ? "dwo_Plan" : null,
      plan_digest: productPhase === "implement" ? "f".repeat(64) : null,
    },
  });
  const states = [order, "unavailable", "available", "", false, false, false, Date.now(), receipt, null, null, null, null,
    false, undefined, null, false, iterations, null];
  let index = 0, calls = 0;
  const context = vm.createContext({
    deviceOrderStatus, isNativeDesktopManifest, desktopExecutionMessage, generalBrowserExecutionMessage,
    isVerifiedMacTask, macResultHeadline, formatMicroCreditUsage, browserReadOnlyAuthority, safeLoopText, loopIterationLabel, errorCodeCopy,
    codingToolLabel, productBuildStatus, safeChangedFiles, isLocalConsole,
    parseProductBuildCaps, parseProductBuildLoop, mergeProductOrderDetails, prepareProductBuildCaps, productBuildActorMatches,
    useState: (initial: unknown) => [index < states.length ? states[index++] : initial, () => {}], useRef: (value: unknown) => ({current: value}),
    useEffect: () => {}, useFeatureVisibility: () => () => true,
    useRouter: () => ({push: () => {calls++;}}), isDesktopShell: () => readOnly || Boolean(productPhase),
    api: () => {calls++; throw new Error("Render must not dispatch");},
    styles: {primary: "primary"}, Link: "a", ErrorNotice: (props: any) => ({type: "error-notice", props,
      children: props.children?.length ? props.children : [props.error]}), BrowserWatchPanel: "browser-watch",
    BrowserExecutionProfileReview: "profile-review",
    React: {Fragment: "fragment", createElement: (type: unknown, props: unknown, ...children: unknown[]) => typeof type === "function"
      ? type({...((props as object) || {}), children}) : ({type, props, children})},
  });
  vm.runInContext(code, context);
  const result = context.render({ventureId: "ven_owned", orderId: "dwo_owned"});
  function text(node: any): string {
    if (typeof node === "string" || typeof node === "number") return String(node);
    return Array.isArray(node) ? node.map(text).join(" ") : node?.children?.map(text).join(" ") || "";
  }
  assert.equal(calls, 0);
  function visit(node: any) {
    if (Array.isArray(node)) { node.forEach(visit); return; }
    if (node?.type === "button") buttonRows.push({label: text(node), className: node.props.className,
      disabled: Boolean(node.props.disabled)});
    node?.children?.forEach(visit);
  }
  visit(result);
  if (readOnly) {
    assert.equal(browserReadOnlyAuthority(order.manifest), true);
    const buttons = buttonRows.map(row => row.label);
    assert.ok(buttons.includes("Stop"));
    assert.ok(!buttons.some(label => /Continue|Open Supymem browser/.test(label)));
  }
  return text(result);
}

test("an expired stale approval cannot renew or restart a claimed product execution", () => {
  const stale = {state: "approved", claimed_at: undefined};
  for (const receipt of [
    {state: "running_coding", execution_id: "dex_owned"},
    {state: "not_started", execution_id: "dex_owned"},
    {state: "stopped", task_verified: true, credits_reconciled: true, evidence: {execution_id: "dex_owned"}},
  ]) {
    const rendered = render({tool: "coding", ...receipt}, false, [], "implement", stale,
      [{index: 0, phase: "code", outcome: "pass"}]);
    assert.doesNotMatch(rendered, /Prepare a fresh request|Start approved coding run|This review expired/);
    if (receipt.state === "stopped") {
      assert.match(rendered, /independently verified/);
      assert.match(rendered, /Review a Vercel preview deployment/);
    }
  }
  assert.match(render({state: "not_started", tool: "coding"}, false, [], "implement", stale), /Prepare a fresh request/);
});

const review = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "Review") as ts.FunctionDeclaration;
const lifecycle = review.body!.statements.find(node => ts.isFunctionDeclaration(node)
  && node.name?.text === "refreshOrderLifecycle")!;
const lifecycleCode = ts.transpileModule(`${lifecycle.getText(tree)};globalThis.refresh=refreshOrderLifecycle;`, {
  compilerOptions: {target: ts.ScriptTarget.ES2022},
}).outputText;
const load = review.body!.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "load")!;
const receipts = review.body!.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "refreshProductReceipts")!;
const loadCode = ts.transpileModule(`${load.getText(tree)};${receipts.getText(tree)};
  globalThis.loadReview=load;globalThis.refreshReceipts=refreshProductReceipts;`, {
  compilerOptions: {target: ts.ScriptTarget.ES2022},
}).outputText;
function productOrder() {
  return { id: "dwo_owned", workspace_id: "ws_owned", venture_id: "ven_owned", actor_id: "usr_owned", device_id: "dev_owned",
    review_digest: "a".repeat(64), expires_at: "2999-01-01T00:00:00Z", state: "pending", claimed_at: null,
    task_snapshot: { kind: "product_build", revision_digest: "f".repeat(64), confirmed_idea: { synthetic: true },
      product_scope: { workspace_slug: "synthetic-page" } },
    manifest: { task_id: "product_build_ven_owned", title: "Plan the reviewed product", instruction: "Plan only this venture's product.",
      applications: ["com.companyos.app"], origins: [], actions: ["editor.edit", "terminal.execute"],
      credit_ceiling_micro: 0, duration_seconds: 300, coding_loop_verification: {
        operation: "workspace_loop_v1", provider: "codex", workspace_slug: "synthetic-page", profile: "web_app_vercel_v1",
        check_recipe: "web_app_vercel_checks_v1", phase: "plan", network: "none",
        read_scope: "venture_workspace", write_scope: "venture_workspace", max_iterations: 1,
        max_wall_seconds: 300, per_call_seconds: 300, max_turns: null, plan_order_id: null, plan_digest: null,
      } } };
}
function nativeProductOrder(full = productOrder()) {
  const { task_snapshot: _snapshot, ...native } = full;
  return native;
}
function loadFixture(full: Record<string, unknown> = productOrder(), native = nativeProductOrder()) {
  const state: Record<string, any> = {};
  const calls: string[] = [];
  const context = vm.createContext({ alive: {current: true}, serial: {current: 0}, inFlight: {current: false},
    sessionSubject: "usr_owned", sessionEpoch: 1, ventureId: native.venture_id, orderId: native.id,
    path: `/api/ventures/${native.venture_id}/device-work-orders/${native.id}`,
    ensureSession: async () => {}, verifiedSessionSubject: () => context.sessionSubject,
    verifiedSessionEpoch: () => context.sessionEpoch, isDesktopShell: () => true, isLocalConsole, featureVisible: () => true,
    desktopInvokeOrThrow: async (command: string) => {
      assert.equal(command, "inspect_device_work_order"); return native;
    }, isNativeDesktopManifest, mergeProductOrderDetails, productBuildActorMatches,
    nativeErrors: {}, apiFailure: () => "Could not load the saved review", invokeErrorMessage: (_caught: unknown, code: string) => code,
    plainErrorText: (_code: string, fallback: string) => fallback,
    api: async (path: string, init?: {method?: string}) => {
      assert.equal(init?.method ?? "GET", "GET", "loading never mutates, approves, claims or starts an order");
      calls.push(path);
      return {ok: true, status: 200, data: path.endsWith("/capabilities") ? {available: true}
        : path.endsWith("/iterations") ? {iterations: []} : path.endsWith("/plan") ? {plan: full.approved_plan ?? null} : full};
    },
  });
  for (const key of ["Busy", "Checking", "Order", "Acknowledged", "Error", "NativeReceipt", "SourceReceipt", "Preflight",
    "DesktopAvailability", "GeneralAvailability", "ProductAvailable", "ProductScope", "ProductPlan", "ProductCapsDirty",
    "LoopIterations", "DeployReceipt", "CopiedFolder", "FullActivity", "Now"]) {
    const name = key[0].toLowerCase() + key.slice(1);
    context[`set${key}`] = (value: any) => {state[name] = typeof value === "function" ? value(state[name]) : value;};
  }
  vm.runInContext(loadCode, context);
  return {context, native, state, calls};
}
test("the real load hydrates native product custody before reading plan receipts", async () => {
  const full = productOrder(), f = loadFixture(full);
  assert.equal(Object.hasOwn(f.native, "task_snapshot"), false);
  await f.context.loadReview();
  assert.equal(f.state.order.manifest, f.native.manifest);
  assert.deepEqual(f.state.order.task_snapshot, full.task_snapshot);
  assert.equal(f.state.productAvailable, true);
  assert.deepEqual(f.state.productScope, full.task_snapshot.product_scope);
  assert.equal(f.state.busy, false);
  assert.equal(f.state.error, "");
  assert.deepEqual(f.calls, [f.context.path + "/capabilities", f.context.path,
    f.context.path + "/iterations", f.context.path + "/plan"]);
});
test("the real native load and lifecycle retain Core's local actor aliases without widening custody", async () => {
  const full = {...productOrder(), actor_id: "per_local000000"};
  for (const subject of ["founder", "local", "per_local000000"]) {
    const f = loadFixture(full, nativeProductOrder(full)); f.context.sessionSubject = subject;
    await f.context.loadReview();
    assert.equal(f.state.order.actor_id, "per_local000000");
    assert.deepEqual(f.state.order.task_snapshot, full.task_snapshot);
    assert.equal(f.state.productAvailable, true);
    const stopped = {...full, state: "stopped", claimed_at: "earlier"};
    const lifecycle = lifecycleFixture(stopped, {...nativeProductOrder(full), state: "claimed", claimed_at: "earlier"});
    lifecycle.context.sessionSubject = subject;
    await lifecycle.context.refresh(lifecycle.saved, 2, {state: "stopped", order_id: full.id, credits_reconciled: true});
    assert.equal(lifecycle.current().state, "stopped");
    assert.deepEqual(lifecycle.current().task_snapshot, full.task_snapshot);
  }
  for (const subject of [null, "", "usr_other", "Founder"]) {
    const f = loadFixture(full, nativeProductOrder(full)); f.context.sessionSubject = subject;
    await f.context.loadReview();
    assert.equal(f.state.productAvailable, false);
    assert.equal(f.state.order?.task_snapshot, undefined);
    assert.equal(f.calls.length, 0);
  }
});
test("the real load fails closed on foreign or changed product metadata", async () => {
  const full = productOrder();
  for (const change of [
    ...["id", "venture_id", "workspace_id", "actor_id", "device_id", "review_digest"].map(field => ({[field]: "foreign"})),
    {manifest: {...full.manifest, instruction: "different scope"}}, {task_snapshot: null},
  ]) {
    const f = loadFixture({...full, ...change});
    await f.context.loadReview();
    assert.equal(f.state.order, f.native);
    assert.equal(f.state.productAvailable, false);
    assert.equal(f.state.productScope, undefined);
    assert.match(f.state.error, /could not be verified/);
    assert.equal(f.calls.length, 2);
  }
});
test("metadata awaited by the real load cannot cross an actor epoch or page generation", async () => {
  for (const retire of ["actor", "epoch", "generation"]) {
    const f = loadFixture();
    let resolve!: (value: unknown) => void, read!: () => void;
    const waiting = new Promise<void>(done => {read = done;});
    const originalApi = f.context.api;
    f.context.api = async (path: string) => {
      if (path !== f.context.path) return originalApi(path);
      read(); return new Promise(done => {resolve = done;});
    };
    const pending = f.context.loadReview(); await waiting;
    if (retire === "actor") f.context.sessionSubject = "usr_other";
    if (retire === "epoch") { f.context.sessionSubject = "usr_other"; f.context.sessionEpoch++;
      f.context.sessionSubject = "usr_owned"; f.context.sessionEpoch++; }
    if (retire === "generation") f.context.serial.current++;
    resolve({ok: true, data: productOrder()}); await pending;
    assert.equal(f.state.order, f.native);
    assert.equal(Object.hasOwn(f.state.order, "task_snapshot"), false);
    assert.equal(f.state.productScope, undefined);
    assert.equal(f.state.productPlan, null);
    assert.equal(f.calls.some(path => /iterations|\/plan$/.test(path)), false);
  }
});
test("product receipt awaits also retain the captured actor epoch", async () => {
  for (const title of ["Fleet maintenance scheduling", "Home gardening reminders"]) {
    const f = loadFixture();
    f.native.manifest.title = title;
    const resolvers: Array<(value: unknown) => void> = [];
    f.context.serial.current = 2;
    f.context.api = () => new Promise(done => {resolvers.push(done);});
    const pending = f.context.refreshReceipts(f.native, 2);
    f.context.sessionEpoch++;
    assert.equal(resolvers.length, 2);
    for (const resolve of resolvers) resolve({ok: true, data: {iterations: [{index: 0, phase: "plan", outcome: "pass"}]}});
    await pending;
    assert.equal(f.state.loopIterations, undefined);
    assert.equal(f.state.productPlan, undefined);
  }
});
function lifecycleFixture(response?: Record<string, unknown>, savedOverride: Record<string, unknown> = {}) {
  const saved = {id: "dwo_owned", venture_id: "ven_owned", review_digest: "a".repeat(64), actor_id: "usr_owned",
    device_id: "dev_owned", workspace_id: "ws_owned", state: "approved", manifest: {reviewed: true}, ...savedOverride};
  let current: any = saved, calls = 0;
  const context = vm.createContext({alive: {current: true}, serial: {current: 2}, lifecycleRefresh: {current: ""},
    sessionSubject: "usr_owned", sessionEpoch: 1,
    verifiedSessionSubject: () => context.sessionSubject, verifiedSessionEpoch: () => context.sessionEpoch,
    mergeProductOrderDetails, productBuildActorMatches,
    path: "/api/ventures/ven_owned/device-work-orders/dwo_owned", api: async () => {
      calls++; return {ok: true, data: response || {...saved, state: "stopped", claimed_at: "now"}};
    }, setOrder: (update: (value: unknown) => unknown) => {current = update(current);},
    setNativeReceipt: () => assert.fail("Lifecycle refresh must retain the native receipt"),
    setAcknowledged: () => assert.fail("Lifecycle refresh must retain the acknowledgment"),
    setError: () => assert.fail("Lifecycle refresh must retain the displayed result"),
  });
  vm.runInContext(lifecycleCode, context);
  return {context, saved, current: () => current, calls: () => calls};
}
test("live lifecycle polling observes lease expiry and wake without a new local receipt", async () => {
  const fixture = lifecycleFixture(undefined, {state: "claimed", claimed_at: "now"});
  let state = "claimed";
  fixture.context.api = async () => ({ok: true, data: {...fixture.saved, state}});
  const receipt = {state: "acting", order_id: fixture.saved.id, execution_id: "dex_owned"};
  await fixture.context.refresh(fixture.saved, 2, receipt);
  state = "waiting_for_device";
  await fixture.context.refresh(fixture.saved, 2, receipt);
  assert.equal(fixture.current().state, "waiting_for_device");
  state = "claimed";
  await fixture.context.refresh(fixture.saved, 2, receipt);
  assert.equal(fixture.current().state, "claimed");
});
test("the real lifecycle refresh updates only the matching order and fetches a settled receipt once", async () => {
  const f = lifecycleFixture();
  const receipt = {state: "stopped", order_id: f.saved.id, evidence: {execution_id: "dex_owned"}};
  await f.context.refresh(f.saved, 2, receipt); await f.context.refresh(f.saved, 2, receipt);
  assert.equal(f.current().state, "stopped"); assert.equal(f.current().claimed_at, "now");
  assert.equal(f.current().manifest, f.saved.manifest); assert.equal(f.calls(), 1);
  f.context.api = async () => ({ok: true, data: {...f.saved, state: "claimed", claimed_at: "now"}});
  await f.context.refresh(f.saved, 2, {...receipt, state: "running_coding"});
  assert.equal(f.current().state, "stopped", "a late claim response cannot regress a settled order");
});
test("a local stop refreshes Core again when its stop receipt is acknowledged", async () => {
  const f = lifecycleFixture();
  let calls = 0;
  f.context.api = async () => {
    calls++; return {ok: true, data: {...f.saved, state: "claimed", claimed_at: "now"}};
  };
  const stopped = {state: "stopped", order_id: f.saved.id, execution_id: "dex_owned",
    task_verified: true, credits_reconciled: false};
  await f.context.refresh(f.saved, 2, stopped);
  assert.equal(f.current().state, "claimed");
  f.context.api = async () => {
    calls++; return {ok: true, data: {...f.saved, state: "stopped", claimed_at: "now"}};
  };
  const acknowledged = {...stopped, credits_reconciled: true};
  await f.context.refresh(f.saved, 2, acknowledged);
  assert.equal(f.current().state, "stopped");
  const settled = f.current();
  await f.context.refresh(f.saved, 2, acknowledged);
  assert.equal(f.current(), settled, "unchanged acknowledgment is memoized");
  assert.equal(calls, 2, "only the stop and its acknowledgment trigger reads");
});

test("lifecycle refresh rejects another order, review, owner or device and ignores old generations", async () => {
  for (const field of ["id", "venture_id", "review_digest", "actor_id", "device_id", "workspace_id"]) {
    const f = lifecycleFixture();
    f.context.api = async () => ({ok: true, data: {...f.saved, [field]: "other", state: "stopped", claimed_at: "now"}});
    await f.context.refresh(f.saved, 2, {state: "stopped", order_id: f.saved.id});
    assert.equal(f.current(), f.saved, field);
  }
  const f = lifecycleFixture();
  await f.context.refresh(f.saved, 1, {state: "stopped", order_id: f.saved.id});
  await f.context.refresh(f.saved, 2, {state: "stopped", order_id: "dwo_other"});
  assert.equal(f.calls(), 0);
  let resolve!: (value: unknown) => void;
  f.context.api = () => new Promise(done => {resolve = done;});
  const pending = f.context.refresh(f.saved, 2, {state: "stopped", order_id: f.saved.id});
  f.context.serial.current = 3;
  resolve({ok: true, data: {...f.saved, state: "stopped", claimed_at: "now"}}); await pending;
  assert.equal(f.current(), f.saved);
});

test("the real product lifecycle refresh hydrates a closed plan for explicit-cap preparation", async () => {
  const full = productOrder(), native = {...nativeProductOrder(full), state: "claimed", claimed_at: "earlier"};
  const closed = {...full, state: "stopped", claimed_at: "earlier", closed_at: "now", execution_id: "dex_finished",
    native_stop: {processes_stopped: true, task_verified: true, verification_kind: "workspace_loop_v1"},
    approved_plan: {plan_digest: "c".repeat(64)}, stop_reconciliation_required: false, inference_reconciliation_required: false};
  const f = lifecycleFixture(closed, native);
  await f.context.refresh(f.saved, 2, {state: "stopped", order_id: full.id, execution_id: "dex_finished", credits_reconciled: true});
  assert.equal(f.current().manifest, f.saved.manifest);
  for (const key of ["task_snapshot", "native_stop", "approved_plan", "closed_at", "execution_id"])
    assert.deepEqual(f.current()[key], closed[key as keyof typeof closed]);
  const caps = {max_iterations: 4, max_wall_seconds: 600, per_call_seconds: 300, max_turns: null};
  const next = {...full, id: "dwo_NewRun", review_digest: "b".repeat(64), manifest: {...full.manifest, duration_seconds: 600,
    coding_loop_verification: {...full.manifest.coding_loop_verification, ...caps, phase: "implement",
      plan_order_id: full.id, plan_digest: closed.approved_plan.plan_digest}},
    task_snapshot: {...full.task_snapshot, approved_plan: closed.approved_plan, plan_order_id: full.id}};
  const calls: string[] = [];
  const prepared = await prepareProductBuildCaps({order: f.current(), caps, phase: "implement", parserSupported: true,
    current: () => true, api: async (path, init) => {
      calls.push(path); assert.deepEqual(JSON.parse(String(init.body)), {caps}); return {ok: true, status: 200, data: next};
    }});
  assert.equal(prepared.ok, true);
  assert.deepEqual(calls, [`/api/ventures/ven_owned/device-work-orders/${full.id}/prepare-run`]);
  f.context.api = async () => ({ok: true, data: {...full, state: "claimed", claimed_at: "earlier"}});
  const settled = f.current();
  await f.context.refresh(f.saved, 2, {state: "running_coding", order_id: full.id});
  assert.equal(f.current(), settled, "a stale claim cannot strip closed-plan receipts");
});

test("real product polling rejects a changed snapshot and an actor epoch after its await", async () => {
  const full = productOrder();
  const changed = {...full, state: "claimed", claimed_at: "now", task_snapshot: {...full.task_snapshot, revision_digest: "e".repeat(64)}};
  const f = lifecycleFixture(changed, full);
  await f.context.refresh(f.saved, 2, {state: "running_coding", order_id: full.id});
  assert.equal(f.current(), f.saved);
  let resolve!: (value: unknown) => void;
  f.context.api = () => new Promise(done => {resolve = done;});
  const pending = f.context.refresh(f.saved, 2, {state: "stopped", order_id: full.id, credits_reconciled: true});
  f.context.sessionEpoch++;
  resolve({ok: true, data: {...full, state: "stopped", claimed_at: "now"}}); await pending;
  assert.equal(f.current(), f.saved);
});

test("a real pending native plan renders the actual caps editor without dispatch", () => {
  const text = render({tool: "coding", state: "not_started", task_verified: false}, false, [], "plan",
    {state: "pending", claimed_at: null, expires_at: "2999-01-01T00:00:00Z"});
  assert.match(text, /Set build plan limits/);
  assert.match(text, /Number of tries/);
  assert.match(text, /Prepare review with these limits/);
});

test("an expired plan makes its fresh review the only primary action before approval or Start", () => {
  for (const state of ["pending", "approved"]) {
    const buttons: Array<{label: string; className?: string; disabled: boolean}> = [];
    render({tool: "coding", state: "not_started"}, false, [], "plan", {state, claimed_at: null}, [], buttons);
    assert.deepEqual(buttons.filter(button => button.className === "primary").map(button => button.label.trim()),
      ["Prepare review with these limits"]);
    const obsolete = buttons.find(button => button.label === (state === "pending"
      ? "Approve build plan task" : "Create my build plan"));
    assert.equal(obsolete?.disabled, true);
    assert.equal(obsolete?.className, undefined);
  }
});

test("a product stop before its first try explains the known failure and keeps source custody separate from verification", () => {
  for (const phase of ["plan", "implement"] as const) {
    const rendered = render({tool: "coding", state: "stopped", task_verified: false, credits_reconciled: true,
      failure_code: "coding_git_scope_invalid", source_receipt: {sha256: "a".repeat(64), qa_verified: false}}, false, [], phase);
    assert.match(rendered, /could not verify this venture's local Git workspace/);
    assert.match(rendered, /source snapshot alone does not verify a working app/);
    assert.match(rendered, /new coding review/);
    assert.doesNotMatch(rendered, /coding_git_scope_invalid|Saved tries|independently verified/);
    assert.doesNotMatch(rendered, /Approve this plan and review coding limits|Review a Vercel preview deployment|Send saved source for QA/);
  }
});

test("a stopped product displays safe fallback copy instead of arbitrary failure diagnostics", () => {
  for (const failure_code of ["coding_unknown_failure", "Error: /Users/founder/private token=private", "private-exception-marker"]) {
    const rendered = render({tool: "coding", state: "stopped", task_verified: false, credits_reconciled: true,
      failure_code}, false, [], "plan");
    assert.match(rendered, /stopped without a verified result/);
    assert.ok(!rendered.includes(failure_code));
    assert.doesNotMatch(rendered, /\/Users\/|token=|private-exception/);
  }
});

test("browser decision stop is visible without desktop evidence and preserves uncertain outcome guidance", () => {
  for (const tool of ["browser", "general_browser"]) {
    const text = render({tool, state: "stopped", task_verified: false, credits_reconciled: false,
      reason_code: "cu_browser_decision_unavailable"});
    assert.match(text, /browser task stopped because its next step could not be verified/);
    assert.match(text, /Review its saved result/);
    assert.match(text, /outcome remains unverified/);
    assert.doesNotMatch(text, /try again|refunded|nothing was done/i);
    assert.equal(text.match(/browser task stopped because its next step could not be verified/g)?.length, 1);
  }
});

test("existing closed desktop reason remains visible once, while arbitrary reason text is not displayed", () => {
  const known = render({tool: "desktop", state: "stopped", task_verified: false, credits_reconciled: false,
    reason_code: "cu_stopped", evidence: {}});
  assert.equal(known.match(/The Mac task stopped\./g)?.length, 1);
  const unknown = render({tool: "browser", state: "stopped", task_verified: false,
    reason_code: "private-exception-marker"});
  assert.doesNotMatch(unknown, /private-exception-marker/);
});

test("actual read-only protected-input render offers Stop without manual input or continue", () => {
  const text = render({tool: "general_browser", state: "needs_you", task_verified: false,
    credits_reconciled: false, pause_kind: "sign_in", handoff: "hidden"}, true);
  assert.match(text, /This read-only task reached protected input/);
  assert.match(text, /No input access is permitted\. Stop this task/);
  assert.doesNotMatch(text, /Continue|Open Supymem browser/);
});

test("actual approval review renders the real exact-page component without dispatch", () => {
  const urls = ["https://pypi.org/project/requests/", "https://pypi.org/project/httpx/",
    "https://pypi.org/project/aiohttp/", "https://github.com/psf/requests",
    "https://github.com/encode/httpx", "https://github.com/aio-libs/aiohttp"];
  const text = render({tool: "general_browser", state: "needs_you", task_verified: false}, true, urls);
  assert.match(text, /Exact pages Supymem may read/);
  for (const url of urls) assert.ok(text.includes(url), url);
  assert.match(text, /No input access is granted/);
});


test("actual browser review renders the complete saved answer as requiring human review", () => {
  const answer = "A practical map of our changing shoreline";
  for (const state of ["stopped", "reconciliation_required", "interrupted_reconciliation_required"]) {
    const text = render({tool: "general_browser", state, task_verified: false,
      credits_reconciled: false, semantic_satisfied: true, result_summary: answer});
    assert.match(text, /Reported browser result/);
    assert.equal(text.split(answer).length - 1, 1);
    assert.match(text, /Compare it with the website; the task remains unverified/);
    assert.doesNotMatch(text, /independently verified|refunded|nothing was done/i);
  }
});


test("browser reported answer stays separate from unknown credits and a closed failure reason", () => {
  const answer = "The saved public headline";
  const text = render({tool: "general_browser", state: "stopped", task_verified: true,
    credits_reconciled: false, effect_outcome_unconfirmed: true, result_summary: answer,
    reason_code: "cu_browser_decision_unavailable", failure_code: "private-exception-marker"});
  assert.match(text, /The saved public headline/);
  assert.match(text, /browser task stopped because its next step could not be verified/);
  assert.match(text, /outcome remains unverified/);
  assert.match(text, /the task remains unverified/);
  assert.doesNotMatch(text, /private-exception-marker|independently verified|Credits reconciled|refunded/i);
});

test("unqualified summaries cannot be shown as a browser result", () => {
  for (const result_summary of [undefined, null, 42, {}, "", " \n", "x".repeat(501)]) {
    const text = render({tool: "general_browser", state: "stopped", task_verified: false, result_summary});
    assert.doesNotMatch(text, /Reported browser result/);
  }
  for (const tool of ["browser", "desktop", "coding"]) {
    const text = render({tool, state: "stopped", task_verified: false, result_summary: "not-a-browser-answer"});
    assert.doesNotMatch(text, /Reported browser result|not-a-browser-answer/);
  }
  const active = render({tool: "general_browser", state: "deciding", task_verified: false, result_summary: "unfinished-answer"});
  assert.doesNotMatch(active, /Reported browser result|unfinished-answer/);
});

test("the maximum supported answer remains whole, including line breaks and literal markup", () => {
  const prefix = "<script>literal public heading</script>\n";
  for (const fill of ["é", "🌊"]) {
    const answer = prefix + fill.repeat(500 - [...prefix].length);
    assert.equal([...answer].length, 500);
    const text = render({tool: "general_browser", state: "stopped", task_verified: false, result_summary: answer});
    assert.ok(text.includes(answer));
  }
});


test("a stale browser task_verified flag cannot advertise independent verification", () => {
  const text = render({tool: "general_browser", state: "stopped", task_verified: true,
    credits_reconciled: true, semantic_satisfied: true, result_summary: "The public headline"});
  assert.match(text, /The public headline/);
  assert.match(text, /the task remains unverified/);
  assert.doesNotMatch(text, /independently verified|reviewed result was verified/i);
});
