import assert from "node:assert/strict";
import { test } from "node:test";
import { founderExperimentContext, personalProjectReview } from "./validationNextTest.ts";

test("suggested experiment uses only complete saved founder answers", () => {
  assert.deepEqual(founderExperimentContext({ problem: "  Lost customer requests  ", customer: "Consultants", offer: "A request tracker", market_claim: "proven demand" }),
    { problem: "Lost customer requests", customer: "Consultants", offer: "A request tracker" });
  assert.equal(founderExperimentContext({ problem: "Lost requests", customer: "Consultants" }), null);
  assert.equal(founderExperimentContext({ problem: ["untrusted"], customer: "Consultants", offer: "Tracker" }), null);
});

test("personal review uses only saved project inputs rather than old startup validation prose", () => {
  assert.deepEqual(personalProjectReview({
    desired_outcome: "  Sort my saved web pages by topic  ", offer: "A private bookmark sorter",
    validation_summary: "No funding potential", market_claim: "Weak market score",
  }), { goal: "Sort my saved web pages by topic", firstVersion: "A private bookmark sorter" });
  assert.deepEqual(personalProjectReview({ problem: "Find my own photos" }), {
    goal: "Find my own photos", firstVersion: null,
  });
  assert.deepEqual(personalProjectReview({}, "  Keep my notes searchable  "), {
    goal: "Keep my notes searchable", firstVersion: null,
  });
});
