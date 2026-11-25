import assert from "node:assert/strict";
import test from "node:test";
import { planningFailureExplanation } from "./planningFailure.ts";

test("reopened failed plan explains its saved invalid-output receipt without raw details", () => {
  const restored = JSON.parse(JSON.stringify({ generation_state: "failed", usage: { credits_micro: 701900,
    has_unsettled: false, operations: [{ error_code: "plan_invalid_output", private_detail: "private schema data" }] } }));
  const message = planningFailureExplanation(restored)!;
  assert.match(message, /no executable plan was saved/);
  assert.match(message, /usage remains charged/);
  assert.match(message, /No automatic retry/);
  assert.match(message, /explicitly approve/);
  assert.doesNotMatch(message, /private|schema|target|plan_invalid_output/);
  assert.equal(restored.usage.credits_micro, 701900);
});

test("only an exact persisted failed-plan code produces this explanation", () => {
  assert.equal(planningFailureExplanation(null), null);
  for (const state of ["completed", "running", "queued"]) {
    assert.equal(planningFailureExplanation({ generation_state: state, usage: { credits_micro: 0, has_unsettled: false,
      operations: [{ error_code: "plan_invalid_output" }] } }), null);
  }
  for (const operations of [undefined, [], [{ error_code: "private provider message" }], [{ error_code: "plan_invalid_output: private" }]]) {
    assert.equal(planningFailureExplanation({ generation_state: "failed", usage: { credits_micro: 0, has_unsettled: false, operations } }), null);
  }
});

test("a timed-out plan attempt explains the timeout and the explicit retry", () => {
  const message = planningFailureExplanation({ generation_state: "failed", usage: { credits_micro: 0, has_unsettled: true,
    operations: [{ error_code: "plan_invalid_output" }, { error_code: "plan_outcome_uncertain" }] } })!;
  assert.match(message, /took too long/);
  assert.match(message, /answers and research are kept/);
  assert.match(message, /No automatic retry/);
  assert.equal(planningFailureExplanation({ generation_state: "failed", usage: { credits_micro: 0, has_unsettled: false,
    operations: [{ error_code: "constructor" }] } }), null);
});
