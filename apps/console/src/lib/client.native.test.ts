import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api, apiSwr, authenticatedFetch, clearVerifiedSession, ensureSession, logout, openIdeaSession, verifiedSessionSubject } from "./client.ts";
import { UI_DIAGNOSTIC_EVENT } from "./uiDiagnostics.ts";

const originalWindow = globalThis.window;
const originalFetch = globalThis.fetch;
const originalStorage = globalThis.sessionStorage;
const originalCustomEvent = globalThis.CustomEvent;

afterEach(() => {
  clearVerifiedSession();
  Object.defineProperty(globalThis, "window", { configurable: true, value: originalWindow });
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: originalStorage });
  globalThis.fetch = originalFetch;
  Object.defineProperty(globalThis, "CustomEvent", { configurable: true, value: originalCustomEvent });
});

function native(handler: (command: string, args?: Record<string, unknown>) => Promise<unknown>) {
  const events: Event[] = [];
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: handler },
    dispatchEvent: (event: Event) => { events.push(event); return true; },
  } });
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: {
    setItem: () => undefined,
  } });
  globalThis.fetch = async () => { throw new Error("Native requests must not fall back to cookie fetch"); };
  return events;
}

test("a retired successful native session check retries without returning a synthetic 401", async () => {
  let checks = 0;
  native(async (command, args) => {
    if (command === "gateway_url") return "https://gateway.supymem.com";
    if (args?.path === "/auth/session") {
      checks++;
      if (checks === 1) clearVerifiedSession();
      return { status: 200, data: { authenticated: true, subject: "usr_current" } };
    }
    throw Error(command);
  });
  assert.equal((await ensureSession("changeme", true)).ok, true);
  assert.equal(checks, 2);
  assert.equal(verifiedSessionSubject(), "usr_current");
});

test("a native session check retired twice stops after one retry", async () => {
  let checks = 0;
  native(async (command, args) => {
    if (command === "gateway_url") return "https://gateway.supymem.com";
    if (args?.path === "/auth/session") {
      checks++;
      if (checks <= 2) clearVerifiedSession();
      return { status: 200, data: { authenticated: true, subject: "usr_retired" } };
    }
    throw Error(command);
  });
  assert.deepEqual(await ensureSession("changeme", true), { ok: false, status: 401, data: { authenticated: false } });
  assert.equal(checks, 2);
  assert.equal(verifiedSessionSubject(), null);
});

test("concurrent idea loaders open once and later visits read the saved session", async () => {
  const calls: { method: string; path: string }[] = [];
  native(async (command, args) => {
    if (command === "gateway_url") return "https://gateway.supymem.com";
    if (args?.path === "/auth/session") return { status: 200, data: { authenticated: true, subject: "usr_idea" } };
    calls.push({ method: String(args?.method), path: String(args?.path) });
    return { status: args?.method === "POST" ? 201 : 200, data: { id: "session_one", state: "reviewing" } };
  });
  await ensureSession("changeme", true);
  const first = openIdeaSession("ven_idea");
  const second = openIdeaSession("ven_idea");
  assert.equal(first, second);
  assert.equal((await first).ok, true);
  assert.equal((await second).ok, true);
  assert.equal((await openIdeaSession("ven_idea")).ok, true);
  assert.deepEqual(calls, [
    { method: "POST", path: "/api/ventures/ven_idea/idea/sessions" },
    { method: "GET", path: "/api/ventures/ven_idea/idea/sessions/session_one" },
  ]);
});

