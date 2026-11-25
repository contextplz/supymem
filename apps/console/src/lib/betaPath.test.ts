import assert from "node:assert/strict";
import test from "node:test";
import { BETA_PATH, betaStageIndex, CREDIT_EXPLAINER } from "./betaPath.ts";

test("path is Idea, Page, Publish, Waitlist, Outreach, Research with time expectations", () => {
  assert.deepEqual(BETA_PATH.map((s) => s.label), ["Idea", "Landing page", "Publish", "Waitlist", "Outreach", "Research (optional)"]);
  for (const s of BETA_PATH) {
    assert.ok(s.what && s.time, s.key);
    assert.ok(s.what.length <= 140 && s.title.split(" ").length <= 6, s.key);
    assert.ok(s.href("v1").startsWith("/"), s.key);
  }
});

test("server next-step keys map to a position, unknown keys to none", () => {
  assert.equal(betaStageIndex("idea.clarify"), 0);
  assert.equal(betaStageIndex("site.draft"), 1);
  assert.equal(betaStageIndex("research.start"), 5);
  assert.equal(betaStageIndex("plan.generate"), 1);
  assert.equal(betaStageIndex("site.publish"), 2);
  assert.equal(betaStageIndex("gtm.outreach"), 4);
  assert.equal(betaStageIndex("work.running"), null);
  assert.equal(betaStageIndex(undefined), null);
});

test("a published page makes outreach available without a waitlist prerequisite", () => {
  assert.equal(betaStageIndex("venture.context", true, "traction"), 4);
  assert.equal(betaStageIndex("work.next_task", true), 4);
  assert.equal(betaStageIndex("build.first", true), 4);
  assert.equal(betaStageIndex("gtm.outreach", true), 4);
  assert.equal(betaStageIndex("work.running", true), 4);
  assert.equal(betaStageIndex("work.next_task", false), 1);
  assert.equal(betaStageIndex("site.ready", false), 3);
  assert.equal(betaStageIndex("site.ready", true), 4);
  assert.equal(betaStageIndex("research.running", true), 5);
});

test("saved founder progress is shared by the stage card and step stack", () => {
  for (const stage of ["page_live", "outreach", "traction"]) {
    assert.equal(betaStageIndex("site.ready", false, stage), 4);
    assert.equal(betaStageIndex(undefined, false, stage), 4);
  }
  assert.equal(betaStageIndex("site.publish", false, "building"), 2);
});

test("credit explainer uses the venture's balance without promising repeated grants or a venture cap", () => {
  assert.match(CREDIT_EXPLAINER, /ask Charlie to do something.*without a confirmation/);
  assert.match(CREDIT_EXPLAINER, /did not ask for.*does not start/);
  assert.doesNotMatch(CREDIT_EXPLAINER, /credits|balance|\d+/);
  assert.doesNotMatch(CREDIT_EXPLAINER, /5,000|up to \d+ ventures/);
});
