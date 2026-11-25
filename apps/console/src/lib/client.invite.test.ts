import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { clearVerifiedSession, loginAccount, signup, startCognitoLogin, completeCognitoLogin, consumeMagicLink, logout, requestPasswordReset } from "./client.ts";

const originals = { window: globalThis.window, storage: globalThis.sessionStorage, fetch: globalThis.fetch };
afterEach(() => {
  clearVerifiedSession();
  Object.defineProperty(globalThis, "window", { configurable: true, value: originals.window });
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: originals.storage });
  globalThis.fetch = originals.fetch;
});
function browser(denied = true) {
  const saved = new Map<string, string>();
  const storage = { getItem: (key: string) => { if (denied) throw new Error("denied"); return saved.get(key) || null; },
    setItem: (key: string, value: string) => { if (denied) throw new Error("denied"); saved.set(key, value); },
    removeItem: (key: string) => { if (denied) throw new Error("denied"); saved.delete(key); } };
  Object.defineProperty(globalThis, "window", { configurable: true, value: { sessionStorage: storage,
    location: { origin: "https://app.supymem.com" }, dispatchEvent: () => true } });
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: storage });
  const sent: Array<{ path: string; body: Record<string, unknown> }> = [];
  globalThis.fetch = async (url, init) => {
    sent.push({ path: String(url), body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify({ authenticated: true, subject: "usr_joined", csrf: "csrf-test" }), { status: 200 });
  };
  return { saved, sent };
}

test("password recovery posts only email and preserves generic success and mail-unavailable responses", async () => {
  browser();
  for (const status of [200, 503]) {
    const data = status === 200 ? { ok: true } : { detail: { code: "auth_mail_unavailable" } };
    globalThis.fetch = async (url, init) => {
      assert.equal(new URL(String(url)).pathname, "/auth/password-reset");
      assert.equal(init?.method, "POST");
      assert.equal(init?.credentials, "include");
      assert.deepEqual(JSON.parse(String(init?.body)), { email: "founder@example.test" });
      return new Response(JSON.stringify(data), { status });
    };
    const result = await requestPasswordReset("founder@example.test");
    assert.equal(result.ok, status === 200);
    assert.deepEqual(result.data, data);
  }
});

test("auth rate limits retain the session and expose friendly retry time", async () => {
  browser();
  await loginAccount("founder@example.test", "synthetic");
  globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: "rate_limited", retry_after: 37 } }), { status: 429, headers: { "Retry-After": "37" } });
  const result = await requestPasswordReset("founder@example.test");
  assert.equal(result.status, 429);
  assert.equal(result.error, "You're going a bit fast - try again in 37 seconds");
});

test("typed invitation reaches password and OAuth start flows despite storage denial", async () => {
  const h = browser();
  assert.equal((await loginAccount("a@example.test", "dummy", "INVITE22")).ok, true);
  assert.equal((await signup("b@example.test", "dummy", "INVITE33")).ok, true);
  assert.equal((await startCognitoLogin("google", "INVITE44")).ok, true);
  assert.deepEqual(h.sent.map(request => request.body.invite_code), ["INVITE22", "INVITE33", "INVITE44"]);
});

test("explicit empty invitation never revives a stale previous member code", async () => {
  const h = browser(false);
  h.saved.set("supymem.invite", "OLDUSER2");
  await loginAccount("a@example.test", "dummy", "");
  assert.equal(h.sent[0].body.invite_code, "");
  assert.equal(h.saved.has("supymem.invite"), false);
  h.saved.set("supymem.invite", "OLDUSER3");
  await logout();
  assert.equal(h.saved.has("supymem.invite"), false);
  await signup("b@example.test", "dummy", "NEWUSER2");
  assert.equal(h.sent.findLast(item => item.path.endsWith("/auth/signup"))?.body.invite_code, "NEWUSER2");
});

test("lost reload state sends no invented invitation and preserves truthful server refusal", async () => {
  const h = browser();
  globalThis.fetch = async (url, init) => {
    h.sent.push({ path: String(url), body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify({ detail: { code: "invite_code_required" } }), { status: 403 });
  };
  const result = await consumeMagicLink("dummy-token");
  assert.equal(result.ok, false);
  assert.equal((result.data as { detail: { code: string } }).detail.code, "invite_code_required");
  assert.equal(h.sent[0].body.invite_code, undefined);
  await completeCognitoLogin("authorization-code", "state");
  assert.equal(h.sent[1].body.invite_code, undefined);
});
