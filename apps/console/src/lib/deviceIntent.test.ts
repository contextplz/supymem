import assert from "node:assert/strict";
import test from "node:test";
import {
  DEVICE_INTENT_KEY,
  DEVICE_INTENT_TTL_MS,
  afterEmailVerify,
  continueDeviceApproval,
  deviceApproveUrl,
  deviceConsolePath,
  deviceIntentFromParams,
  macProductName,
  parseDeviceIntent,
  persistDeviceIntent,
  readDeviceIntent,
  serializeDeviceIntent,
} from "./deviceIntent.ts";

const valid = {
  code_challenge: "c".repeat(43),
  state: "s".repeat(43),
  device_name: "Founder Mac",
  platform: "macos",
};

function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value); },
    removeItem: (key: string) => { data.delete(key); },
    snapshot: () => Object.fromEntries(data),
  };
}

test("valid Mac connect params persist without tokens and reconstruct the approve path", () => {
  const intent = deviceIntentFromParams(valid);
  assert.ok(intent);
  const storage = memoryStorage();
  const snapshot = { ...intent, issued_at: 1000 };
  assert.deepEqual(persistDeviceIntent(intent, storage, 1000), snapshot);
  assert.equal(Object.keys(storage.snapshot())[0], DEVICE_INTENT_KEY);
  assert.doesNotMatch(storage.snapshot()[DEVICE_INTENT_KEY], /token|password|refresh|secret/i);

  const afterVerify = readDeviceIntent(storage, 1001);
  assert.deepEqual(afterVerify, snapshot);
  const resume = deviceConsolePath(afterVerify!);
  assert.equal(resume.startsWith("/auth/device?"), true);
  assert.deepEqual(deviceIntentFromParams(Object.fromEntries(new URLSearchParams(resume.split("?")[1]))), intent);

  const approve = deviceApproveUrl(afterVerify!, "https://gateway.example");
  const target = new URL(approve);
  assert.equal(target.origin, "https://gateway.example");
  assert.equal(target.pathname, "/auth/device/approve");
  assert.equal(target.searchParams.get("code_challenge"), valid.code_challenge);
  assert.equal(target.searchParams.get("state"), valid.state);
  assert.equal(target.searchParams.has("return_to"), false);
  assert.equal(target.searchParams.has("redirect"), false);
  assert.equal(target.searchParams.has("issued_at"), false, "custody time stays out of the Gateway URL");
});

test("caller-supplied return URLs and secrets never become stored intent", () => {
  assert.equal(deviceIntentFromParams({ ...valid, code_challenge: "short" }), null);
  assert.equal(deviceIntentFromParams({ ...valid, state: "<script>alert(1)</script>" }), null);
  assert.equal(deviceIntentFromParams({ ...valid, device_name: "n".repeat(129) }), null);
  assert.equal(parseDeviceIntent(JSON.stringify({
    version: 1,
    ...valid,
    token: "magic-link-secret",
    redirect: "https://attacker.example",
  })), null);
  assert.equal(serializeDeviceIntent({ version: 1, code_challenge: "nope", device_name: "Mac", platform: "macos" }), null);
});

test("verify resumes stored Mac intent or tells the founder to restart Sign in", () => {
  const storage = memoryStorage();
  persistDeviceIntent(deviceIntentFromParams(valid)!, storage);
  const resumed = afterEmailVerify(readDeviceIntent(storage));
  assert.equal(resumed.path, deviceConsolePath(deviceIntentFromParams(valid)!));
  assert.match(resumed.message, /still needs approval/);
  assert.equal(resumed.actionLabel, "Continue Mac connection");

  const missing = afterEmailVerify(null, "console.staging.supymem.com");
  assert.equal(missing.path, null);
  assert.match(missing.message, /Sign in/);
  assert.match(missing.message, /cannot finish the Mac connection/);
  assert.match(macProductName("app.staging.example"), /Staging/);
  assert.equal(macProductName("app.supymem.com"), "Supymem");
  const hops: string[] = [];
  continueDeviceApproval(readDeviceIntent(storage)!, "https://gw.test", (url) => hops.push(url));
  assert.equal(hops[0], deviceApproveUrl(deviceIntentFromParams(valid)!, "https://gw.test"));
});

