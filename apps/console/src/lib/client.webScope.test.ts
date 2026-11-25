import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api, authenticatedFetch, clearVerifiedSession, downloadFile, ensureSession, loginAccount, verifiedSessionSubject } from "./client.ts";

const originals = { window: globalThis.window, storage: globalThis.sessionStorage, document: globalThis.document, fetch: globalThis.fetch,
  blob: Response.prototype.blob, createURL: URL.createObjectURL, revokeURL: URL.revokeObjectURL };
afterEach(() => {
  clearVerifiedSession();
  Object.defineProperty(globalThis, "window", { configurable: true, value: originals.window });
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: originals.storage });
  Object.defineProperty(globalThis, "document", { configurable: true, value: originals.document });
  globalThis.fetch = originals.fetch;
  Response.prototype.blob = originals.blob;
  URL.createObjectURL = originals.createURL;
  URL.revokeObjectURL = originals.revokeURL;
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function reply(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}
const path = "/api/ventures/ven_fixture_a/pause";
const mutation = { method: "POST", body: "{}" };
function browser() {
  const saved = new Map([["cos_gateway_url", "https://owned-fixture.invalid"]]);
  const storage = { getItem: (key: string) => saved.get(key) || null, setItem: (key: string, value: string) => saved.set(key, value), removeItem: (key: string) => saved.delete(key) };
  const doc = { cookie: "" };
  Object.defineProperty(globalThis, "window", { configurable: true, value: { sessionStorage: storage, location: { origin: "https://owned-ui.invalid" }, dispatchEvent: () => true } });
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: storage });
  Object.defineProperty(globalThis, "document", { configurable: true, value: doc });
  const calls: Array<{ path: string; actor: string | null; csrf: string | null }> = [];
  let actor = "usr_fixture_a", token: string | undefined = "fixture-csrf-a";
  const h = {
    calls, doc,
    session: async () => reply({ authenticated: true, subject: actor, csrf: token }),
    request: async (_path: string, _init?: RequestInit): Promise<Response> => reply({ saved: true }),
    login: async (subject: string, csrf?: string) => {
      actor = subject; token = csrf;
      return loginAccount(`${subject}@example.invalid`, "synthetic-fixture-password");
    },
  };
  globalThis.fetch = async (url, init) => {
    const currentPath = new URL(String(url)).pathname;
    if (currentPath === "/auth/login") return reply({ authenticated: true, subject: actor, csrf: token });
    if (currentPath === "/auth/session") return h.session();
    calls.push({ path: currentPath, actor: verifiedSessionSubject(), csrf: new Headers(init?.headers).get("X-CSRF-Token") });
    return h.request(currentPath, init);
  };
  return h;
}
function scopeRefused(result: Awaited<ReturnType<typeof api>>) {
  assert.deepEqual(result, { ok: false, status: 409, data: null, error: "session_scope_changed" });
}

test("retired web session checks retry using the current actor instead of a synthetic 401", async () => {
  const h = browser();
  await h.login("usr_fixture_a", "csrf-a");
  const old = deferred<Response>(), entered = deferred<void>();
  let checks = 0;
  h.session = async () => {
    checks++;
    if (checks === 1) { entered.resolve(); return old.promise; }
    return reply({ authenticated: true, subject: "usr_fixture_b", csrf: "csrf-b" });
  };
  const pending = ensureSession("changeme", true);
  await entered.promise;
  await h.login("usr_fixture_b", "csrf-b");
  old.resolve(reply({ authenticated: true, subject: "usr_fixture_a", csrf: "csrf-a" }));
  assert.equal((await pending).ok, true);
  assert.equal(checks, 2);
  assert.equal(verifiedSessionSubject(), "usr_fixture_b");
});

for (const rejected of [false, true]) {
  test(`a web session check retired twice stops after one retry (rejected=${rejected})`, async () => {
    const h = browser();
    await h.login("usr_fixture_a", "csrf-a");
    let checks = 0;
    h.session = async () => {
      checks++;
      if (checks <= 2) {
        clearVerifiedSession();
        if (rejected) throw Error("retired session check");
      }
      return reply({ authenticated: true, subject: "usr_fixture_a", csrf: "csrf-a" });
    };
    assert.deepEqual(await ensureSession("changeme", true), { ok: false, status: 401, data: { authenticated: false } });
    assert.equal(checks, 2);
    assert.equal(verifiedSessionSubject(), null);
  });
}

