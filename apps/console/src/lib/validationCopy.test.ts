import assert from "node:assert/strict";
import test from "node:test";
import { cleanAssessment, founderGaps, founderReviewText, forcingQuestions, splitQuestions } from "./validationCopy.ts";

test("hides no-memory note and strips jargon lead", () => {
  assert.equal(cleanAssessment("No scoped GBrain memory was provided; no memory-based synthesis was performed."), "");
  assert.equal(cleanAssessment("Prime directives applied to the envisioned reminder system: Zero silent failures."), "Zero silent failures.");
});

test("splits founder questions", () => {
  const items = splitQuestions("Q1 Demand Reality: none yet. Q2 Status Quo: texts.");
  assert.deepEqual(items, [
    { label: "Customer demand:", body: "none yet." },
    { label: "Current workarounds:", body: "texts." },
  ]);
});

test("U57 evidence hints and saved review gaps use plain names without claiming verified demand", () => {
  assert.equal(founderReviewText("GStack looks for: Specific behavior. Someone paying…"), "Strong evidence looks like: Specific behavior. Someone paying…");
  assert.deepEqual(founderGaps(["Q1 demand reality is unverified", "q1_demand_reality_unverified"]), ["Customer demand is unverified", "Customer demand unverified."]);
  assert.equal(cleanAssessment("Q2 Status Quo: no interviews yet."), "Current workarounds: no interviews yet.");
  assert.equal(founderReviewText("Your GStack review is back."), "Your idea review is back.");
});

test("structured forcing questions replace parsed Q strings, and old rows have none", () => {
  const item = { question_id: "Q1", question: "Who pays?", founder_answer: "One bakery", assessment: "a", risk: "r", suggested_test: "t" };
  assert.deepEqual(forcingQuestions({ forcing_questions: [item] }), [item]);
  assert.deepEqual(forcingQuestions({ forcing_questions: [{ ...item, question: "" }] }), []);
  assert.deepEqual(forcingQuestions({}), []);
  assert.deepEqual(forcingQuestions(undefined), []);
  assert.equal(splitQuestions("Q1 Demand Reality: none.").length, 1); // legacy rows still parse
});

test("founderGaps hides the internal no-memory gap", () => {
  assert.deepEqual(founderGaps(["No scoped GBrain memory was provided.", "Pricing is unverified."]), ["Pricing is unverified."]);
  assert.deepEqual(founderGaps(undefined), []);
});

test("founderGaps never shows raw snake_case codes", () => {
  assert.deepEqual(founderGaps(["no_memory_gap"]).length, 1);
  assert.ok(!/_/.test(founderGaps(["no_memory_gap", "pricing_unverified"]).join(" ")));
  assert.deepEqual(founderGaps(["No_customer_conversation", "no_customer_conversation"]),
    ["No customer conversation recorded yet.", "No customer conversation recorded yet."]);
});
