import assert from "node:assert/strict";
import test from "node:test";
import { editableFindingPath, groupPlanFindings, planReviewAction, type PlanReviewState } from "./planReview.ts";

const ready: PlanReviewState = {
  state: "founder_review", generation_state: "completed", reviewable: true,
  stale_dependencies: false, review_findings: [],
};

test("only a clean, current, reviewable draft offers approval", () => {
  assert.equal(planReviewAction(ready), "review");
  assert.equal(planReviewAction({ ...ready, review_findings: [{ code: "conflict", path: "milestones.0", message: "Conflicting scope" }] }), "revise");
  assert.equal(planReviewAction({ ...ready, stale_dependencies: true }), "revise");
  assert.equal(planReviewAction({ ...ready, newer_plan_id: "new" }), "revise");
  assert.equal(planReviewAction({ ...ready, reviewable: false }), "revise");
  assert.equal(planReviewAction({ ...ready, generation_state: "failed" }), "revise");
  assert.equal(planReviewAction({ ...ready, state: "approved" }), "approved");
});

test("repeated server findings appear once while their locations remain available", () => {
  const grouped = groupPlanFindings([
    { code: "price", path: "proposal.summary", message: "Choose a price." },
    { code: "price", path: "proposal.milestones[0]", message: "Choose a price." },
    { code: "scope", path: "proposal.exclusions[0]", message: "Resolve scope." },
  ]);
  assert.deepEqual(grouped, [
    { code: "price", message: "Choose a price.", paths: ["proposal.summary", "proposal.milestones[0]"] },
    { code: "scope", message: "Resolve scope.", paths: ["proposal.exclusions[0]"] },
  ]);
});

test("finding locations resolve to their exact editable field or multiline list", () => {
  const fields = ["proposal.summary", "proposal.milestones[4].tasks[2].description",
    "proposal.milestones[4].tasks[2].acceptance", "proposal.milestones[4].tasks[2].founder_action.steps"];
  assert.equal(editableFindingPath("proposal.milestones[4].tasks[2].description", fields), fields[1]);
  assert.equal(editableFindingPath("proposal.milestones[4].tasks[2].acceptance[1]", fields), fields[2]);
  assert.equal(editableFindingPath("proposal.milestones[4].tasks[2].founder_action.steps[0]", fields), fields[3]);
  assert.equal(editableFindingPath("proposal.milestones[4].tasks[2].reason", fields), null);
});
