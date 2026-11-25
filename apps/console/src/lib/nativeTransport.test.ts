import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { nativeFetch, NativeEventSource } from "./nativeTransport.ts";
import { UI_DIAGNOSTIC_EVENT } from "./uiDiagnostics.ts";

const originalWindow = globalThis.window;
const originalFetch = globalThis.fetch;
afterEach(() => { Object.defineProperty(globalThis, "window", { configurable: true, value: originalWindow }); globalThis.fetch = originalFetch; });
function fixture(handler: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>, events: Event[] = []) {
  const listeners = new Map<string, (event: { payload: unknown }) => void>();
  Object.defineProperty(globalThis, "window", { configurable: true, value: { __TAURI__: {
    core: { invoke: handler }, event: { listen: async (name: string, cb: (event: { payload: unknown }) => void) => { listeners.set(name, cb); return () => listeners.delete(name); } },
  }, dispatchEvent: (event: Event) => { events.push(event); return true; } } });
  globalThis.fetch = async () => { throw new Error("No cookie fallback allowed"); };
  return listeners;
}
function reply(status = 200, body = "hello", type = "text/plain") { return { status, bodyBase64: btoa(body), headers: { "content-type": type, "set-cookie": "not-forwarded" } }; }

test("native fetch preserves binary/text/JSON error bodies and drops auth headers", async () => {
  let captured: Record<string, unknown> | undefined;
  fixture(async (cmd, args) => { assert.equal(cmd, "desktop_http_request"); captured = args; return reply(400, "error details"); });
  const response = await nativeFetch("/api/export", { method: "POST", headers: { Authorization: "unsafe", Cookie: "unsafe", "Content-Type": "application/json", "Idempotency-Key": "operation" }, body: '{"value":1}' });
  assert.equal(response.status, 400); assert.equal(await response.text(), "error details"); assert.equal(response.headers.get("set-cookie"), null);
  assert.deepEqual(captured, { method: "POST", path: "/api/export", headers: { "Idempotency-Key": "operation" }, body: { kind: "json", value: { value: 1 } } });
});
test("multipart uploads preserve bytes and ordinary fields", async () => {
  let body: unknown;
  fixture(async (_, args) => { body = args?.body; return reply(201, '{"saved":true}', "application/json"); });
  const form = new FormData(); form.append("label", "Logo"); form.append("file", new Blob([new Uint8Array([0, 255, 3])], { type: "image/png" }), "logo.png");
  const response = await nativeFetch("/api/brand/logo", { method: "POST", body: form });
  assert.deepEqual(body, { kind: "multipart", fields: [{ name: "label", value: "Logo" }, { name: "file", filename: "logo.png", contentType: "image/png", base64: "AP8D" }] });
  assert.deepEqual(await response.json(), { saved: true });
});
test("empty204 is a valid response", async () => { fixture(async () => reply(204, "")); const response = await nativeFetch("/api/item", { method: "DELETE" }); assert.equal(response.status, 204); assert.equal(await response.text(), ""); });
test("native fetch exposes only the gateway request reference header for diagnostics", async () => {
  fixture(async () => ({ status: 503, bodyBase64: btoa("{}"), headers: {
    "content-type": "application/json", "x-request-id": "req_7gkmnpqrstvw", "set-cookie": "private" } }));
  const response = await nativeFetch("/api/item");
  assert.equal(response.headers.get("x-request-id"), "req_7gkmnpqrstvw");
  assert.equal(response.headers.get("set-cookie"), null);
});
test("401 and ambiguous failures never retry a mutation", async () => {
  for (const status of [401, 0]) { let count = 0; fixture(async () => { count++; return reply(status, "unconfirmed"); });
    if (status) assert.equal((await nativeFetch("/api/item", { method: "POST", body: "{}" })).status, 401);
    else await assert.rejects(nativeFetch("/api/item", { method: "POST", body: "{}" }), /outcome is unconfirmed/);
    assert.equal(count, 1);
  }
});
test("malformed native replies produce one closed diagnostic without body or path", async () => {
  const events: Event[] = [];
  fixture(async () => ({ status: 200, bodyBase64: "not base64 private_token", headers: {} }), events);
  await assert.rejects(nativeFetch("/api/private?key=secret"), /Invalid character/);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, UI_DIAGNOSTIC_EVENT);
  const detail = (events[0] as CustomEvent<{ text: string; context: string }>).detail;
  assert.deepEqual(detail, { code: "native_error", context: "native", text: "Mac command_failed", nativeCommand: "desktop_http_request" });
});
test("oversized upload is rejected before native invocation", async () => { let calls = 0; fixture(async () => { calls++; return reply(); }); const blob = new Blob([new Uint8Array(32 * 1024 * 1024 + 1)]); await assert.rejects(nativeFetch("/api/files", { method: "POST", body: blob }), /32 MiB/); assert.equal(calls, 0); });
test("an aborted request is not dispatched", async () => { let calls = 0; fixture(async () => { calls++; return reply(); }); const abort = new AbortController(); abort.abort(); await assert.rejects(nativeFetch("/api/item", { signal: abort.signal }), { name: "AbortError" }); assert.equal(calls, 0); });
test("native stream subscribes after listeners and closes on signout", async () => {
  const calls: { cmd: string; args?: Record<string, unknown> }[] = [];
  const listeners = fixture(async (cmd, args) => { calls.push({ cmd, args }); return undefined; });
  const source = new NativeEventSource("/api/ventures/ven_a/activity/stream", "12");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls[0].cmd, "desktop_events_subscribe"); assert.equal(calls[0].args?.lastEventId, "12");
  let data = ""; source.addEventListener("activity", (event) => { data = (event as MessageEvent).data; });
  listeners.get("supymem-native-sse")?.({ payload: { id: source.id, kind: "open" } }); assert.equal(source.readyState, 1);
  listeners.get("supymem-native-sse")?.({ payload: { id: source.id, kind: "message", event: "activity", data: "scoped", last_event_id: "13" } }); assert.equal(data, "scoped");
  listeners.get("supymem-native-auth")?.({ payload: { state: "signed_out" } }); assert.equal(source.readyState, 2);
  assert.ok(calls.some((call) => call.cmd === "desktop_events_unsubscribe")); assert.equal(listeners.size, 0);
});