test("a delayed A CSRF refusal never retries A's mutation as B", async () => {
  const h = browser(), response = deferred<Response>(), entered = deferred<void>();
  await h.login("usr_fixture_a", "csrf-a");
  h.request = async () => { entered.resolve(); return response.promise; };
  const pending = api(path, mutation); await entered.promise;
  await h.login("usr_fixture_b", "csrf-b");
  response.resolve(reply({ detail: { code: "csrf_failed" } }, 403));
  scopeRefused(await pending);
  assert.deepEqual(h.calls.map(c => c.actor), ["usr_fixture_a"]);
});

test("A to B to A cannot revive an old mutation retry", async () => {
  const h = browser(), response = deferred<Response>(), entered = deferred<void>();
  await h.login("usr_fixture_a", "csrf-a");
  h.request = async () => { entered.resolve(); return response.promise; };
  const pending = api(path, mutation); await entered.promise;
  await h.login("usr_fixture_b", "csrf-b"); await h.login("usr_fixture_a", "csrf-new-a");
  response.resolve(reply({ error: { code: "csrf_failed" } }, 403));
  scopeRefused(await pending); assert.equal(h.calls.length, 1);
});

test("retirement during CSRF recovery withholds the stale session reply and retry", async () => {
  const h = browser(), session = deferred<Response>(), entered = deferred<void>();
  await h.login("usr_fixture_a", "csrf-a");
  h.request = async () => reply({ detail: { code: "csrf_failed" } }, 403);
  h.session = async () => { entered.resolve(); return session.promise; };
  const pending = api(path, mutation); await entered.promise;
  await h.login("usr_fixture_b", "csrf-b");
  session.resolve(reply({ authenticated: true, subject: "usr_fixture_a", csrf: "obsolete-a" }));
  scopeRefused(await pending); assert.equal(h.calls.length, 1);
  assert.equal(verifiedSessionSubject(), "usr_fixture_b");
});

test("retirement during a missing-CSRF preflight causes zero mutation delivery", async () => {
  const h = browser(), session = deferred<Response>(), entered = deferred<void>();
  await h.login("usr_fixture_a");
  h.session = async () => { entered.resolve(); return session.promise; };
  const pending = api(path, mutation); await entered.promise;
  await h.login("usr_fixture_b", "csrf-b");
  session.resolve(reply({ authenticated: true, subject: "usr_fixture_a", csrf: "obsolete-a" }));
  scopeRefused(await pending); assert.equal(h.calls.length, 0);
  assert.equal(verifiedSessionSubject(), "usr_fixture_b");
});

test("a stable verified actor obtains CSRF before one mutation", async () => {
  const h = browser(); await h.login("usr_fixture_a");
  h.session = async () => reply({ authenticated: true, subject: "usr_fixture_a", csrf: "fresh-a" });
  assert.equal((await api(path, mutation)).ok, true);
  assert.deepEqual(h.calls, [{ path, actor: "usr_fixture_a", csrf: "fresh-a" }]);
});

test("same-owner CSRF recovery fetches a fresh token despite an existing session memo", async () => {
  const h = browser(); await h.login("usr_fixture_a", "old-a");
  h.session = async () => reply({ authenticated: true, subject: "usr_fixture_a", csrf: "old-a" });
  await ensureSession("changeme", true);
  let freshChecks = 0;
  h.session = async () => { freshChecks++; return reply({ authenticated: true, subject: "usr_fixture_a", csrf: "new-a" }); };
  h.request = async () => h.calls.length === 1 ? reply({ detail: { code: "csrf_failed" } }, 403) : reply({ saved: true });
  assert.equal((await api(path, mutation)).ok, true);
  assert.equal(freshChecks, 1);
  assert.deepEqual(h.calls.map(c => c.csrf), ["old-a", "new-a"]);
});

test("a second CSRF failure is returned without a third delivery", async () => {
  const h = browser(); await h.login("usr_fixture_a", "old-a");
  h.session = async () => reply({ authenticated: true, subject: "usr_fixture_a", csrf: "new-a" });
  h.request = async () => reply({ detail: { code: "csrf_failed" } }, 403);
  assert.equal((await api(path, mutation)).status, 403); assert.equal(h.calls.length, 2);
});

test("failed same-owner refresh never retries an owned mutation", async () => {
  const h = browser(); await h.login("usr_fixture_a", "csrf-a");
  h.request = async () => reply({ detail: { code: "csrf_failed" } }, 403);
  h.session = async () => reply({ error: { code: "session_invalid" } }, 401);
  scopeRefused(await api(path, mutation)); assert.equal(h.calls.length, 1);
  assert.equal(verifiedSessionSubject(), null);
});

