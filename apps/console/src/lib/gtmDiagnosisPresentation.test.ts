import assert from "node:assert/strict";
import test from "node:test";
import { diagnosisExcerpt, diagnosisFirstAction, diagnosisNextStep } from "./gtmDiagnosisPresentation.ts";

test("GTM first screen is bounded while the original report remains intact", () => {
  const full = "Start with one narrow customer segment. ".repeat(20);
  const short = diagnosisExcerpt(full);
  assert.ok(short.length <= 211);
  assert.ok(short.endsWith("…"));
  assert.equal(diagnosisExcerpt("  A saved  hypothesis\n to test.  "), "A saved hypothesis to test.");
  assert.equal(full.includes(short.slice(0, -1)), true);
});

test("every diagnosis action gives a truthful next step scoped to the selected company", () => {
  const id = "company/with space";
  assert.deepEqual(["research", "human_interview", "strategy_review"].map(kind =>
    diagnosisNextStep(kind as "research" | "human_interview" | "strategy_review", id)?.href), [
    "/v/company%2Fwith%20space/research",
    "/v/company%2Fwith%20space/feedback",
    "/v/company%2Fwith%20space/plan",
  ]);
  const interview = diagnosisNextStep("human_interview", id);
  assert.match(interview!.guidance, /Founder task: conduct the interviews/);
  assert.match(interview!.guidance, /no interview has been scheduled or conducted/);
  assert.equal(diagnosisNextStep("send_email" as "research", id), null);
});

test("saved GTM action presentation tolerates older reports with missing or empty actions", () => {
  const action = { kind: "research" as const, instruction: "Review the saved customer evidence.", reason: "Check the demand hypothesis." };
  assert.equal(diagnosisFirstAction(null), undefined);
  assert.equal(diagnosisFirstAction({}), undefined);
  assert.equal(diagnosisFirstAction({ next_actions: [] }), undefined);
  assert.deepEqual(diagnosisFirstAction({ next_actions: [action] }), action);
});

test("local diagnosis recommendations open working workflows for unrelated audiences", () => {
  for (const ventureId of ["factory-safety-teams", "family-hiking-routes"]) {
    for (const kind of ["find_customers", "draft_outreach", "build_page"] as const) {
      const next = diagnosisNextStep(kind, ventureId);
      assert.ok(next);
      assert.equal(next.href, kind === "build_page" ? `/v/${ventureId}/site` : `/gtm?venture=${ventureId}`);
      assert.ok(next.guidance);
    }
  }
});
