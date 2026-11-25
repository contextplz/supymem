import assert from "node:assert/strict";
import test from "node:test";
import { companyJourneyStep, companyWorkState, currentPlanState, ideaJourneyProgress, acceptedArtifactIsPublished, JOURNEY_OUTCOME, type CompanyJourneyInput } from "./companyJourney.ts";

test("Idea uses the latest valid saved plan and tolerates malformed responses", () => {
  assert.equal(currentPlanState(null), undefined);
  assert.equal(currentPlanState({ items: null }), undefined);
  assert.equal(currentPlanState({ items: [null, { state: "approved" }] }), undefined);
  assert.equal(currentPlanState({ items: [
    { version: 1, state: "approved" },
    { version: 3, state: "founder_review" },
    { version: 2, state: "superseded" },
  ] }), "founder_review");
});

const base: CompanyJourneyInput = {
  hasActiveJobs: false,
  ideaConfirmed: true,
  hasPlan: true,
  planState: "executing",
  productCount: 1,
  hasAcceptedBuild: true,
  websitePublished: false,
};

test("U53 preparing clarification explains the short wait and retains its progress route", () => {
  const step = companyJourneyStep({ ...base, ideaConfirmed: false, hasActiveJobs: true, activeWorkState: "queued", preparingClarification: true });
  assert.equal(step.title, "Preparing your next question");
  assert.equal(step.detail, "Preparing your next question, usually under a minute.");
  assert.equal(step.path, "tasks");
  assert.equal(step.workState, "queued");
});

test("an executing plan with an accepted private build advances to the launch decision", () => {
  const step = companyJourneyStep(base);
  assert.equal(step.key, "review");
  assert.equal(step.path, "site");
  assert.match(step.title, /decide when to publish/i);
  assert.match(step.detail, /saved milestone acceptance/i);
  assert.match(step.detail, /private/i);
});

test("approved and completed plans are executable while unreviewed plans remain at plan review", () => {
  for (const state of ["approved", "executing", "completed", "confirmed"]) {
    assert.equal(companyJourneyStep({ ...base, planState: state, hasAcceptedBuild: false }).key, "build");
  }
  for (const state of ["draft", "founder_review", "editing", "superseded"]) {
    assert.equal(companyJourneyStep({ ...base, planState: state }).key, "plan");
  }
});

test("a published website advances from private review to measured launch", () => {
  const step = companyJourneyStep({ ...base, websitePublished: true });
  assert.equal(step.key, "launch");
  assert.equal(step.path, "analytics");
  assert.match(step.detail, /measure real usage, collect feedback/i);
  assert.doesNotMatch(step.detail, /paid user/i);
});

test("research next step names a goal that works for activation and paid plans", () => {
  const step = companyJourneyStep({ ...base, hasPlan: false });
  assert.equal(step.key, "research");
  assert.match(step.detail, new RegExp(JOURNEY_OUTCOME));
  assert.doesNotMatch(step.detail, /paid user/i);
});

test("a live acquisition page does not complete a failed application build", () => {
  const step = companyJourneyStep({ ...base, websitePublished: true,
    hasProductBuildAttempt: true, hasAcceptedProductBuild: false });
  assert.equal(step.key, "build");
  assert.equal(step.path, "builds");
  assert.match(step.title, /application/i);
  assert.match(step.detail, /acquisition page is live/i);
});

test("an accepted application build is not a hosted, tested product", () => {
  const step = companyJourneyStep({ ...base, websitePublished: true,
    hasProductBuildAttempt: true, hasAcceptedProductBuild: true });
  assert.equal(step.key, "review");
  assert.equal(step.path, "builds");
  assert.match(step.detail, /live runtime and real account tests/i);
});

test("a validating idea stays on validation instead of regressing to clarification", () => {
  const step = companyJourneyStep({ ...base, ideaConfirmed: false, ideaState: "validating", hasPlan: false });
  assert.equal(step.key, "research");
  assert.equal(step.path, "idea");
  assert.match(step.eyebrow, /validate/i);
  assert.match(step.cta, /validation/i);
});

