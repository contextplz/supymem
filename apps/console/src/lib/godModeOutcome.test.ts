import assert from "node:assert/strict";
import test from "node:test";
import { godModeOutcomeLabel } from "./godModeOutcome.ts";
test("only an exact new completed receipt carries the verified label", () => {
  assert.equal(godModeOutcomeLabel({ outcome: "completed", completion_verified: true }), "Verified effect saved");
  for (const receipt of [{ outcome: "completed" }, { outcome: "completed", completion_verified: false },
    { outcome: "done", display_outcome: "completed", completion_verified: true }, { outcome: "done" },
    { outcome: "prepared", display_outcome: "completed", completion_verified: true }])
    assert.equal(godModeOutcomeLabel(receipt), "Recorded activity (completion unverified)");
  assert.equal(godModeOutcomeLabel({ outcome: "future" }), "Recorded activity (status unconfirmed)");
});
