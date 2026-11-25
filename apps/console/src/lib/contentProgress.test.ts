import assert from "node:assert/strict";
import test from "node:test";
import { runProgress } from "./contentProgress.ts";

test("content run progress never shows internal phase names", () => {
  assert.equal(runProgress({ kind: "draft", state: "running", phase: "critic_3" }), "Checking quality…");
  assert.equal(runProgress({ kind: "draft", state: "running", phase: "draft_1" }), "Writing your draft…");
  assert.equal(runProgress({ kind: "draft", state: "failed", phase: "critic_3" }), "Your draft could not be finished.");
});
