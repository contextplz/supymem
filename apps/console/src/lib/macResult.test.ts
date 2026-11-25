import assert from "node:assert/strict";
import test from "node:test";
import { isVerifiedMacTask, macResultHeadline } from "./macResult.ts";

test("shared presentation proof rejects failure, uncertainty, and absent verification", () => {
  assert.equal(isVerifiedMacTask({ task_verified: true }), true);
  assert.equal(isVerifiedMacTask({ task_verified: true, failure_code: "native_cleanup_failed" }), false);
  assert.equal(isVerifiedMacTask({ task_verified: true, effect_outcome_unconfirmed: true }), false);
  assert.equal(isVerifiedMacTask({ task_verified: false }), false);
  assert.equal(isVerifiedMacTask(null), false);
});

test("leads with plain visible facts and never a code", () => {
  const line = macResultHeadline({ verdict: "done", visible_facts: [
    "Safari shows fixture-fitness.supymem.com", "Page title: ClubCheck — Reduce Member Cancellations."] });
  assert.equal(line, "Here is what Supymem saw: Safari shows fixture-fitness.supymem.com. Page title: ClubCheck — Reduce Member Cancellations.");
  assert.doesNotMatch(macResultHeadline(null), /_/);
});

test("a failed first observation reports an unconfirmed stop without a finished claim", () => {
  const line = macResultHeadline(null, {task_verified: false,
    failure_code: "desktop_cua_observation_unpinned"});
  assert.match(line, /stopped without confirming/i);
  assert.doesNotMatch(line, /finished|done|unpinned|_/i);
});

test("visual done and stale facts do not override a failed native receipt", () => {
  const review = {verdict: "done", visible_facts: ["TextEdit shows the note"]};
  assert.match(macResultHeadline(review, {task_verified: false,
    failure_code: "desktop_cua_observation_unpinned"}), /^Supymem stopped with an unconfirmed result/);
  assert.match(macResultHeadline(review, {task_verified: true,
    effect_outcome_unconfirmed: true}), /^Supymem stopped with an unconfirmed result/);
  assert.match(macResultHeadline(review, {task_verified: true}), /^Verified/);
  assert.match(macResultHeadline(null, {task_verified: true}), /independently verified/);
});
