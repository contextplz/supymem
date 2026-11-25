import assert from "node:assert/strict";
import test from "node:test";
import { researchNextStep, researchReportState, savedPlanStatus } from "./researchNextStep.ts";

test("completed and partial deep research lead to the actual plan workflow", () => {
  for (const state of ["complete", "partial"]) {
    const next = researchNextStep("ven_123", "deep", state, null, "none", false);
    assert.equal(next.href, "/v/ven_123/plan");
    assert.equal(next.label, "Open execution planning");
    assert.match(next.explanation, /Estimate plan generation/);
    assert.match(next.explanation, /then choose Generate plan/);
    assert.match(next.explanation, /separate reviewed approval/);
  }
});

test("saved founder-review plan takes Research directly to review without a second paid plan prompt", () => {
  const planStatus = savedPlanStatus({ items: [{ id: "pln_latest", state: "founder_review", version: 2 }] });
  for (const state of ["complete", "partial"]) {
    const next = researchNextStep("ven_A&B", "deep", state, "market_first", planStatus, false);
    assert.equal(next.href, "/v/ven_A%26B/plan#plan-review");
    assert.equal(next.label, "Review execution plan");
    assert.match(next.explanation, /missing research sources and unanswered questions remain unverified/i);
    assert.doesNotMatch(next.explanation, /Estimate plan generation|Generate plan/);
  }
});

test("unreadable or other saved plan states never imply a fresh paid generation", () => {
  assert.equal(savedPlanStatus(null), "unknown");
  assert.equal(savedPlanStatus({ items: [{ state: "founder_review" }] }), "unknown");
  assert.equal(savedPlanStatus({ items: [] }), "none");
  assert.equal(savedPlanStatus({ items: [{ id: "pln_1", state: "approved" }] }), "saved");
  for (const planStatus of ["saved", "unknown"] as const) {
    const next = researchNextStep("ven_123", "deep", "partial", null, planStatus, false);
    assert.equal(next.href, "/v/ven_123/plan");
    assert.doesNotMatch(next.explanation, /Estimate plan generation|Generate plan/);
  }
});

test("validation and failed deep research do not skip required review", () => {
  assert.equal(researchNextStep("ven_123", "validation", "complete").href, "/v/ven_123/idea");
  assert.equal(researchNextStep("ven_123", "deep", "failed").href, "#research-gaps");
});

test("active work and outdated history never become a research verdict or plan action", () => {
  assert.equal(researchReportState("complete", true, "old", "current"), "running");
  assert.equal(researchReportState("partial", false, "old", "current"), "outdated");
  assert.equal(researchReportState("failed", false, "current", "current"), "failed");
  assert.equal(researchReportState("partial", false, "current", "current"), "partial");
  for (const state of ["queued", "scheduled", "pending", "leased", "in_progress", "retry_wait", "running"])
    assert.equal(researchReportState(state, false, "current", "current"), "running");
  assert.equal(researchReportState("running", false, "old", "current"), "outdated");
  for (const state of ["running", "failed", "outdated"]) {
    const next = researchNextStep("ven_123", "deep", state, null, "none", false);
    assert.equal(next.href, "#research-gaps");
    assert.doesNotMatch(next.label, /plan|proceed|recommended/i);
  }
});

test("a saved market-first choice directs completed research to the same venture's real waitlist workflow", () => {
  for (const state of ["complete", "partial"]) {
    const next = researchNextStep("ven_A&B", "deep", state, "market_first", "none", false);
    assert.equal(next.href, "/gtm?venture=ven_A%26B");
    assert.equal(next.label, "Test demand with a waitlist");
    assert.match(next.explanation, /test its public signup/);
    assert.match(next.explanation, /advice, not an acquisition action/);
  }
  assert.equal(researchNextStep("ven_123", "deep", "complete", "product_first", "none", false).href, "/v/ven_123/plan");
  assert.equal(researchNextStep("ven_123", "deep", "failed", "market_first").href, "#research-gaps");
  assert.equal(researchNextStep("ven_123", "validation", "complete", "market_first").href, "/v/ven_123/idea");
});


test("plan-optional completed research always offers a page or customers, even with a saved plan", () => {
  for (const state of ["complete", "partial"]) for (const plan of ["none", "saved", "review", "unknown"] as const) {
    const next = researchNextStep("ven_A&B", "deep", state, null, plan, true);
    assert.equal(next.pageChoices, true);
    assert.equal(next.href, "/v/ven_A%26B/site?build=1");
    assert.doesNotMatch(next.explanation, /generate.*plan/i);
  }
});

test("page choices require an explicit verified eligibility decision", () => {
  for (const state of ["complete", "partial"]) {
    assert.equal(researchNextStep("ven_123", "deep", state).pageChoices, undefined);
    assert.equal(researchNextStep("ven_123", "deep", state, null, "none", false).pageChoices, undefined);
  }
  for (const state of ["running", "outdated", "failed"])
    assert.equal(researchNextStep("ven_123", "deep", state, null, "none", true).pageChoices, undefined);
  assert.equal(researchNextStep("ven_123", "validation", "complete", null, "none", true).pageChoices, undefined);
});
