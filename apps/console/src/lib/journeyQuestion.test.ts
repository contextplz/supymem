import assert from "node:assert/strict";
import test from "node:test";
import { pendingJourneyQuestion } from "./journeyQuestion.ts";

const question = { role: "agent", kind: "question" };
test("restored clarification shows only an unanswered current question", () => {
  assert.equal(pendingJourneyQuestion("clarifying", [question]), question);
  assert.equal(pendingJourneyQuestion("clarifying", [question, { role: "founder", kind: "answer" }]), undefined);
  assert.equal(pendingJourneyQuestion("clarifying", [question, { role: "agent", kind: "proposal" }]), undefined);
});
test("a GStack office-hours question is pending while validating", () => {
  const gstack = { role: "agent", kind: "question", options: { phase: "gstack", options: ["a", "b"] } };
  assert.equal(pendingJourneyQuestion("validating", [gstack]), gstack);
  assert.equal(pendingJourneyQuestion("validating", [gstack, { role: "founder", kind: "answer" }]), undefined);
});
test("historical questions cannot hide validation or confirmed research", () => {
  for (const state of ["validating", "reviewing", "confirmed", "abandoned"]) {
    assert.equal(pendingJourneyQuestion(state, [question]), undefined);
  }
});
