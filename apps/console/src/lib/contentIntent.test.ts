import assert from "node:assert/strict";
import test from "node:test";
import { contentIntentOwner, contentIntentKey, saveContentIntent, readContentIntent, contentRequestFence, contentRejectedBeforeEnqueue } from "./contentIntent.ts";

const alice = { actorId: "usr_alice", workspaceId: "ws_shared", ventureId: "ven_shared" };
const bob = { ...alice, actorId: "usr_bob" };
const envelope = { venture_id: alice.ventureId, kind: "topics", idempotency_key: "approved-intent", input_digest: "approved-quote" };

test("lost-response recovery belongs to the approving member, even in a shared company", () => {
  const storage = new Map([[contentIntentKey(alice), saveContentIntent(alice, envelope)]]);
  assert.notEqual(contentIntentKey(alice), contentIntentKey(bob));
  assert.equal(storage.get(contentIntentKey(bob)), undefined);
  assert.equal(readContentIntent(saveContentIntent(alice, envelope), bob), null);
  assert.deepEqual(readContentIntent(storage.get(contentIntentKey(alice))!, alice), envelope);
});

test("anonymous, legacy unowned, corrupt and different company receipts cannot authorize recovery", () => {
  assert.equal(contentIntentOwner({ workspaceId: alice.workspaceId, ventureId: alice.ventureId }), null);
  assert.equal(contentIntentKey(null), "");
  for (const raw of [JSON.stringify(envelope), "bad json", "null", saveContentIntent(alice, { ...envelope, venture_id: "ven_other" })]) {
    assert.equal(readContentIntent(raw, alice), null);
  }
  assert.equal(readContentIntent(saveContentIntent(alice, envelope), { ...alice, workspaceId: "ws_other" }), null);
});

test("late quote or submission responses remain fenced after logout/login to the same owner", async () => {
  const fence = contentRequestFence();
  fence.activate(contentIntentKey(alice));
  const current = fence.capture();
  let finish!: () => void;
  let applied = false;
  const pending = new Promise<void>(resolve => { finish = resolve; }).then(() => { if (current()) applied = true; });
  fence.invalidate();
  fence.activate(contentIntentKey(bob));
  assert.equal(current(), false);
  fence.activate(contentIntentKey(alice));
  finish(); await pending;
  assert.equal(applied, false);
  assert.equal(fence.capture()(), true);
});

test("unmount and missing authenticated identity stop further polling or approval work", () => {
  const fence = contentRequestFence();
  fence.activate(contentIntentKey(alice));
  const current = fence.capture();
  fence.invalidate();
  assert.equal(current(), false);
  assert.equal(fence.capture()(), false);
  fence.activate("");
  assert.equal(fence.capture()(), false);
});

test("only definite pre-enqueue rejection releases a saved generation approval", () => {
  const definite = ["content_quote_changed", "content_insufficient_credits"];
  for (const code of definite) {
    assert.equal(contentRejectedBeforeEnqueue({ status: 409, detail: { code } }), true);
  }
  for (const error of [
    new Error("Supymem could not connect"),
    { status: 401 }, { status: 403 }, { status: 422 },
    { status: 409, detail: { code: "content_idempotency_conflict" } },
    { status: 409, detail: { code: "content_generation_unavailable" } },
    { status: 409, detail: { code: "content_quote_changed: unsafe detail" } },
  ]) {
    assert.equal(contentRejectedBeforeEnqueue(error), false);
    assert.deepEqual(readContentIntent(saveContentIntent(alice, envelope), alice), envelope);
  }
});