test("normal initial session bootstrap enables a newly owned mutation", async () => {
  const h = browser();
  assert.equal((await ensureSession("changeme", true)).ok, true);
  assert.equal((await api(path, mutation)).ok, true);
  assert.deepEqual(h.calls.map(c => c.actor), ["usr_fixture_a"]);
});

test("an unknown-owner mutation cannot borrow the actor discovered by its preflight", async () => {
  const h = browser(); h.doc.cookie = "cos_csrf=unverified-cookie";
  scopeRefused(await api(path, mutation)); assert.equal(h.calls.length, 0);
  assert.equal(verifiedSessionSubject(), "usr_fixture_a");
  assert.equal((await api(path, mutation)).ok, true, "a new intent after verification is allowed");
});

test("an authenticated response without a subject does not authorize mutation", async () => {
  const h = browser(); h.session = async () => reply({ authenticated: true, csrf: "unknown-owner" });
  scopeRefused(await api(path, mutation)); assert.equal(h.calls.length, 0);
});

test("a retired successful private GET never returns A's body to B", async () => {
  const h = browser(), response = deferred<Response>(), entered = deferred<void>();
  await h.login("usr_fixture_a", "csrf-a");
  h.request = async () => { entered.resolve(); return response.promise; };
  const pending = api("/api/ventures/ven_fixture_a/settings"); await entered.promise;
  await h.login("usr_fixture_b", "csrf-b");
  response.resolve(reply({ private_fixture: "a-only" }));
  scopeRefused(await pending); assert.equal(h.calls.length, 1);
});

test("a delayed initial session check cannot overwrite a newly signed-in actor", async () => {
  const h = browser(), session = deferred<Response>(), entered = deferred<void>();
  h.session = async () => { entered.resolve(); return session.promise; };
  const pending = ensureSession("changeme", true); await entered.promise;
  await h.login("usr_fixture_b", "csrf-b");
  session.resolve(reply({ authenticated: true, subject: "usr_fixture_a", csrf: "old-a" }));
  assert.equal((await pending).ok, false); assert.equal(verifiedSessionSubject(), "usr_fixture_b");
  assert.equal((await api(path, mutation)).ok, true);
  assert.equal(h.calls[0].csrf, "csrf-b");
});

test("a retired error is a static scope refusal rather than A's transport details", async () => {
  const h = browser(), response = deferred<Response>(), entered = deferred<void>();
  await h.login("usr_fixture_a", "csrf-a");
  h.request = async () => { entered.resolve(); await response.promise; throw new Error("private fixture failure"); };
  const pending = api(path, mutation); await entered.promise;
  clearVerifiedSession(); response.resolve(reply(null)); scopeRefused(await pending);
});

test("stable read bootstrap and ordinary server refusal retain their response shape", async () => {
  const h = browser(); h.request = async () => reply({ detail: { code: "unauthenticated" } }, 401);
  assert.deepEqual(await api("/api/ventures"), { ok: false, status: 401, data: { detail: { code: "unauthenticated" } } });
});

test("a retired private file response is withheld by the same web boundary", async () => {
  const h = browser(), response = deferred<Response>(), entered = deferred<void>();
  await h.login("usr_fixture_a", "csrf-a");
  h.request = async () => { entered.resolve(); return response.promise; };
  const pending = authenticatedFetch("/api/ventures/ven_fixture_a/export"); await entered.promise;
  await h.login("usr_fixture_b", "csrf-b"); response.resolve(new Response("private fixture bytes"));
  const result = await pending;
  assert.equal(result.status, 409); assert.deepEqual(await result.json(), { detail: { code: "session_scope_changed" } });
});

test("a file mutation preflight cannot dispatch after account retirement", async () => {
  const h = browser(), session = deferred<Response>(), entered = deferred<void>();
  await h.login("usr_fixture_a");
  h.session = async () => { entered.resolve(); return session.promise; };
  const pending = authenticatedFetch("/api/ventures/ven_fixture_a/import", { method: "PUT", body: "fixture bytes" }); await entered.promise;
  await h.login("usr_fixture_b", "csrf-b");
  session.resolve(reply({ authenticated: true, subject: "usr_fixture_a", csrf: "old-a" }));
  assert.equal((await pending).status, 409); assert.equal(h.calls.length, 0);
});

test("stable owned file mutations preserve CSRF and transfer response bytes", async () => {
  const h = browser(); await h.login("usr_fixture_a", "csrf-a");
  h.request = async () => new Response("fixture transferred", { status: 201 });
  const result = await authenticatedFetch("/api/ventures/ven_fixture_a/import", { method: "PUT", body: "fixture bytes" });
  assert.equal(result.status, 201); assert.equal(await result.text(), "fixture transferred");
  assert.equal(h.calls[0].csrf, "csrf-a");
});

