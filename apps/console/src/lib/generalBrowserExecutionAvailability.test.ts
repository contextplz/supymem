import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { generalBrowserExecutionAvailability, generalBrowserOrderAvailability, generalBrowserExecutionMessage } from "./generalBrowserExecutionAvailability.ts";
import { desktopExecutionAvailability, desktopExecutionMessage, isNativeDesktopManifest } from "./desktopExecutionAvailability.ts";
import { browserReadOnlyAuthority } from "./browserExecutionProfile.ts";

const source = fs.readFileSync(new URL("../app/v/[ventureId]/device-work-orders/[orderId]/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("review.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function actual(name: string, globals: Record<string, unknown>) {
  let callback = "";
  function visit(node: ts.Node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) callback = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(callback);
  const context = vm.createContext({ generalBrowserExecutionAvailability, generalBrowserOrderAvailability, generalBrowserExecutionMessage,
    desktopExecutionAvailability, desktopExecutionMessage, isNativeDesktopManifest, browserReadOnlyAuthority,
    productGrantBlocked: false, productCapsDirty: false, isDesktopShell: () => true, isLocalConsole: () => false, browserBuild: false, ...globals });
  vm.runInContext(ts.transpileModule(`${callback};globalThis.actual=${name};`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
  return context.actual as (action: string) => Promise<void>;
}
const available = {ok: true, data: {available: true, general_browser_execution_v1: true}};
const unavailable = {ok: true, data: {available: false}};
function fixture(status = "available", fresh: unknown = unavailable, manifest: object = {general_browser_verification: {}}) {
  const calls: string[] = [], errors: string[] = [];
  const context = {order: {manifest, review_digest: "saved-digest"}, generalAvailability: status,
    desktopGrantBlocked: false, desktopAvailability: "unknown", inFlight: {current: false}, alive: {current: true}, serial: {current: 1},
    acknowledged: true, productCapsDirty: false, path: "/api/ventures/ven_a/device-work-orders/dwo_a", ventureId: "ven_a", orderId: "dwo_a",
    setBusy: () => {}, setError: (message: string) => errors.push(message), setGeneralAvailability: () => {},
    setDesktopAvailability: () => {}, setNativeReceipt: () => {}, setOrder: () => {}, setAcknowledged: () => {},
    desktopInvokeOrThrow: async (_command: string, body: {action: string}) => {calls.push(body.action); return {order_id: "dwo_a"};},
    api: async (path: string) => {calls.push(path.endsWith("/capabilities") ? "capabilities" : "approve"); return fresh;},
    load: async () => {}, window: {requestAnimationFrame: () => {}}, focusActions: () => {}};
  return {context, calls, errors};
}
test("general availability requires exact current server capability", () => {
  assert.equal(generalBrowserExecutionAvailability(available), "available");
  assert.equal(generalBrowserExecutionAvailability(unavailable), "unavailable");
  for (const value of [{ok: false, data: {available: true}}, {ok: true}, {ok: true, data: {available: "true"}}, {ok: true, data: {available: true}}]) {
    assert.equal(generalBrowserExecutionAvailability(value), "unknown");
  }
});
test("actual general callbacks refuse missing or unavailable loaded capabilities", async () => {
  for (const status of ["unknown", "unavailable"]) for (const action of ["start", "continue", "handoff", "approve"]) {
    const f = fixture(status); await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, []);
  }
});
test("actual general callbacks recheck capabilities before approval or native dispatch", async () => {
  for (const action of ["start", "continue", "handoff", "approve"]) for (const fresh of [unavailable, {ok: false}]) {
    const f = fixture("available", fresh); await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, ["capabilities"]); assert.ok(f.errors.some(Boolean));
  }
});
test("delayed general preflight cannot cross review retirement", async () => {
  for (const action of ["start", "approve"]) {
    const f = fixture(); f.context.api = async () => {f.calls.push("capabilities"); f.context.serial.current++; return available;};
    await actual(action === "approve" ? "act" : "nativeAction", f.context)(action); assert.deepEqual(f.calls, ["capabilities"]);
  }
});
test("explicit stable capability permits general action but cancel and reconcile need no grant", async () => {
  for (const action of ["start", "continue", "handoff", "approve"]) {
    const f = fixture("available", available); await actual(action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, ["capabilities", action]);
  }
  for (const action of ["cancel", "reconcile"]) {
    const f = fixture("unavailable"); await actual("nativeAction", f.context)(action); assert.deepEqual(f.calls, [action]);
  }
});
test("actual read-only callbacks refuse input handoff and continue while preserving cancel and reconcile", async () => {
  const profile = JSON.parse(fs.readFileSync(new URL("../../../../config/browser_cuai_profile.json", import.meta.url), "utf8"));
  const manifest = {actions: ["browser.read"], general_browser_verification: {
    execution_profile: profile, allow_type: false, fill_values: {}}};
  assert.equal(browserReadOnlyAuthority(manifest), true);
  for (const action of ["continue", "handoff"]) {
    const f = fixture("available", available, manifest);
    await actual("nativeAction", f.context)(action);
    assert.deepEqual(f.calls, []);
    assert.match(f.errors.at(-1)!, /read only.*No browser input access is permitted/);
  }
  for (const action of ["cancel", "reconcile"]) {
    const f = fixture("unavailable", unavailable, manifest);
    await actual("nativeAction", f.context)(action);
    assert.deepEqual(f.calls, [action]);
  }
});
test("typed page coding and terminal profiles preserve their existing start path", async () => {
  for (const manifest of [{browser_verification: {}}, {coding_verification: {}}, {terminal_verification: {}}]) {
    const f = fixture("unknown", unavailable, manifest); await actual("nativeAction", f.context)("start"); assert.deepEqual(f.calls, ["start"]);
  }
});
test("actual general review controls include a grant fence and honest beta status", () => {
  assert.match(source, /const canStart = !desktopGrantBlocked && !generalGrantBlocked/);
  assert.match(source, /data-usage-control="mac_approve" disabled=\{busy \|\| productCapsDirty \|\| expired \|\| !acknowledged \|\| desktopGrantBlocked \|\| generalGrantBlocked \|\| productGrantBlocked\}/);
  for (const action of ["handoff", "continue"]) assert.ok(source.includes(`disabled={busy || generalGrantBlocked} onClick={() => void nativeAction("${action}")}`));
  assert.match(generalBrowserExecutionMessage("unavailable"), /unavailable in this beta/);
});

test("unsaved product limits cannot borrow the previous acknowledgment", async () => {
  const f = fixture("available");
  f.context.productCapsDirty = true;
  await actual("act", f.context)("approve");
  assert.deepEqual(f.calls, []);
});