test("Mac API calls preserve operation keys without exporting credentials", async () => {
  const calls: unknown[] = [];
  native(async (command, args) => { calls.push({ command, args }); return { status: 201, data: { id: "job_test" } }; });
  const result = await api("/api/workspaces/ws_a/tasks", {
    method: "POST", body: JSON.stringify({ name: "Build landing page" }),
    headers: { "Idempotency-Key": "operation_a", "X-Workspace-ID": "ws_a", "Authorization": "Bearer untrusted", "Cookie": "untrusted" },
  });
  assert.equal(result.status, 201);
  // Exactly the headers released Mac apps accept; anything else is native_request_denied.
  assert.deepEqual(calls, [{ command: "desktop_api_request", args: {
    method: "POST", path: "/api/workspaces/ws_a/tasks", body: { name: "Build landing page" },
    headers: { "Idempotency-Key": "operation_a", "X-Workspace-ID": "ws_a" },
  } }]);
});

test("U70 stops unauthorized polling until an explicit successful session check", async () => {
  let rejected = 0;
  native(async (command, args) => {
    if (command === "gateway_url") return "https://gateway.supymem.com";
    if (command === "desktop_auth_status") return { state: "connected" };
    if (args?.path === "/auth/session") return {status: 200, data: {authenticated: true, subject: "usr_poll"}};
    rejected++;
    return {status: 401, data: {error: {code: "unauthenticated"}}};
  });
  await ensureSession("changeme", true);
  for (let attempt = 0; attempt < 4; attempt++) assert.equal((await api("/api/ventures/ven_poll/dashboard")).status, 401);
  assert.equal(rejected, 1);
  await ensureSession("changeme", true);
  assert.equal((await api("/api/ventures/ven_poll/dashboard")).status, 401);
  assert.equal(rejected, 2);
  for (let attempt = 0; attempt < 2; attempt++) await api("/api/ventures/ven_poll/dashboard", {method: "POST", body: "{}"});
  assert.equal(rejected, 4, "explicit actions remain retryable; only unauthorized polling stops");
});

test("optional greeting IPC failure stays quiet without hiding other API failures", async () => {
  const events = native(async () => { throw new Error("private native failure"); });
  assert.equal((await api("/api/me/greeting")).ok, false);
  const greeting = events.filter(event => event.type === UI_DIAGNOSTIC_EVENT) as CustomEvent[];
  assert.equal(greeting.length, 1);
  assert.equal(greeting[0].detail.severity, "transient");
  assert.equal((await api("/api/teams")).ok, false);
  const failures = events.filter(event => event.type === UI_DIAGNOSTIC_EVENT) as CustomEvent[];
  assert.equal(failures.length, 2);
  assert.notEqual(failures[1].detail.severity, "transient");
});

test("Stop aborts the native chat wait immediately without replaying the request", async () => {
  let calls = 0, finish: (reply: unknown) => void = () => {};
  native(async () => {calls++; return new Promise(resolve => {finish = resolve;});});
  const controller = new AbortController();
  const pending = api("/api/ventures/ven_stop/chat", {method: "POST", body: '{"message":"research"}', signal: controller.signal});
  controller.abort();
  assert.equal((await pending).status, 0);
  assert.equal(calls, 1);
  finish({status: 200, data: {body_md: "Saved"}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 1);
});

test("Mac session is the native authenticated subject", async () => {
  native(async (command) => command === "gateway_url" ? "https://gateway.supymem.com" : {
    status: 200, data: { authenticated: true, subject: "usr_native" },
  });
  const result = await ensureSession("changeme", true);
  assert.equal(result.ok, true);
  assert.equal((result.data as { subject: string }).subject, "usr_native");
  assert.equal(verifiedSessionSubject(), "usr_native");
});

test("a route can retain only a verified process-local subject while its native check runs", async () => {
  let available = true;
  native(async (command) => command === "gateway_url" ? "https://gateway.supymem.com" : available
    ? { status: 200, data: { authenticated: true, subject: "usr_route" } }
    : { status: 503, data: { error: { code: "native_auth_busy" } } });
  assert.equal(verifiedSessionSubject(), null);
  assert.equal((await ensureSession("changeme", true)).ok, true);
  assert.equal(verifiedSessionSubject(), "usr_route");
  available = false;
  assert.equal((await ensureSession("changeme", true)).status, 503);
  assert.equal(verifiedSessionSubject(), "usr_route", "a temporary busy check must not erase a verified actor");
  clearVerifiedSession();
  assert.equal(verifiedSessionSubject(), null, "sign-out must invalidate the route hint immediately");
});

test("a confirmed hosted 401 invalidates the route hint before the next page", async () => {
  native(async (command) => command === "gateway_url" ? "https://gateway.supymem.com" : {
    status: 200, data: { authenticated: true, subject: "usr_route" },
  });
  assert.equal((await ensureSession("changeme", true)).ok, true);
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    location: { origin: "https://app.supymem.com" },
    dispatchEvent: () => true,
  } });
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: {
    getItem: () => "https://gateway.supymem.com",
  } });
  globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: "session_invalid" } }), { status: 401 });
  assert.equal((await ensureSession("changeme", true)).status, 401);
  assert.equal(verifiedSessionSubject(), null);
});

