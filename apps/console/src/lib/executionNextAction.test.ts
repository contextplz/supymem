import assert from "node:assert/strict";
import test from "node:test";
import { executionNextAction, type ExecutionNextActionInput } from "./executionNextAction.ts";

const ready: ExecutionNextActionInput = {
  ventureId: "v-one", ready: true, busy: false, jobId: null, error: "", syncError: "",
  resourceDirty: false, resourcesComplete: true, quoteReady: false, hasProposal: true,
  staleDependencies: false, findingCount: 0, planState: "draft",
  planReviewable: true, generationState: "completed", editing: false,
};

test("a restored background job takes precedence over saved plan approval", () => {
  assert.equal(executionNextAction({ ...ready, jobId: "job-1", planState: "approved" }).label, "Plan is updating");
});

test("incomplete resources and a quote each lead to their actual controls", () => {
  assert.equal(executionNextAction({ ...ready, resourcesComplete: false }).href, "#plan-resources");
  assert.equal(executionNextAction({ ...ready, quoteReady: true }).href, "#plan-estimate");
});

test("stale dependencies do not send the founder to approval", () => {
  assert.equal(executionNextAction({ ...ready, staleDependencies: true }).href, "#plan-create");
  assert.equal(executionNextAction({ ...ready, findingCount: 2 }).label, "Edit plan manually");
});

test("a saved guarded draft opens its edit control without a new planning quote", () => {
  const action = executionNextAction({ ...ready, findingCount: 3 });
  assert.equal("intent" in action && action.intent, "edit");
  assert.equal(executionNextAction({ ...ready, findingCount: 3, editing: true }).href, "#plan-edit");
  assert.equal(executionNextAction({ ...ready, findingCount: 3, newerPlanId: "plan-2" }).label, "Review the latest plan");
  assert.equal(executionNextAction({ ...ready, findingCount: 3, staleDependencies: true }).href, "#plan-create");
});

test("approved build tasks lead to their exact saved build review", () => {
  assert.equal(executionNextAction({ ...ready, planState: "approved", nextTask: {
    id: "pt-1", title: "Build the app", capabilityId: "build.run", implementation: "new_code",
  } }).href, "/v/v-one/builds?task=pt-1");
  assert.equal(executionNextAction(ready).href, "#plan-review");
});

test("saved approved work stays primary even when current resource inventory is incomplete", () => {
  const action = executionNextAction({ ...ready, planState: "approved", resourcesComplete: false, quoteReady: true, nextTask: {
    id: "pt-1", title: "Build the app", capabilityId: "build.run", implementation: "new_code",
  } });
  assert.equal(action.href, "/v/v-one/builds?task=pt-1");
});

test("a founder task stays in Execution instead of landing on a disabled build estimate", () => {
  const action = executionNextAction({ ...ready, planState: "approved", nextTask: {
    id: "pt-2", title: "Interview five users", capabilityId: "founder.interview", implementation: "founder_action",
  } });
  assert.equal(action.href, "#plan-task-pt-2");
  assert.match(action.detail, /Interview five users/);
});

test("approved non-build work goes to the runnable work queue", () => {
  assert.equal(executionNextAction({ ...ready, planState: "executing", nextTask: {
    id: "pt-3", title: "Analyze evidence", capabilityId: "research.deep", implementation: "tool",
  } }).href, "/v/v-one/tasks");
});

test("unavailable plan work never routes to Build or the generic Work queue", () => {
  for (const task of [
    { id: "pt-build", title: "Build the app", capabilityId: "build.run", implementation: "new_code" },
    { id: "pt-tool", title: "Run research", capabilityId: "research.deep", implementation: "internal" },
  ]) {
    const action = executionNextAction({ ...ready, planState: "approved", nextTask: {
      ...task, scope: "out", reason: "This capability is unavailable for the milestone.",
    } });
    assert.equal(action.label, "Review blocked step");
    assert.equal(action.href, `#plan-task-${task.id}`);
    assert.match(action.detail, /unavailable for the milestone/);
  }
});