test("failed file preflight cannot report a successful undispatched upload from a 200 session refusal", async () => {
  const h = browser(); await h.login("usr_fixture_a");
  h.session = async () => reply({ authenticated: false });
  const result = await authenticatedFetch("/api/ventures/ven_fixture_a/import", { method: "PUT", body: "fixture bytes" });
  assert.equal(result.ok, false); assert.equal(result.status, 409); assert.equal(h.calls.length, 0);
  assert.equal(verifiedSessionSubject(), null);
});

test("bridge hydration cannot bypass the custody of a delayed web response", async () => {
  const h = browser(), response = deferred<Response>(), entered = deferred<void>();
  await h.login("usr_fixture_a", "csrf-a");
  h.request = async () => { entered.resolve(); return response.promise; };
  const pending = api("/api/ventures/ven_fixture_a/settings"); await entered.promise;
  await h.login("usr_fixture_b", "csrf-b");
  let nativeCalls = 0;
  Object.assign(window, { __TAURI_INTERNALS__: { invoke: async () => { nativeCalls++; throw new Error("unexpected native effect"); } } });
  response.resolve(reply({ private_fixture: "a-only" }));
  scopeRefused(await pending); assert.equal(nativeCalls, 0);
});

test("a web CSRF retry refuses hydration rather than borrowing native A for browser B", async () => {
  const h = browser(); await h.login("usr_fixture_a", "csrf-a");
  const nativeCalls: Array<{ command: string; path?: unknown }> = [];
  h.request = async () => {
    if (h.calls.length > 1) return reply({ saved: true });
    h.doc.cookie = "cos_csrf=browser-b";
    Object.assign(window, { __TAURI_INTERNALS__: { invoke: async (command: string, args?: Record<string, unknown>) => {
      nativeCalls.push({ command, path: args?.path });
      if (command === "gateway_url") return "https://owned-fixture.invalid";
      return { status: 200, data: { authenticated: true, subject: "usr_fixture_a" } };
    } } });
    return reply({ detail: { code: "csrf_failed" } }, 403);
  };
  scopeRefused(await api(path, mutation));
  assert.equal(h.calls.length, 1); assert.deepEqual(nativeCalls, []);
  assert.equal(verifiedSessionSubject(), "usr_fixture_a");
});

test("a web session reply cannot establish identity after the native bridge appears", async () => {
  const h = browser(), session = deferred<Response>(), entered = deferred<void>();
  h.session = async () => { entered.resolve(); return session.promise; };
  const pending = ensureSession("changeme", true); await entered.promise;
  Object.assign(window, { __TAURI_INTERNALS__: { invoke: async () => { throw new Error("unexpected native effect"); } } });
  session.resolve(reply({ authenticated: true, subject: "usr_fixture_a", csrf: "web-a" }));
  assert.equal((await pending).ok, false); assert.equal(verifiedSessionSubject(), null);
});

function downloadUI() {
  const effects = { objectURLs: 0, clicks: 0, revoked: [] as string[] };
  Object.assign(document, { createElement: () => ({ href: "", download: "", click: () => { effects.clicks++; } }) });
  URL.createObjectURL = () => { effects.objectURLs++; return "blob:owned-fixture"; };
  URL.revokeObjectURL = (url) => { effects.revoked.push(url); };
  return effects;
}

for (const transport of ["web", "native"] as const) {
  for (const transition of ["owner", "ABA", "mode"] as const) {
    test(`${transport} download body retirement (${transition}) cannot create an object URL or click`, async () => {
      const h = browser(); await h.login("usr_fixture_a", "csrf-a");
      let nativeOwner = "usr_fixture_a", nativeTransfers = 0;
      const invoke = async (command: string) => {
        if (command === "gateway_url") return "https://owned-fixture.invalid";
        if (command === "desktop_api_request") return { status: 200, data: { authenticated: true, subject: nativeOwner } };
        assert.equal(command, "desktop_http_request"); nativeTransfers++;
        return { status: 200, bodyBase64: btoa("fixture bytes"), headers: { "content-type": "text/plain" } };
      };
      if (transport === "native") Object.assign(window, { __TAURI_INTERNALS__: { invoke } });
      h.request = async () => new Response("fixture bytes");
      const effects = downloadUI(), body = deferred<Blob>(), entered = deferred<void>();
      Response.prototype.blob = async () => { entered.resolve(); return body.promise; };
      const pending = downloadFile("/api/ventures/ven_fixture_a/export", "fixture.txt"); await entered.promise;
      if (transition === "mode") {
        if (transport === "web") Object.assign(window, { __TAURI_INTERNALS__: { invoke } });
        else delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
      } else if (transport === "web") {
        await h.login("usr_fixture_b", "csrf-b");
        if (transition === "ABA") await h.login("usr_fixture_a", "csrf-new-a");
      } else {
        nativeOwner = "usr_fixture_b"; await ensureSession("changeme", true);
        if (transition === "ABA") { nativeOwner = "usr_fixture_a"; await ensureSession("changeme", true); }
      }
      body.resolve(new Blob(["fixture bytes"]));
      const result = await pending;
      assert.deepEqual({ result, objectURLs: effects.objectURLs, clicks: effects.clicks }, { result: false, objectURLs: 0, clicks: 0 });
      assert.equal(transport === "native" ? nativeTransfers : h.calls.length, 1, "the read was delivered; retirement does not cancel it");
    });
  }
}