test("a saved GTM goal survives reopen for product and existing Idea ventures", () => {
  const step = companyJourneyStep({ ...base, entryGoal: "gtm", ideaConfirmed: false,
    hasPlan: false, productCount: 0, hasAcceptedBuild: false });
  assert.equal(step.key, "gtm");
  assert.equal(step.path, "gtm");
  assert.match(step.detail, /before starting any paid work/i);
  assert.equal(companyJourneyStep({ ...base, entryGoal: null, ideaConfirmed: false }).key, "clarify");
  assert.equal(companyJourneyStep({ ...base, entryGoal: "gtm", ideaConfirmed: false, hasPlan: false, hasActiveJobs: true }).key, "gtm");
});

test("personal journey reviews the saved project and goes to local build without market stages", () => {
  const reviewing = companyJourneyStep({ ...base, projectIntent: "personal", ideaConfirmed: false,
    ideaState: "reviewing", hasPlan: false });
  assert.equal(reviewing.path, "idea");
  assert.match(reviewing.title, /personal project/i);
  assert.doesNotMatch(reviewing.detail, /market validation is required/i);
  const legacy = companyJourneyStep({ ...base, projectIntent: "personal", ideaConfirmed: false,
    ideaState: "validating", hasPlan: false });
  assert.equal(legacy.path, "idea");
  assert.match(legacy.cta, /review project/i);
  const confirmed = companyJourneyStep({ ...base, projectIntent: "personal", hasPlan: false });
  assert.equal(confirmed.path, "idea");
  assert.match(confirmed.cta, /local build/i);
  assert.doesNotMatch(confirmed.detail, /research|funding|customers/i);
});

test("the idea journey highlights validation while a validation is active", () => {
  assert.equal(ideaJourneyProgress("validating", false), 1);
  assert.equal(ideaJourneyProgress("reviewing", false), 1);
  assert.equal(ideaJourneyProgress("open", true), 1);
  assert.equal(ideaJourneyProgress("confirmed", true), 2);
  assert.equal(ideaJourneyProgress("confirmed", true, "founder_review"), 3);
  assert.equal(ideaJourneyProgress("confirmed", true, "approved"), 4);
});


test("publication uses the exact accepted artifact, not an older website or another app", () => {
  const receipt = { app_id: "app_one", build_artifact_id: "artifact_new", state: "live", url: "https://example.supymem.com/" };
  assert.equal(acceptedArtifactIsPublished("app_one", "artifact_new", receipt), true);
  for (const changed of [{app_id: "app_other"}, {build_artifact_id: "artifact_old"}, {state: "deploying"}, {url: ""}]) {
    assert.equal(acceptedArtifactIsPublished("app_one", "artifact_new", {...receipt, ...changed}), false);
  }
});


test("queued, claimed and unknown job receipts never imply active execution", () => {
  for (const status of ["pending", "scheduled", "leased", "retry_wait"]) {
    const work = companyWorkState([{ status }]);
    assert.equal(work, "queued");
    const step = companyJourneyStep({ ...base, hasActiveJobs: true, activeWorkState: work });
    assert.match(step.title, /queued/i); assert.doesNotMatch(step.eyebrow, /is working|running/i);
  }
  for (const jobs of [null, [{ status: "future" }], [{}]]) {
    const step = companyJourneyStep({ ...base, hasActiveJobs: true, activeWorkState: companyWorkState(jobs) });
    assert.match(step.title, /check saved/i); assert.doesNotMatch(step.detail, /continues securely/i);
  }
  assert.equal(companyWorkState([{ status: "running" }]), "running");
  assert.equal(companyWorkState([{ status: "succeeded" }]), "none");
  assert.equal(companyWorkState([]), "none");
  assert.equal(companyJourneyStep({ ...base, hasActiveJobs: true }).workState, "unknown");
});
