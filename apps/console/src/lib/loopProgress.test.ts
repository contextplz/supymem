import assert from "node:assert/strict";
import test from "node:test";
import { loopIterationLabel, safeLoopText } from "./loopProgress.ts";

test("progress hides raw ids, errors, credentials, absolute paths and URLs", () => {
  for (const value of ["job_example", "dex_example", "Error: failed", "token=private", "sk-pretend", "a".repeat(40)]) {
    assert.equal(safeLoopText(value), "A private detail was removed from this update.");
  }
  assert.equal(safeLoopText("Editing /Users/founder/app.js"), "Editing a local file");
  assert.equal(safeLoopText("Read https://example.test/?credential=private"), "Read a website");
  assert.equal(safeLoopText("Editing tests/totals.test.js"), "Editing tests/totals.test.js");
  assert.equal(safeLoopText("Checking your app (try 3 of 6)"), "Checking your app (try 3 of 6)");
  assert.equal(safeLoopText(null), "");
  assert.equal(safeLoopText("x ".repeat(200)).length, 200);
});
test("iterations show a bounded try number and plain outcomes", () => {
  assert.equal(loopIterationLabel({ index: 2, phase: "code", outcome: "tests_failed" }, 6), "Try 3 of 6: Checks still failing, fixing");
  assert.equal(loopIterationLabel({ index: 0, phase: "plan", outcome: "pass" }, 6), "Plan: Local checks passed");
  assert.equal(loopIterationLabel({ index: 42, phase: "code", outcome: "Error" }, 6), "Try 6 of 6: Review the saved result");
});
