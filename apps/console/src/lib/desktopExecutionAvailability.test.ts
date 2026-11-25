import { routeInChat } from "./chatRouteAction.ts";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { desktopExecutionAvailability, desktopExecutionMessage, isNativeDesktopManifest } from "./desktopExecutionAvailability.ts";
import { desktopControlRequest, SUPPORTED_MAC_TASK_EXAMPLES } from "./reviewableDeviceRequest.ts";
import { MAC_TASK_PERMISSION_EVENT, macTaskPermissionsGranted } from "./macPermissions.ts";
import { browserReadOnlyAuthority } from "./browserExecutionProfile.ts";

const available = { ok: true, data: { paused: false, execution_available: true, unavailability_code: null } };
const unavailable = { ok: true, data: { paused: false, execution_available: false, unavailability_code: "device_desktop_execution_unavailable" } };

test("native availability requires an explicit capability and unavailable takes priority over pause", () => {
  assert.equal(desktopExecutionAvailability(available), "available");
  assert.equal(desktopExecutionAvailability(unavailable), "unavailable");
  assert.equal(desktopExecutionAvailability({ ...unavailable, data: { ...unavailable.data, paused: true } }), "unavailable");
  assert.equal(desktopExecutionAvailability({ ok: true, data: { paused: true, execution_available: false } }), "paused");
  for (const result of [{ ok: false, data: available.data }, { ok: true }, { ok: true, data: { paused: false } },
    { ok: true, data: { paused: false, execution_available: "true" } }, { ok: true, data: { paused: true, execution_available: true } },
    { ok: true, data: { paused: false, execution_available: true, unavailability_code: "unknown" } }]) {
    assert.equal(desktopExecutionAvailability(result), "unknown");
  }
});

test("native manifest classification preserves legacy desktop actions and excludes browser and coding", () => {
  assert.equal(isNativeDesktopManifest({ desktop_verification: {} }), true);
  assert.equal(isNativeDesktopManifest({ actions: ["desktop.observe"] }), true);
  assert.equal(isNativeDesktopManifest({ actions: ["browser.navigate", "coding.subscription_static"] }), false);
});

