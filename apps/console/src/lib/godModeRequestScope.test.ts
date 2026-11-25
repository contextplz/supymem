import assert from "node:assert/strict";
import test from "node:test";
import { createGodModeRequestFence, sameGodModeScope } from "./godModeRequestScope.ts";

const scope = { venture: "venture-a", owner: "account-a", epoch: 1 };

test("late poll cannot undo the newer observed mutation", async () => {
  const fence = createGodModeRequestFence();
  const read = fence.begin(scope);
  const write = fence.begin(scope);
  let rendered = "unknown";
  if (fence.current(write, scope)) rendered = "off";
  await Promise.resolve();
  if (fence.current(read, scope)) rendered = "on";
  assert.equal(rendered, "off");
});

test("old venture/account responses and receipts cannot populate the current settings", () => {
  const fence = createGodModeRequestFence();
  const old = fence.begin(scope);
  for (const next of [{ ...scope, venture: "venture-b" }, { ...scope, owner: "account-b" }, { ...scope, owner: null }]) {
    assert.equal(fence.current(old, next), false);
    assert.equal(sameGodModeScope(old, next), false);
  }
  assert.equal(sameGodModeScope(null, scope), false);
});

test("sign-out then same-account sign-in cannot revive a prior response", () => {
  const fence = createGodModeRequestFence();
  const old = fence.begin(scope);
  fence.invalidate();
  assert.equal(fence.current(old, scope), false);
  const fresh = fence.begin(scope);
  assert.equal(fence.current(fresh, scope), true);
});

test("new read retires old read while scoped saved receipts remain usable", () => {
  const fence = createGodModeRequestFence();
  const first = fence.begin(scope), next = fence.begin(scope);
  assert.equal(fence.current(first, scope), false);
  assert.equal(fence.current(next, scope), true);
  assert.equal(sameGodModeScope(scope, scope), true);
});

test("returning to an actor cannot revive its earlier receipt generation", () => {
  const later = { ...scope, epoch: 3 };
  assert.equal(sameGodModeScope(scope, later), false);
  const fence = createGodModeRequestFence(), before = fence.begin(scope);
  assert.equal(fence.current(before, later), false);
});