test("the active Mac attempt expires with the native window and retries do not renew it", () => {
  const intent = deviceIntentFromParams(valid)!;
  const storage = memoryStorage();
  persistDeviceIntent(intent, storage, 1000);
  assert.deepEqual(persistDeviceIntent(intent, storage, 2000), { ...intent, issued_at: 1000 });
  assert.equal(JSON.parse(storage.snapshot()[DEVICE_INTENT_KEY]).issued_at, 1000);
  for (const path of ["/auth/device", "/auth/device/approve", "/auth/consume", "/auth/cognito/callback"]) {
    assert.deepEqual(readDeviceIntent(storage, 1000 + DEVICE_INTENT_TTL_MS, path), { ...intent, issued_at: 1000 });
  }
  assert.equal(persistDeviceIntent(intent, storage, 1001 + DEVICE_INTENT_TTL_MS), null);
  assert.equal(readDeviceIntent(storage, 1001 + DEVICE_INTENT_TTL_MS), null);
  assert.deepEqual(storage.snapshot(), {});
  const newAttempt = deviceIntentFromParams({ ...valid, state: "n".repeat(43) })!;
  persistDeviceIntent(intent, storage, 1000);
  assert.deepEqual(persistDeviceIntent(newAttempt, storage, 2000), { ...newAttempt, issued_at: 2000 });
  assert.equal(JSON.parse(storage.snapshot()[DEVICE_INTENT_KEY]).issued_at, 2000);
});

test("ordinary sign-in routes abandon the Mac attempt; malformed or legacy storage cannot hijack callbacks", () => {
  const intent = deviceIntentFromParams(valid)!;
  const storage = memoryStorage();
  for (const path of ["/account", "/", "/new", "/auth/cognito/start", "/auth/device/other"]) {
    persistDeviceIntent(intent, storage, 1000);
    assert.equal(readDeviceIntent(storage, 2000, path), null);
    assert.equal(readDeviceIntent(storage, 2001, "/auth/cognito/callback"), null);
    assert.deepEqual(storage.snapshot(), {});
  }
  for (const raw of [serializeDeviceIntent(intent)!, "broken", JSON.stringify({ ...intent, issued_at: 3000 }),
    JSON.stringify({ ...intent, issued_at: -1 }), JSON.stringify({ ...intent, issued_at: 1.5 })]) {
    const invalid = memoryStorage({ [DEVICE_INTENT_KEY]: raw });
    assert.equal(readDeviceIntent(invalid, 2000, "/auth/consume"), null);
    assert.deepEqual(invalid.snapshot(), {});
  }
  const malformed = memoryStorage({ [DEVICE_INTENT_KEY]: "broken" });
  assert.deepEqual(persistDeviceIntent(intent, malformed, 1000), { ...intent, issued_at: 1000 }, "a new valid Mac request can replace corrupt storage");
});

test("Gateway handoff retires the tab-local intent before navigation with exact challenge and state", () => {
  const storage = memoryStorage();
  const intent = persistDeviceIntent(deviceIntentFromParams(valid)!, storage)!;
  const hops: string[] = [];
  continueDeviceApproval(intent, "https://gateway.example", (url) => {
    assert.deepEqual(storage.snapshot(), {});
    hops.push(url);
  }, storage);
  assert.deepEqual(hops, [deviceApproveUrl(intent, "https://gateway.example")]);
  assert.equal(readDeviceIntent(storage), null);
  assert.doesNotThrow(() => readDeviceIntent({ getItem() { throw Error("unavailable"); }, setItem() {}, removeItem() { throw Error("unavailable"); } }));
  assert.equal(persistDeviceIntent(intent, { getItem() { throw Error("unavailable"); }, setItem() {}, removeItem() {} }), null);
});