function actual(path: string, name: string, globals: Record<string, unknown>) {
  const source = fs.readFileSync(new URL(path, import.meta.url), "utf8");
  const tree = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let callback = "";
  function visit(node: ts.Node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) callback = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(callback, name);
  // Replace module loading only. The actual production callback and its guards execute unchanged.
  callback = callback.replace(/await import\("(@\/lib\/[^\"]+)"\)/g, (_match, module) => `(await Promise.resolve(modules[${JSON.stringify(module)}]))`);
  const context = vm.createContext({routeInChat,  exports: {}, MAC_TASK_PERMISSION_EVENT, macTaskPermissionsGranted, desktopExecutionAvailability, desktopExecutionMessage, isNativeDesktopManifest, browserReadOnlyAuthority, productGrantBlocked: false, productCapsDirty: false, isDesktopShell: () => true, isLocalConsole: () => false, ...globals });
  vm.runInContext(ts.transpileModule(`${callback};globalThis.actual=${name};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, context);
  return context.actual as (argument?: string) => Promise<void>;
}

const reviewPath = "../app/v/[ventureId]/device-work-orders/[orderId]/page.tsx";
function reviewFixture(status: string, manifest: object = { desktop_verification: {} }, fresh: { ok: boolean; data?: unknown } = available) {
  const calls: string[] = [], errors: string[] = [];
  const context = {
    order: { manifest, review_digest: "digest" }, desktopGrantBlocked: isNativeDesktopManifest(manifest) && status !== "available",
    desktopAvailability: status, browserBuild: false, inFlight: { current: false }, alive: { current: true }, serial: { current: 1 },
    acknowledged: true, ventureId: "ven_a", orderId: "dwo_a", path: "/api/ventures/ven_a/device-work-orders/dwo_a",
    setBusy: () => {}, setError: (value: string) => errors.push(value), setDesktopAvailability: () => {},
    setNativeReceipt: () => {}, setOrder: () => {}, setAcknowledged: () => {},
    desktopInvokeOrThrow: async (_command: string, body: { action: string }) => { calls.push(body.action); return {order_id: "dwo_a"}; },
    api: async (path: string) => { calls.push(path.endsWith("/device-desktop-pause") ? "availability" : "approve"); return fresh; },
    load: async () => {}, window: { requestAnimationFrame: () => {} },
  };
  return { calls, errors, context };
}

test("actual saved native approve/start/continue/handoff callbacks refuse unavailable, paused and unknown", async () => {
  for (const status of ["unavailable", "paused", "unknown"]) for (const manifest of [{ desktop_verification: {} }, { actions: ["desktop.observe"] }]) {
    for (const action of ["start", "continue", "handoff"]) {
      const f = reviewFixture(status, manifest);
      await actual(reviewPath, "nativeAction", f.context)(action);
      assert.deepEqual(f.calls, []);
    }
    const f = reviewFixture(status, manifest);
    await actual(reviewPath, "act", f.context)("approve");
    assert.deepEqual(f.calls, []);
  }
});

test("actual saved native callbacks recheck current availability before effects or approval", async () => {
  for (const action of ["start", "continue", "handoff", "approve"]) {
    const f = reviewFixture("available", { desktop_verification: {} }, unavailable);
    await actual(reviewPath, action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, ["availability"]);
    assert.match(f.errors.at(-1)!, /unavailable in this beta/);
  }
});

test("actual saved native callbacks cannot use delayed availability after session or page retirement", async () => {
  for (const action of ["start", "approve"]) {
    const f = reviewFixture("available");
    f.context.api = async () => { f.calls.push("availability"); f.context.serial.current++; return available; };
    await actual(reviewPath, action === "approve" ? "act" : "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, ["availability"]);
  }
});

test("actual saved native cancel/reconcile remain available and browser/static start is unchanged", async () => {
  for (const action of ["cancel", "reconcile"]) {
    const f = reviewFixture("unavailable");
    await actual(reviewPath, "nativeAction", f.context)(action);
    assert.deepEqual(f.calls, [action]);
  }
  for (const manifest of [{ browser_verification: { expected_locations: ["https://owned.example/"] } }, { coding_verification: {} }, { app_navigation_verification: {} }]) {
    const f = reviewFixture("unknown", manifest);
    await actual(reviewPath, "nativeAction", f.context)("start");
    assert.deepEqual(f.calls, ["start"]);
  }
});

function paneFixture(fresh: { ok: boolean; data?: unknown } = unavailable, explicit = false, permissions: unknown = {"screen-recording": "granted", accessibility: "granted"}) {
  const calls: string[] = [], errors: string[] = [];
  const owner = { ventureId: "ven_a", epoch: 2, generation: 1 };
  const events: Array<{type: string; detail: {resume: () => Promise<void>}}> = [];
  const context = {
    renderedPreparationOwner: owner, preparationOwner: { current: owner }, verifiedSessionEpoch: () => 2,
    sendingRef: { current: false }, turnRef: { current: 1 }, setErr: (text: string) => errors.push(text),
    isComputerUse: true, announceUsageControl: () => {}, setBusy: () => {}, setItems: () => {}, known: null,
    ventureId: "ven_a", surfaceReview: explicit ? {} : null, reviewedSurfaceBody: () => explicit ? { reviewed_surfaces: [] } : null,
    modules: {
      "@/lib/client": { api: async (path: string) => { calls.push(path.endsWith("/device-desktop-pause") ? "availability" : "prepare"); return path.endsWith("/device-desktop-pause") ? fresh : { ok: true, data: { review_path: "/v/ven_a/device-work-orders/dwo_a" } }; } },
      "@/lib/reviewableDeviceRequest": { desktopControlRequest },
      "@/lib/desktopApplication": { selectDesktopApplication: async () => { calls.push("inventory"); return {}; } },
    },
    clientRef: { current: null }, acpSession: { current: null }, setSessionId: () => {}, setPermissions: () => {},
    invokeErrorMessage: (error: Error) => error.message, isUnknownDesktopCommand: () => false,
    setInput: () => {}, setPhase: () => {}, toolLabel: "Computer Use", onFailedRef: { current: null },
    desktopInvoke: async (command: string) => {calls.push(command); assert.equal(command, "permissions_status"); return permissions;},
    CustomEvent: class {type: string; detail: unknown; constructor(type: string, init: {detail: unknown}) {this.type = type; this.detail = init.detail;}},
    window: { location: { assign: () => calls.push("review") }, dispatchEvent: (event: typeof events[number]) => {events.push(event);} },
  };
  return { calls, errors, context, events };
}

test("actual separate computer-use callback refuses before native inventory, including retained surface reviews", async () => {
  for (const fresh of [unavailable, { ok: true, data: { paused: false } }]) for (const explicit of [false, true]) {
    const f = paneFixture(fresh, explicit);
    await actual("../components/desktop/AgentSessionPane.tsx", "startOrSend", f.context)("Open Calculator and tell me exactly what number is shown.");
    assert.deepEqual(f.calls, ["availability"]);
    assert.match(f.errors.at(-1)!, /unavailable|Could not confirm/);
  }
});

test("actual separate computer-use callback retains isolated-browser, static coding and first-party review routes", async () => {
  for (const goal of ["Go to resend.com and prepare the signup form without submitting or entering credentials", "Build the landing page using Codex.", "Open Builds in Supymem. Do not modify data."]) {
    const f = paneFixture();
    await actual("../components/desktop/AgentSessionPane.tsx", "startOrSend", f.context)(goal);
    assert.deepEqual(f.calls, ["prepare", "review"]);
  }
});

test("actual separate computer-use callback rejects delayed actor retirement before native inventory", async () => {
  const f = paneFixture(available);
  let epoch = 2;
  f.context.verifiedSessionEpoch = () => epoch;
  f.context.modules["@/lib/client"].api = async () => { f.calls.push("availability"); epoch++; return available; };
  await actual("../components/desktop/AgentSessionPane.tsx", "startOrSend", f.context)("Open Calculator and tell me exactly what number is shown.");
  assert.deepEqual(f.calls, ["availability"]);
});

test("actual Mac-task example filter hides native examples without losing browser or coding examples", () => {
  const source = fs.readFileSync(new URL("../components/chat/MacTasksEntry.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("MacTasksEntry.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let predicate = "";
  function visit(node: ts.Node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === "SUPPORTED_MAC_TASK_EXAMPLES.filter") predicate = node.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(predicate);
  for (const availability of ["unavailable", "paused", "unknown", "available"]) {
    const filtered = vm.runInNewContext(ts.transpileModule(`SUPPORTED_MAC_TASK_EXAMPLES.filter(${predicate});`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022 },
    }).outputText, { availability, SUPPORTED_MAC_TASK_EXAMPLES }) as typeof SUPPORTED_MAC_TASK_EXAMPLES;
    assert.equal(filtered.some(example => "desktop" in example), availability === "available");
    assert.ok(filtered.some(example => example.label === "Browser signup goal"));
    assert.ok(filtered.some(example => example.label === "Cursor inspect"));
  }
});

test("ordinary browser Mac-task entry shows unavailable device execution without fetching or exposing native examples", () => {
  for (const ventureId of ["ven_freight", "ven_gardening"]) {
    const requests: string[] = [];
    const elements: Array<{type: unknown; props: Record<string, unknown>}> = [];
    const jsx = (type: unknown, props: Record<string, unknown>) => {
      const element = { type, props }; elements.push(element); return element;
    };
    const render = actual("../components/chat/MacTasksEntry.tsx", "MacTasksEntry", {
      isDesktopShell: () => false, verifiedSessionEpoch: () => 2, useLocalMode: () => false, useFeatureVisibility: () => () => true,
      useRef: (current: unknown) => ({current}), useState: () => [null, () => {}],
      useEffect: (effect: () => void) => effect(),
      styles: {}, Link: "a", SUPPORTED_MAC_TASK_EXAMPLES,
      require: () => ({jsx, jsxs: jsx}),
      api: async (path: string) => {requests.push(path); return available;},
    }) as unknown as (props: {ventureId: string}) => unknown;
    render({ventureId});
    assert.deepEqual(requests, []);
    assert.equal(elements.some(element => element.type === "button"), false);
    assert.match(String(elements.find(element => element.type === "p")?.props.children), /browser cannot access the local executor/);
  }
});

test("actual settings callback cannot resume unavailable or unknown native control", async () => {
  for (const availability of ["unavailable", "unknown"]) {
    let dispatched = 0;
    await actual("../components/settings/DesktopPause.tsx", "toggle", {
      availability, busy: false, api: async () => { dispatched++; },
    })();
    assert.equal(dispatched, 0);
  }
});

test("actual settings callback rejects a retained available callback after owner retirement", async () => {
  let dispatched = 0;
  const renderedOwner = { ventureId: "ven_a", epoch: 2 };
  await actual("../components/settings/DesktopPause.tsx", "toggle", {
    availability: "available", busy: false, renderedOwner, ownerRef: { current: { ...renderedOwner, epoch: 3 } },
    verifiedSessionEpoch: () => 3, api: async () => { dispatched++; },
  })();
  assert.equal(dispatched, 0);
});

test("actual native availability render predicates cannot expose an earlier venture or actor receipt", () => {
  for (const [path, name, receiptName, ownerName] of [
    ["../components/desktop/AgentSessionPane.tsx", "nativeAvailability", "nativeAvailabilityReceipt", "renderedPreparationOwner"],
    ["../components/chat/MacTasksEntry.tsx", "availability", "receipt", "renderedOwner"],
    ["../components/settings/DesktopPause.tsx", "availability", "receipt", "renderedOwner"],
  ]) {
    const source = fs.readFileSync(new URL(path, import.meta.url), "utf8");
    const tree = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    let predicate = "";
    function visit(node: ts.Node) {
      if (ts.isVariableDeclaration(node) && node.name.getText(tree) === name && node.initializer) predicate = node.initializer.getText(tree);
      ts.forEachChild(node, visit);
    }
    visit(tree); assert.ok(predicate);
    const owner = { ventureId: "ven_a", epoch: 2 };
    for (const current of [owner, { ...owner }, { ventureId: "ven_b", epoch: 2 }, { ...owner, epoch: 3 }]) {
      const status = vm.runInNewContext(ts.transpileModule(`(${predicate});`, {
        compilerOptions: { target: ts.ScriptTarget.ES2022 },
      }).outputText, { [receiptName]: { owner, status: "available" }, [ownerName]: current });
      assert.equal(status, current === owner ? "available" : "unknown");
    }
  }
});


test("separate computer-use entry keeps denied permissions inline before inventory and preparation", async () => {
  for (const explicit of [false, true]) {
    const f = paneFixture(available, explicit, {"screen-recording": "not_determined", accessibility: "granted"});
    await actual("../components/desktop/AgentSessionPane.tsx", "startOrSend", f.context)("Open Calculator and tell me exactly what number is shown.");
    assert.deepEqual(f.calls, ["availability", "permissions_status"]);
    assert.equal(f.events.length, 1);
    assert.equal(f.events[0].type, MAC_TASK_PERMISSION_EVENT);
    assert.equal(f.errors.filter(Boolean).length, 0);
  }
});
