import assert from "node:assert/strict";
import test from "node:test";
import { clarificationAnswered, clarificationPayload, selectAllClarificationOptions, toggleClarificationOption } from "./clarificationSelection.ts";
import type { ClarifyQuestion } from "./api-types.ts";

const q: ClarifyQuestion = { id: "q1", selection_mode: "multiple", exclusive_option_ids: ["unknown"], options: [
  { id: "workaround", label: "Workaround" }, { id: "paid", label: "Paid product" }, { id: "unknown", label: "Unknown" },
] };

test("multi-select preserves two choices, supports edit, and excludes unknown", () => {
  let a = toggleClarificationOption(q, undefined, "workaround");
  a = toggleClarificationOption(q, a, "paid");
  assert.deepEqual(a.option_ids, ["workaround", "paid"]);
  assert.equal(clarificationAnswered(q, a), true);
  assert.deepEqual(clarificationPayload(q, a).option_ids, ["workaround", "paid"]);
  a = toggleClarificationOption(q, a, "unknown");
  assert.deepEqual(a.option_ids, ["unknown"]);
  a = toggleClarificationOption(q, a, "workaround");
  assert.deepEqual(a.option_ids, ["workaround"]);
  assert.deepEqual(selectAllClarificationOptions(q).option_ids, ["workaround", "paid"]);
});

test("single-select replaces prior choice and requires useful other text", () => {
  const single = { ...q, selection_mode: "single" as const };
  assert.deepEqual(toggleClarificationOption(single, { option_id: "workaround" }, "paid"), { option_id: "paid" });
  assert.equal(clarificationAnswered(single, { option_id: "other", other_text: "no" }), false);
  assert.equal(clarificationAnswered(single, { option_id: "other", other_text: "agency" }), true);
});

test("saved answers must still match the question before Continue is enabled", () => {
  const single = { ...q, selection_mode: "single" as const };
  assert.equal(clarificationAnswered(single, { option_id: "unknown-id" }), false);
  assert.equal(clarificationAnswered(single, { option_ids: ["workaround", "paid"] }), false);
  assert.equal(clarificationAnswered(single, { option_id: "workaround", other_text: "agency" }), false);
  assert.equal(clarificationAnswered(q, { option_ids: ["unknown", "paid"] }), false);
  assert.equal(clarificationAnswered(q, { option_ids: ["workaround", "workaround"] }), false);
  assert.equal(clarificationAnswered(q, { option_ids: ["paid"], other_text: " " }), false);
  assert.equal(clarificationAnswered(q, { option_ids: ["paid"], other_text: "no" }), false);
  assert.equal(clarificationAnswered({ ...q, allow_other: false }, { other_text: "agency" }), false);
});