test("only the direct current URL mode can proceed without storage, and it never revives a known expired attempt", () => {
  const intent = deviceIntentFromParams(valid)!;
  assert.equal(persistDeviceIntent(intent, null, 1000), null);
  assert.deepEqual(persistDeviceIntent(intent, null, 1000, true), { ...intent, issued_at: 1000 });
  const blocked = { getItem() { throw Error("unavailable"); }, setItem() {}, removeItem() {} };
  assert.equal(persistDeviceIntent(intent, blocked, 1000), null);
  assert.deepEqual(persistDeviceIntent(intent, blocked, 1000, true), { ...intent, issued_at: 1000 });
  const storage = memoryStorage(); persistDeviceIntent(intent, storage, 1000);
  assert.equal(persistDeviceIntent(intent, storage, 1001 + DEVICE_INTENT_TTL_MS, true), null);
  assert.deepEqual(storage.snapshot(), {});
});

test("every handoff refuses an expired, future or unaged snapshot without retiring a newer stored attempt", () => {
  const intent = deviceIntentFromParams(valid)!;
  const storage = memoryStorage();
  const newer = persistDeviceIntent({ ...intent, state: "n".repeat(43) }, storage, 301001)!;
  const preserved = storage.snapshot();
  const hops: string[] = [];
  for (const stale of [{ ...intent, issued_at: 1000 }, { ...intent, issued_at: 301002 }, intent]) {
    assert.equal(continueDeviceApproval(stale, "https://gateway.example", url => hops.push(url), storage, 301001), false);
    assert.deepEqual(storage.snapshot(), preserved);
  }
  assert.equal(hops.length, 0);
  assert.equal(continueDeviceApproval(newer, "https://gateway.example", url => hops.push(url), storage, 301001), true);
  assert.equal(hops.length, 1);
});

test("a denied-storage retained snapshot keeps its first issue time and cannot be re-persisted after expiry", () => {
  const intent = deviceIntentFromParams(valid)!;
  const retained = persistDeviceIntent(intent, null, 1000, true)!;
  assert.deepEqual(persistDeviceIntent(retained, null, 2000, true), retained);
  assert.equal(persistDeviceIntent(retained, null, 301001, true), null);
  assert.equal(persistDeviceIntent(retained, null, 999, true), null);
});

test("a failed storage write retains an earlier matching attempt timestamp already read", () => {
  const intent = deviceIntentFromParams(valid)!;
  const storage = { getItem: () => JSON.stringify({ ...intent, issued_at: 1000 }), setItem() { throw Error("denied"); }, removeItem() {} };
  assert.deepEqual(persistDeviceIntent(intent, storage, 200000, true), { ...intent, issued_at: 1000 });
});

test("a fresh superseded snapshot cannot clear newer custody; unavailable custody is left untouched", () => {
  const intent = deviceIntentFromParams(valid)!;
  const original = persistDeviceIntent(intent, null, 1000, true)!;
  const storage = memoryStorage(); persistDeviceIntent({ ...intent, state: "n".repeat(43) }, storage, 2000);
  const preserved = storage.snapshot(); const hops: string[] = [];
  assert.equal(continueDeviceApproval(original, "https://gateway.example", url => hops.push(url), storage, 2001), false);
  assert.deepEqual(storage.snapshot(), preserved); assert.equal(hops.length, 0);
  let removals = 0;
  const unreadable = { getItem() { throw Error("denied"); }, setItem() {}, removeItem() { removals++; } };
  assert.equal(continueDeviceApproval(original, "https://gateway.example", url => hops.push(url), unreadable, 2001), true);
  assert.equal(removals, 0); assert.equal(hops.length, 1);
});
