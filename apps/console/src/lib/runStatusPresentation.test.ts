import assert from "node:assert/strict";
import test from "node:test";
import { isPreparingFirstQuestion, runStatusPresentation, statusBarAction } from "./runStatusPresentation.ts";

test("a sleeping Mac never renders a raw state code", () => {
  const result = runStatusPresentation({state: "waiting_for_device", activity: "Waiting for your Mac, it resumes when your Mac is awake"}, 0);
  assert.equal(result.state, "Waiting for your Mac, it resumes when your Mac is awake");
  assert.equal(result.activity, "Waiting for your Mac, it resumes when your Mac is awake");
});

test("status bar CTA prefers the server next step unless the run needs a specific action", () => {
  const action = { label: "Fix it", href: "/v/x/builds" }, next = { cta_label: "Review plan", href: "/v/x/plan" };
  assert.deepEqual(statusBarAction({ state: "idle", action }, next, false), { label: "Review plan", href: "/v/x/plan" });
  assert.deepEqual(statusBarAction({ state: "needs_you", action }, next, false), action);
  assert.deepEqual(statusBarAction({ state: "idle", action }, undefined, false), action);
  assert.equal(statusBarAction({ state: "completed", action }, next, true), null);
});

test("a ready landing page keeps its live Visit action ahead of product planning", () => {
  const action = {label: "Visit landing page", href: "https://oximoron.supymem.com/"};
  assert.equal(statusBarAction({state: "ready", action}, {cta_label: "Review build", href: "/v/v1/builds"}, false), action);
  assert.equal(runStatusPresentation({state: "ready", activity: "Landing page ready with QA notes."}, 0).state, "Ready");
});

test("cancelled Mac task is historical when nothing needs attention", () => {
  const view = runStatusPresentation({
    state: "cancelled",
    activity: "The reviewed Mac task was cancelled.",
    next_step: "Review the saved result before deciding whether to retry or change the plan.",
  }, 0);
  assert.equal(view.state, "Idle");
  assert.match(view.activity || "", /No work is in progress/);
  assert.doesNotMatch(view.activity || "", /cancelled/i);
  assert.equal(view.historical, true);
});

test("a saved attention request still takes priority over a terminal result", () => {
  const view = runStatusPresentation({ state: "completed", activity: "The last job completed." }, 1);
  assert.equal(view.state, "Needs you");
  assert.match(view.nextStep || "", /saved action request/);
});

test("active work and unresolved Mac review remain current", () => {
  assert.deepEqual(runStatusPresentation({ state: "working", activity: "Building the approved product.", next_step: "Review QA." }, 0), {
    historical: false, state: "Working", activity: "Building the approved product.", nextStep: "Review QA.",
  });
  const review = runStatusPresentation({ state: "needs_you", activity: "Mac task finished, review the result", next_step: "Review Mac task" }, 0);
  assert.equal(review.state, "Needs you");
  assert.match(review.activity || "", /review the result/);
  const failed = runStatusPresentation({ state: "failed", activity: "Build needs review." }, 0);
  assert.equal(failed.state, "Failed");
  assert.equal(failed.activity, "Build needs review.");
});

test("shows Working while the first question is being prepared", () => {
  const run = { state: "needs_you", activity: "Answer" };
  const v = runStatusPresentation(run, 0, "work.running");
  assert.equal(v.state, "Working");
  assert.equal(v.activity, "Preparing your first question");
  assert.equal(runStatusPresentation(run, 0, "idea.clarify").state, "Needs you");
});

test("only the first-question step says Preparing; other running work with a waiting question does not", () => {
  const run = { state: "needs_you", activity: "Waiting for your answer." };
  assert.equal(isPreparingFirstQuestion(run, 0, "work.running", "Preparing your first question"), true);
  assert.equal(isPreparingFirstQuestion(run, 0, "work.running", "Your company work is in progress"), false);
  const view = runStatusPresentation(run, 0, "work.running", "Your company work is in progress");
  assert.equal(view.state, "Needs you");
  assert.equal(view.activity, "Waiting for your answer.");
});

test("a run link to the same screen keeps its anchor instead of the bare server link", () => {
  const action = { label: "Review build evidence", href: "/v/x/builds?app=a&run=r#build-review" };
  const next = { cta_label: "Open builds and QA", href: "/v/x/builds" };
  assert.deepEqual(statusBarAction({ state: "idle", action }, next, false), action);
});

test("a paused venture reads Paused, not Idle", () => {
  const view = runStatusPresentation({ state: "paused", activity: "You paused this venture." }, 0);
  assert.equal(view.state, "Paused");
  assert.equal(view.historical, false);
});

test("active run progress wins over an older dashboard next step", () => {
  const action = { label: "View progress", href: "/v/x/now" };
  assert.deepEqual(statusBarAction({ state: "working", action }, { cta_label: "Continue to plan", href: "/v/x/plan" }, false), action);
});
