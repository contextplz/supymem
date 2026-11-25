import assert from "node:assert/strict";
import test from "node:test";
import { approveDiagnosis, diagnosisActive, diagnosisNeedsResolution, gtmIntentKey, gtmRequestFence,
  readGtmIntent, saveGtmIntent, persistDiagnosisIntent, type DiagnosisInput, type DiagnosisQuote, type DiagnosisRun } from "./gtmDiagnosisIntent.ts";

const owner = { actorId: "alice", workspaceId: "workspace-one", ventureId: "venture-one" };
const input: DiagnosisInput = { kind: "diagnose", idea: "A useful product", problem: "An expensive task", who: "Teams", users_count: 0 };
const quote: DiagnosisQuote = { provider: "provider", model: "model", payer: "platform_credits", input_digest: "a".repeat(64),
  resource_digest: "b".repeat(64), ceiling_micro: 14000, call_budget: 1, max_output_tokens: 8192,
  available_micro: 15000, sufficient_now: true, basis: "quoted", input_provenance: "founder_reported", operations: ["diagnosis:1"] };

test("approved input, scope, budget and key survive lost response/restart unchanged", () => {
  const original = { ...input };
  const intent = approveDiagnosis(owner, original, quote, "approved-key");
  original.idea = "Changed input needs another quote";
  const restored = readGtmIntent(saveGtmIntent(intent), owner);
  assert.deepEqual(restored, intent);
  assert.equal(restored?.request.idea, input.idea);
  assert.equal(restored?.idempotencyKey, "approved-key");
  assert.equal(restored?.request.ceiling_micro, quote.ceiling_micro);
});

test("another actor, workspace or venture cannot recover a saved approval", () => {
  const receipt = saveGtmIntent(approveDiagnosis(owner, input, quote, "approved-key"));
  for (const other of [{ ...owner, actorId: "bob" }, { ...owner, workspaceId: "other" }, { ...owner, ventureId: "other" }]) {
    assert.notEqual(gtmIntentKey(other), gtmIntentKey(owner));
    assert.equal(readGtmIntent(receipt, other), null);
  }
  assert.equal(readGtmIntent(receipt, null), null);
});

test("unknown credits and corrupt/unowned receipts never authorize a replacement run", () => {
  for (const bad of [{ ...quote, sufficient_now: false }, { ...quote, available_micro: null },
    { ...quote, ceiling_micro: -1 }, { ...quote, input_digest: "missing" }, { ...quote, available_micro: 1 }]) {
    assert.throws(() => approveDiagnosis(owner, input, bad, "approved-key"));
  }
  assert.doesNotThrow(() => approveDiagnosis(owner, input, { ...quote, payer: "founder", available_micro: null, ceiling_micro: 0 }, "approved-key"));
  for (const raw of ["null", "{", JSON.stringify(input), JSON.stringify({ version: 1, owner, request: {} })]) {
    assert.equal(readGtmIntent(raw, owner), null);
  }
});

test("account switches invalidate delayed quote/start/poll tickets even after returning to same actor", async () => {
  const fence = gtmRequestFence(); fence.activate(gtmIntentKey(owner));
  const current = fence.capture();
  let finish!: () => void;
  let applied = false;
  const delayed = new Promise<void>(resolve => { finish = resolve; }).then(() => { if (current()) applied = true; });
  fence.invalidate(); fence.activate(gtmIntentKey({ ...owner, actorId: "bob" })); fence.activate(gtmIntentKey(owner));
  finish(); await delayed;
  assert.equal(applied, false);
  assert.equal(current(), false);
  assert.equal(fence.capture()(), true);
  fence.invalidate(); assert.equal(fence.capture()(), false);
});

test("partial errors and unsettled usage remain unresolved; completion is diagnosis-only", () => {
  const run = { state: "failed", reserved_micro: 10 } as DiagnosisRun;
  assert.equal(diagnosisActive(run), false);
  assert.equal(diagnosisNeedsResolution(run), true);
  assert.equal(diagnosisNeedsResolution({ ...run, state: "needs_input", reserved_micro: 0 }), true);
  assert.equal(diagnosisNeedsResolution({ ...run, state: "completed", reserved_micro: 0 }), false);
});


test("approval is durable before the first request; blocked storage never sends", async () => {
  const intent = approveDiagnosis(owner, input, quote, "approved-key");
  const storage = new Map<string, string>();
  let calls = 0;
  const send = async (value: typeof intent) => {
    calls++;
    assert.deepEqual(readGtmIntent(storage.get(gtmIntentKey(owner))!, owner), value);
    throw new Error("response lost");
  };
  await assert.rejects(persistDiagnosisIntent(intent, { setItem: (key, value) => { storage.set(key, value); } }, send), /response lost/);
  const restored = readGtmIntent(storage.get(gtmIntentKey(owner))!, owner)!;
  assert.equal(restored.idempotencyKey, intent.idempotencyKey);
  await assert.rejects(persistDiagnosisIntent(restored, { setItem: () => { throw new Error("storage blocked"); } }, send), /storage blocked/);
  assert.equal(calls, 1);
});


test("proven pre-call terminal failure permits a fresh quote; zero-spend uncertainty does not", () => {
  const rejected = { state: "failed", calls_used: 0, consumed_micro: 0, reserved_micro: 0,
    error_code: "gtm_resources_changed" } as DiagnosisRun;
  assert.equal(diagnosisNeedsResolution(rejected), false);
  assert.equal(diagnosisNeedsResolution({ ...rejected, reserved_micro: 10 }), true);
  assert.equal(diagnosisNeedsResolution({ ...rejected, state: "needs_input", error_code: "gtm_outcome_uncertain" }), true);
  const fresh = approveDiagnosis(owner, input, quote, "fresh-approved-key");
  assert.equal(fresh.idempotencyKey, "fresh-approved-key");
});
