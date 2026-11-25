import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { desktopInvoke, desktopInvokeOrThrow, desktopListen, desktopListenOrThrow, waitForDesktopShell } from "./desktop.ts";
import { UI_DIAGNOSTIC_EVENT } from "./uiDiagnostics.ts";

const originalWindow = globalThis.window;
const originalCustomEvent = globalThis.CustomEvent;
afterEach(() => {
  Object.defineProperty(globalThis, "window", { configurable: true, value: originalWindow });
  Object.defineProperty(globalThis, "CustomEvent", { configurable: true, value: originalCustomEvent });
});

function nativeBridge(invoke: () => Promise<unknown>) {
  const events: Array<{ type: string; detail: { code: string; context: string; text: string; nativeCommand?: string } }> = [];
  Object.defineProperty(globalThis, "CustomEvent", {
    configurable: true,
    value: class {
      type: string;
      detail: { code: string; context: string; text: string; nativeCommand?: string };
      constructor(type: string, options: { detail: { code: string; context: string; text: string; nativeCommand?: string } }) {
        this.type = type;
        this.detail = options.detail;
      }
    },
  });
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: { __TAURI__: { core: { invoke } }, dispatchEvent: (event: (typeof events)[number]) => { events.push(event); return true; } },
  });
  return events;
}

test("nullable native invoke reports a sanitized failure instead of silently dropping it", async () => {
  const events = nativeBridge(async () => { throw new Error("failed at /Users/founder/private with token sk_abcdefghijklmnop"); });
  assert.equal(await desktopInvoke("set_autostart", { enabled: true }), null);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, UI_DIAGNOSTIC_EVENT);
  assert.equal(events[0].detail.code, "native_error");
  assert.equal(events[0].detail.context, "native");
  assert.equal(events[0].detail.text, "Mac command_failed");
  assert.equal(events[0].detail.nativeCommand, "set_autostart");
  assert.doesNotMatch(events[0].detail.text, /founder|private|sk_abcdefghijklmnop/);
});

test("native permission requests stay out of operator diagnostics", async () => {
  const events = nativeBridge(async () => { throw "permission_required: enable Accessibility"; });
  assert.equal(await desktopInvoke("macos_control_status"), null);
  assert.equal(events.length, 0);
});

test("strict native invoke rejects the mutation and emits one diagnostic", async () => {
  const events = nativeBridge(async () => { throw "autostart failed"; });
  await assert.rejects(desktopInvokeOrThrow("set_autostart", { enabled: true }), /autostart failed/);
  assert.equal(events.length, 1);
});

test("missing native event bridge reports listener failure without event name or payload", async () => {
  const events = nativeBridge(async () => null);
  const stop = await desktopListen("private-stream", () => undefined);
  stop();
  await assert.rejects(desktopListenOrThrow("private-stream", () => undefined), /event bridge is unavailable/);
  assert.equal(events.length, 2);
  assert.ok(events.every(event => event.detail.text === "Mac command_failed"));
  assert.ok(events.every(event => event.detail.nativeCommand === "listen_event"));
});

test("unknown native command names cannot enter the diagnostic receipt", async () => {
  const events = nativeBridge(async () => { throw "failed with secret"; });
  assert.equal(await desktopInvoke("private_token_123"), null);
  assert.equal(events.length, 1);
  assert.equal(events[0].detail.nativeCommand, undefined);
});

test("local builder failures retain the closed command name without arguments", async () => {
  const events = nativeBridge(async () => { throw "failed to write /Users/founder/private/config with token=secret"; });
  assert.equal(await desktopInvoke("write_hermes_files", { files: { secret: "private" } }), null);
  assert.equal(events.length, 1);
  assert.deepEqual(events[0].detail, {
    code: "native_error", context: "native", text: "Mac command_failed", nativeCommand: "write_hermes_files",
  });
});

test("desktop detection waits for a late native bridge", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  Object.defineProperty(globalThis, "window", { configurable: true, value: {} });
  const detection = waitForDesktopShell(100, 5);
  setTimeout(() => {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: { __TAURI_INTERNALS__: { invoke: async () => null } },
    });
  }, 15);
  context.mock.timers.tick(15);
  context.mock.timers.tick(5);
  assert.equal(await detection, true);
});

test("desktop detection resolves when the bridge never appears", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  Object.defineProperty(globalThis, "window", { configurable: true, value: {} });
  const detection = waitForDesktopShell(20, 5);
  context.mock.timers.tick(20);
  assert.equal(await detection, false);
});

test("desktop detection stops when its boundary unmounts", async () => {
  Object.defineProperty(globalThis, "window", { configurable: true, value: {} });
  const controller = new AbortController();
  const detection = waitForDesktopShell(100, 5, controller.signal);
  controller.abort();
  assert.equal(await detection, false);
});

test("quiet invoke still throws and logs the failure as transient", async () => {
  const events: Event[] = [];
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: async () => { throw "stream_unavailable"; } },
    dispatchEvent: (e: Event) => { events.push(e); return true; } } });
  await assert.rejects(desktopInvokeOrThrow("desktop_events_unsubscribe", {}, { quiet: true }), /stream_unavailable/);
  assert.equal((events[0] as CustomEvent<{ severity?: string }>).detail.severity, "transient");
  await assert.rejects(desktopInvokeOrThrow("desktop_events_unsubscribe", {}), /stream_unavailable/);
  assert.equal((events[1] as CustomEvent<{ severity?: string }>).detail.severity, undefined);
});
