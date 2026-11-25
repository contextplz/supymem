import assert from "node:assert/strict";
import test from "node:test";

/** Sticky review actions must stay above the fold after Confirm. */
export function reviewActionsShouldStick(windowHeight: number, actionTop: number, actionHeight: number): boolean {
  if (windowHeight <= 0 || actionHeight <= 0) return false;
  return actionTop + actionHeight > windowHeight;
}

test("review action bar needs sticky placement when Start sits below a 744px window", () => {
  assert.equal(reviewActionsShouldStick(744, 774, 48), true);
  assert.equal(reviewActionsShouldStick(744, 600, 48), false);
});
