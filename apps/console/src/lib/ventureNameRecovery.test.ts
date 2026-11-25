import assert from "node:assert/strict";
import { test } from "node:test";
import { startVentureNameRecovery } from "./ventureNameRecovery.ts";

const settle = async () => { await Promise.resolve(); await Promise.resolve(); };
function harness() {
  let owner: string | null = "account_a";
  const names: string[] = [], paths: string[] = [], delays: number[] = [];
  const callbacks: ((result: { ok: boolean; data: unknown }) => void)[] = [];
  let scheduled: (() => void) | undefined;
  const recovery = startVentureNameRecovery("ven_a", {
    scope: () => owner, accept: name => names.push(name),
    read: async (path, onData) => { paths.push(path); callbacks.push(onData); },
    schedule: (callback, delay) => { delays.push(delay); scheduled = callback; return 1 as unknown as ReturnType<typeof setTimeout>; },
    cancelTimer: () => { scheduled = undefined; },
  });
  return { recovery, names, paths, callbacks, delays, scope: (value: string | null) => { owner = value; },
    tick: async () => { const callback = scheduled; scheduled = undefined; callback?.(); await settle(); } };
}

test("a transient missing-name read recovers through scoped GETs and stops retrying", async () => {
  const h = harness();
  await h.recovery.refresh();
  h.callbacks[0]({ ok: false, data: null });
  await h.tick();
  h.callbacks[1]({ ok: true, data: { id: "ven_a", name: " Saved name " } });
  await h.tick();
  assert.deepEqual(h.names, ["Saved name"]);
  assert.deepEqual(h.paths, ["/api/ventures/ven_a", "/api/ventures/ven_a"]);
  assert.deepEqual(h.delays, [1000, 3000]);
});

test("navigation or saved rename wins over an unresolved old name response", async () => {
  const h = harness();
  await h.recovery.refresh();
  h.recovery.stop();
  h.callbacks[0]({ ok: true, data: { id: "ven_a", name: "Old name" } });
  await h.tick();
  await h.recovery.refresh();
  assert.deepEqual(h.names, []);
  assert.equal(h.paths.length, 1);
});

test("account change rejects old responses and recovers only in the new account scope", async () => {
  const h = harness();
  await h.recovery.refresh();
  h.scope("account_b");
  h.callbacks[0]({ ok: true, data: { id: "ven_a", name: "Private old name" } });
  assert.deepEqual(h.names, []);
  await h.tick();
  h.callbacks[1]({ ok: true, data: { id: "ven_a", name: "New account name" } });
  assert.deepEqual(h.names, ["New account name"]);
});

test("unverified identity and mismatched venture payload cannot resolve an editable name", async () => {
  const h = harness();
  h.scope(null);
  await h.recovery.refresh();
  h.callbacks[0]({ ok: true, data: { id: "ven_a", name: "Unverified" } });
  h.scope("account_a");
  await h.tick();
  h.callbacks[1]({ ok: true, data: { id: "ven_b", name: "Different venture" } });
  await h.tick();
  h.callbacks[2]({ ok: true, data: { id: "ven_a", name: "   " } });
  assert.deepEqual(h.names, []);
});

test("unresolved-name backoff stays bounded and concurrent wakeups never duplicate requests", async () => {
  const h = harness();
  await h.recovery.refresh();
  for (let i = 0; i < 8; i++) await h.tick();
  assert.deepEqual(h.delays, [1000, 3000, 8000, 15000, 30000, 30000, 30000, 30000, 30000]);
  h.recovery.stop();
  let finish!: () => void;
  let reads = 0;
  const recovery = startVentureNameRecovery("ven_a", {
    scope: () => "account_a", accept: () => {},
    read: async () => { reads++; await new Promise<void>(resolve => { finish = resolve; }); },
  });
  const pending = recovery.refresh();
  await recovery.refresh();
  assert.equal(reads, 1);
  recovery.stop();
  finish();
  await pending;
});