test("an unknown download owner refuses before any file request or local download effect", async () => {
  const h = browser(), effects = downloadUI();
  assert.equal(await downloadFile("/api/ventures/ven_fixture_a/export", "fixture.txt"), false);
  assert.equal(h.calls.length, 0); assert.equal(effects.objectURLs, 0); assert.equal(effects.clicks, 0);
});

test("a stable owned download preserves the file handoff", async () => {
  const h = browser(); await h.login("usr_fixture_a", "csrf-a");
  const effects = downloadUI(); h.request = async () => new Response("fixture bytes");
  assert.equal(await downloadFile("/api/ventures/ven_fixture_a/export", "fixture.txt"), true);
  assert.equal(h.calls.length, 1); assert.equal(effects.objectURLs, 1); assert.equal(effects.clicks, 1);
});

test("a stable native download uses its native transfer and preserves the handoff", async () => {
  const h = browser(); await h.login("usr_fixture_a", "csrf-a");
  let nativeTransfers = 0;
  Object.assign(window, { __TAURI_INTERNALS__: { invoke: async (command: string) => {
    assert.equal(command, "desktop_http_request"); nativeTransfers++;
    return { status: 200, bodyBase64: btoa("fixture bytes"), headers: { "content-type": "text/plain" } };
  } } });
  const effects = downloadUI();
  assert.equal(await downloadFile("/api/ventures/ven_fixture_a/export", "fixture.txt"), true);
  assert.equal(nativeTransfers, 1); assert.equal(h.calls.length, 0); assert.equal(effects.objectURLs, 1); assert.equal(effects.clicks, 1);
});

test("retirement during object URL preparation revokes it without creating an anchor", async () => {
  const h = browser(); await h.login("usr_fixture_a", "csrf-a");
  const effects = downloadUI(); h.request = async () => new Response("fixture bytes");
  URL.createObjectURL = () => { effects.objectURLs++; clearVerifiedSession(); return "blob:owned-fixture"; };
  Object.assign(document, { createElement: () => { throw new Error("retired download must not create an anchor"); } });
  assert.equal(await downloadFile("/api/ventures/ven_fixture_a/export", "fixture.txt"), false);
  assert.equal(effects.objectURLs, 1); assert.equal(effects.clicks, 0); assert.deepEqual(effects.revoked, ["blob:owned-fixture"]);
});

test("retirement during local anchor preparation revokes the object URL without clicking", async () => {
  const h = browser(); await h.login("usr_fixture_a", "csrf-a");
  const effects = downloadUI(); h.request = async () => new Response("fixture bytes");
  Object.assign(document, { createElement: () => { clearVerifiedSession(); return { href: "", download: "", click: () => { effects.clicks++; } }; } });
  assert.equal(await downloadFile("/api/ventures/ven_fixture_a/export", "fixture.txt"), false);
  assert.equal(effects.objectURLs, 1); assert.equal(effects.clicks, 0); assert.deepEqual(effects.revoked, ["blob:owned-fixture"]);
});

test("a revoked download stays refused even when local URL cleanup throws", async () => {
  const h = browser(); await h.login("usr_fixture_a", "csrf-a");
  const effects = downloadUI(); h.request = async () => new Response("fixture bytes");
  Object.assign(document, { createElement: () => { clearVerifiedSession(); return { href: "", download: "", click: () => { effects.clicks++; } }; } });
  URL.revokeObjectURL = () => { throw new Error("synthetic cleanup unavailable"); };
  assert.equal(await downloadFile("/api/ventures/ven_fixture_a/export", "fixture.txt"), false);
  assert.equal(effects.objectURLs, 1); assert.equal(effects.clicks, 0);
});