test("native stream busy failure reports its closed stream and source without venture path", async () => {
  const events: Event[] = [];
  const listeners = fixture(async () => undefined, events);
  const source = new NativeEventSource("/api/ventures/ven_private/activity/stream");
  await new Promise((resolve) => setTimeout(resolve, 0));
  listeners.get("supymem-native-sse")?.({ payload: { id: source.id, kind: "error", status: 503,
    origin: "native_auth_busy" } });
  const diagnostic = events.find(event => event.type === UI_DIAGNOSTIC_EVENT) as CustomEvent<Record<string, string>>;
  assert.deepEqual(diagnostic.detail, { code: "server_error", context: "api", text: "API request_failed (status 503)",
    severity: "transient",
    operation: "venture_activity_stream", origin: "native_auth_busy", transport: "native_stream",
    route: "/ventures/{id}/activity/stream", status: 503, serverCode: null, feature: "execution" });
  assert.ok(!JSON.stringify(diagnostic.detail).includes("ven_private"));
  source.close();
});

test("dropped-connection frames are logged transient, then escalate to error after repeated drops", async () => {
  const events: Event[] = [];
  const listeners = fixture(async () => undefined, events);
  const source = new NativeEventSource("/api/ventures/ven_a/activity/stream");
  await new Promise((resolve) => setTimeout(resolve, 0));
  const send = (kind: string) => listeners.get("supymem-native-sse")?.({ payload: { id: source.id, kind, status: 0 } });
  const severities = () => events.filter((e) => e.type === UI_DIAGNOSTIC_EVENT).map((e) => (e as CustomEvent<{ severity?: string }>).detail.severity ?? "error");
  send("error"); send("error"); send("error");
  assert.deepEqual(severities(), ["transient", "transient", "transient"]);
  assert.equal(source.readyState, 0);
  send("open"); send("error"); send("error"); send("error");
  assert.deepEqual(severities().slice(3), ["transient", "transient", "transient"]);
  send("error");
  assert.equal(severities()[6], "error");
  source.close();
});

test("a failing unsubscribe on close is logged as transient", async () => {
  const events: Event[] = [];
  fixture(async (cmd) => { if (cmd === "desktop_events_unsubscribe") throw "stream_unavailable"; return undefined; }, events);
  const source = new NativeEventSource("/api/ventures/ven_a/activity/stream");
  await new Promise((resolve) => setTimeout(resolve, 0));
  source.close();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(events.length, 1);
  assert.equal((events[0] as CustomEvent<{ severity?: string }>).detail.severity, "transient");
});
