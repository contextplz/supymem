import assert from "node:assert/strict";
import test from "node:test";
import { milestoneStatus, oneLine, planProgress, shortTitle } from "./planTimeline.ts";

test("shortTitle caps at six words", () => {
  assert.equal(shortTitle("Build and launch the public waitlist page today"), "Build and launch the public waitlist…");
  assert.equal(shortTitle("Launch waitlist"), "Launch waitlist");
});
test("oneLine keeps the first sentence and caps length", () => {
  assert.equal(oneLine("First one. Second one."), "First one.");
  assert.equal(oneLine("x".repeat(300), 10), "xxxxxxxxx…");
});
test("milestoneStatus and planProgress", () => {
  const done = { included: true, tasks: [{ id: "a", state: "done" }] };
  const cur = { included: true, tasks: [{ id: "b", state: "pending" }] };
  const later = { included: true, tasks: [{ id: "c", state: "pending" }] };
  assert.equal(milestoneStatus(done, "b"), "done");
  assert.equal(milestoneStatus(cur, "b"), "active");
  assert.equal(milestoneStatus(later, "b"), "upcoming");
  assert.equal(milestoneStatus({ ...later, included: false }, "c"), "excluded");
  assert.equal(planProgress([done, cur]), 50);
  assert.equal(planProgress([]), 0);
});
