import assert from "node:assert/strict";
import test from "node:test";
import { plainJob } from "./jobCopy.ts";

const internal = /job_[a-z0-9]+|failed_dead|handler_failed|\w+Error\b|research\.deep/;
test("saved failures always expose plain copy and an action", () => {
  for (const code of ["handler_failed:ValueError", "job_failed", "job_bad123:RuntimeError", "research_boundary_blocked", "research_provider_rejected", "research_provider_rate_limited", "unknown_error:Exception"]) {
    const result = plainJob({ kind: "research.deep", status: "failed_dead", error_code: code });
    assert.equal(result.title, "Research · Needs attention");
    assert.doesNotMatch(`${result.title} ${result.body}`, internal);
    assert.match(result.body, /try again|refresh|review|run research again/i);
  }
});
test("known durable recovery messages stay specific", () => {
  assert.match(plainJob({ error_code: "plan_inputs_changed" }).body, /idea, research or resources changed/);
  assert.match(plainJob({ last_error: "handler_failed:TypeError" }).body, /saved work is safe/);
  assert.match(plainJob({ error_code: "no_public_candidates" }).body, /Refine the audience or add a contact/);
  assert.match(plainJob({ error_code: "public_search_unavailable" }).body, /Retry search/);
});
test("work and campaign statuses are readable without exposing unknown strings", () => {
  assert.deepEqual(plainJob({ kind: "research.deep", status: "retry_wait" }), { title: "Research · Retrying in a moment", body: "" });
  assert.equal(plainJob({ status: "pending_approval" }).title, "Awaiting approval");
  assert.equal(plainJob({ status: "PAUSED" }).title, "Paused");
  assert.equal(plainJob({ status: "some_internal_status" }).title, "Saved");
});

test("active backoff and completed work retain their authoritative state despite a saved error", () => {
  assert.deepEqual(plainJob({kind: "research.deep", status: "retry_wait", error_code: "research_provider_rate_limited"}),
    {title: "Research · Retrying in a moment", body: ""});
  assert.deepEqual(plainJob({status: "succeeded", last_error: "handler_failed:ValueError"}), {title: "Complete", body: ""});
  assert.match(plainJob({last_error: "handler_failed:ValueError"}).body, /saved work is safe/);
});
