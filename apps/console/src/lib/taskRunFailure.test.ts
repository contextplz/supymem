import assert from "node:assert/strict";
import test from "node:test";
import { taskRunFailure } from "./taskRunFailure.ts";

test("unconfirmed run explains itself and offers a retry", () => {
  for (const code of ["request_outcome_unknown", ""]) {
    const f = taskRunFailure(0, code, "request outcome unknown");
    assert.equal(f.unconfirmed, true);
    assert.doesNotMatch(f.message, /outcome unknown/);
    assert.match(f.message, /press Run/);
  }
});

test("server messages and paused ventures keep a next action", () => {
  assert.equal(taskRunFailure(422, "x", "Plan not approved.").message, "Plan not approved.");
  assert.match(taskRunFailure(409, "venture_paused", "").message, /Resume/);
  assert.match(taskRunFailure(500, "", "").message, /try again/);
});