test("a native session reply from before sign-out cannot restore the old actor", async () => {
  let finishOldCheck: ((value: unknown) => void) | undefined;
  let calls = 0;
  native(async (command) => {
    if (command === "gateway_url") return "https://gateway.supymem.com";
    if (command === "desktop_auth_status") return { state: "signed_out" };
    calls++;
    if (calls === 1) return { status: 200, data: { authenticated: true, subject: "usr_route" } };
    if (calls > 2) return { status: 401, data: { authenticated: false } };
    return new Promise((resolve) => { finishOldCheck = resolve; });
  });
  assert.equal((await ensureSession("changeme", true)).ok, true);
  const pending = ensureSession("changeme", true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(finishOldCheck);
  clearVerifiedSession();
  finishOldCheck({ status: 200, data: { authenticated: true, subject: "usr_route" } });
  assert.equal((await pending).status, 401);
  assert.equal(calls, 3, "the retired reply is discarded and sign-out is verified by a new check");
  assert.equal(verifiedSessionSubject(), null);
});

test("Revoked native auth never falls back to another browser account", async () => {
  native(async (command) => command === "gateway_url" ? "https://gateway.supymem.com" : {
    status: 401, data: { error: { code: "reauth_required" } },
  });
  assert.equal((await ensureSession("changeme", true)).status, 401);
  assert.equal((await api("/api/ventures")).status, 401);
});

test("a feature 401 does not clear the Mac actor when native auth remains connected", async () => {
  const events = native(async (command, args) => {
    if (command === "gateway_url") return "https://gateway.supymem.com";
    if (command === "desktop_auth_status") return { state: "connected", subject: "usr_native" };
    if (command === "desktop_api_request") return args?.path === "/auth/session"
      ? { status: 200, data: { authenticated: true, subject: "usr_native" } }
      : { status: 401, data: { error: { code: "feature_credential_required" } } };
    throw new Error(`Unexpected command ${command}`);
  });
  assert.equal((await ensureSession("changeme", true)).ok, true);
  const before = events.filter((event) => event.type === "supymem-session-changed").length;
  const result = await api("/api/ventures");
  assert.equal(result.status, 401);
  assert.equal(events.filter((event) => event.type === "supymem-session-changed").length, before);
});

test("a feature 401 clears the Mac actor after native revocation", async () => {
  const events = native(async (command, args) => {
    if (command === "gateway_url") return "https://gateway.supymem.com";
    if (command === "desktop_auth_status") return { state: "reauth_required" };
    if (command === "desktop_api_request") return args?.path === "/auth/session"
      ? { status: 200, data: { authenticated: true, subject: "usr_native" } }
      : { status: 401, data: { error: { code: "device_revoked_or_expired" } } };
    throw new Error(`Unexpected command ${command}`);
  });
  assert.equal((await ensureSession("changeme", true)).ok, true);
  const before = events.filter((event) => event.type === "supymem-session-changed").length;
  const result = await api("/api/ventures");
  assert.equal(result.status, 401);
  assert.equal(events.filter((event) => event.type === "supymem-session-changed").length, before + 1);
});

test("Native IPC failure remains an unavailable operation", async () => {
  const events = native(async () => { throw new Error("keychain unavailable"); });
  const result = await api("/api/ventures");
  assert.equal(result.ok, false);
  assert.equal(result.status, 0);
  assert.equal(events.filter(event => event.type === UI_DIAGNOSTIC_EVENT).length, 0);
});

test("Pending secure storage stays unavailable without falling back to a browser account", async () => {
  native(async (command) => command === "gateway_url" ? "https://gateway.supymem.com" : {
    status: 503, data: { error: { code: "secure_store_pending" } },
  });
  const session = await ensureSession("changeme", true);
  assert.equal(session.ok, false);
  assert.equal(session.status, 503);
  assert.equal((session.data as { error: { code: string } }).error.code, "secure_store_pending");
  native(async () => ({ state: "secure_store_pending" }));
  assert.equal((await logout()).ok, false);
});

test("Mac sign-out uses native revocation", async () => {
  const calls: string[] = [];
  native(async (command) => { calls.push(command); return { state: "signed_out" }; });
  assert.equal((await logout()).ok, true);
  assert.deepEqual(calls, ["desktop_auth_sign_out"]);
});

test("Mac logout distinguishes pending revocation and secure-store failure", async () => {
  native(async () => ({ state: "revocation_pending" }));
  const pending = await logout();
  assert.equal(pending.status, 202);
  assert.equal(pending.ok, true);
  native(async () => ({ state: "keychain_unavailable" }));
  const failed = await logout();
  assert.equal(failed.status, 503);
  assert.equal(failed.ok, false);
});

test("native file response failure emits a closed diagnostic and preserves caller body", async () => {
  Object.defineProperty(globalThis, "CustomEvent", { configurable: true, value: class {
    type: string; detail: unknown;
    constructor(type: string, options: { detail: unknown }) { this.type = type; this.detail = options.detail; }
  } });
  const events = native(async command => {
    assert.equal(command, "desktop_http_request");
    return { status: 500, bodyBase64: btoa(JSON.stringify({ error: { code: "build_worker_failed", message: "xoxb-private founder prompt" } })),
      headers: { "content-type": "application/json" } };
  });
  const response = await authenticatedFetch("/api/build/evidence");
  assert.equal(response.status, 500);
  assert.match(await response.text(), /founder prompt/);
  const diagnostic = events.find(event => event.type === UI_DIAGNOSTIC_EVENT) as unknown as { detail: { text: string } };
  assert.equal(diagnostic.detail.text, "API request_failed (status 500)");
});

test("Mac service failure emits a closed operation label without the requested path", async () => {
  Object.defineProperty(globalThis, "CustomEvent", { configurable: true, value: class {
    type: string; detail: unknown;
    constructor(type: string, options: { detail: unknown }) { this.type = type; this.detail = options.detail; }
  } });
  const events = native(async () => ({ status: 503, data: { error: { code: "temporary_failure", message: "private founder content" } } }));
  const result = await api("/api/ventures/ven_private/waitlist?token=secret");
  assert.equal(result.status, 503);
  const diagnostic = events.find(event => event.type === UI_DIAGNOSTIC_EVENT) as unknown as {
    detail: { text: string; operation?: string; transport?: string; orderId?: string };
  };
  assert.equal(diagnostic.detail.text, "API request_failed (status 503)");
  assert.equal(diagnostic.detail.operation, "venture_waitlist_read");
  assert.equal(diagnostic.detail.transport, "native_bridge");
  assert.equal(diagnostic.detail.orderId, undefined);
  assert.ok(!JSON.stringify(diagnostic.detail).includes("ven_private"));
  assert.ok(!JSON.stringify(diagnostic.detail).includes("secret"));
});

test("Mac work-order API failure carries only exact opaque order correlation", async () => {
  Object.defineProperty(globalThis, "CustomEvent", { configurable: true, value: class {
    type: string; detail: unknown;
    constructor(type: string, options: { detail: unknown }) { this.type = type; this.detail = options.detail; }
  } });
  const events = native(async () => ({ status: 503, data: { error: { code: "upstream_unavailable", message: "private chat and token" } } }));
  await api("/api/ventures/ven_private/device-work-orders/dwo_exact/approve?token=secret", { method: "POST", body: "{}" });
  const diagnostic = events.find(event => event.type === UI_DIAGNOSTIC_EVENT) as unknown as {
    detail: { operation: string; origin?: string; transport: string; orderId: string };
  };
  assert.deepEqual(diagnostic.detail, { code: "server_error", context: "api", text: "API request_failed (status 503)",
    operation: "mac_order_approve", transport: "native_bridge", orderId: "dwo_exact",
    route: "/ventures/{id}/device-work-orders/{id}/approve", status: 503, serverCode: "upstream_unavailable", feature: "computer_use" });
  assert.ok(!JSON.stringify(diagnostic.detail).includes("ven_private"));
  assert.ok(!JSON.stringify(diagnostic.detail).includes("secret"));
});

test("optional missing site is an empty state but another site 404 remains diagnostic", async () => {
  Object.defineProperty(globalThis, "CustomEvent", { configurable: true, value: class {
    type: string; detail: unknown;
    constructor(type: string, options: { detail: unknown }) { this.type = type; this.detail = options.detail; }
  } });
  const responses = [
    { status: 404, data: { detail: { error: { code: "no_site" } } } },
    { status: 404, data: { detail: { error: { code: "not_found" } } } },
  ];
  const events = native(async () => responses.shift());
  assert.equal((await api("/api/workspaces/wsp_private/site")).status, 404);
  assert.equal(events.filter(event => event.type === UI_DIAGNOSTIC_EVENT).length, 0);
  assert.equal((await api("/api/workspaces/wsp_private/site")).status, 404);
  assert.equal(events.filter(event => event.type === UI_DIAGNOSTIC_EVENT).length, 1);
});


test("SWR callback and awaited result cannot expose a prior account reply", async () => {
  let subject = "usr_swr_a", finish: ((value: unknown) => void) | undefined;
  native(async (command, args) => {
    if (command === "gateway_url") return "https://gateway.supymem.com";
    if (args?.path === "/api/ventures/swr-account/dashboard") return new Promise(resolve => { finish = resolve; });
    return { status: 200, data: { authenticated: true, subject } };
  });
  await ensureSession("changeme", true);
  const observed: unknown[] = [];
  const pending = apiSwr("/api/ventures/swr-account/dashboard", r => observed.push(r.data));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.ok(finish);
  subject = "usr_swr_b"; await ensureSession("changeme", true);
  finish({ status: 200, data: { private_to_old_actor: true } });
  const result = await pending;
  assert.deepEqual(observed, []);
  assert.equal(result.ok, false); assert.equal(result.data, null);
});

test("SWR response cannot revive across account A to B to A", async () => {
  let subject = "usr_swr_aba", finish: ((value: unknown) => void) | undefined;
  native(async (command, args) => {
    if (command === "gateway_url") return "https://gateway.supymem.com";
    if (args?.path === "/api/ventures/swr-aba/dashboard") return new Promise(resolve => { finish = resolve; });
    return { status: 200, data: { authenticated: true, subject } };
  });
  await ensureSession("changeme", true);
  const observed: unknown[] = [];
  const pending = apiSwr("/api/ventures/swr-aba/dashboard", r => observed.push(r.data));
  await new Promise(resolve => setTimeout(resolve, 0));
  subject = "usr_swr_other"; await ensureSession("changeme", true);
  subject = "usr_swr_aba"; await ensureSession("changeme", true);
  finish!({ status: 200, data: { stale_scope: true } });
  assert.equal((await pending).data, null);
  assert.deepEqual(observed, []);
});
