import assert from "node:assert/strict";
import test from "node:test";
import { buildPlanTaskId, buildWorkflowHref, planTaskId } from "./taskWorkflow.ts";

test("approved build work opens the exact plan task in Builds", () => {
  const task = {
    source: "approved_plan:pln_1",
    tags: ["plan:pln_1", "plan_task:tsk_plan_1", "capability:build.run"],
  };
  assert.equal(buildPlanTaskId(task), "tsk_plan_1");
  assert.equal(planTaskId(task), "tsk_plan_1");
  assert.equal(buildWorkflowHref("ven_1", task), "/v/ven_1/builds?task=tsk_plan_1");
});

test("ordinary work cannot impersonate a build handoff", () => {
  assert.equal(buildPlanTaskId({ source: "plan", tags: ["capability:build.run"] }), "");
  assert.equal(planTaskId({ source: "plan", tags: ["plan_task:tsk_fake"] }), "");
  assert.equal(buildWorkflowHref("ven_1", { source: "plan" }), "/v/ven_1/builds");
});
