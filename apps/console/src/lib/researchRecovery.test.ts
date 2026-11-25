import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { loadResearchRecovery, researchEmptyMessage, researchReceiptMatches, researchStartDecision, unknownResearchRecovery } from "./researchRecovery.ts";
import type { DurableJob } from "./durableJobs.ts";
const originalWindow = globalThis.window, originalFetch = globalThis.fetch;
afterEach(() => {
  Object.defineProperty(globalThis, "window", { configurable: true, value: originalWindow });
  globalThis.fetch = originalFetch;
});
const failed: DurableJob = { id: "job_failed", kind: "research.deep", status: "failed_dead", venture_id: "ven_a", revision_id: "rev_current", error_code: "research_invalid_output" };
function receipts(active: DurableJob[], history: DurableJob[], state = "confirmed", intent = "startup") {
  const requests: string[] = [];
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: async (command: string, args: { path: string; method?: string }) => {
      assert.equal(command, "desktop_api_request"); assert.equal(args.method, "GET"); requests.push(args.path);
      if (args.path.endsWith("/idea/sessions")) return { status: 200, data: { items: [{ id: "ses_a", state, project_intent: intent }] } };
      if (args.path.endsWith("/idea/sessions/ses_a")) return { status: 200, data: { id: "ses_a", state, venture_id: "ven_a", workspace_id: "wsp_a", current_revision: { id: "rev_current", structured: { project_intent: intent } } } };
      const url = new URL(args.path, "https://gateway.example");
      assert.equal(url.pathname, "/api/workspaces/wsp_a/jobs");
      assert.equal(url.searchParams.get("venture_id"), "ven_a");
      assert.deepEqual(url.searchParams.getAll("kinds"), [state === "confirmed" ? "research.deep" : "idea.validate"]);
      return { status: 200, data: { items: url.searchParams.get("active_only") === "true" ? active : history } };
    } }, dispatchEvent: () => true,
  } });
  globalThis.fetch = async () => { throw new Error("Unexpected browser request"); };
  return requests;
}
test("reopening confirmed research restores actual current failed receipt without any POST", async () => {
  const requests = receipts([], [failed]);
  const saved = await loadResearchRecovery("ven_a", "wsp_a");
  assert.equal(saved.state, "confirmed"); assert.equal(saved.failure, failed);
  assert.match(researchEmptyMessage(saved), /did not finish/);
  assert.doesNotMatch(researchEmptyMessage(saved), /Clarify|queued or running/);
  assert.equal(requests.length, 4);
  assert.equal(researchStartDecision("rev_current", saved), "start"); // only after explicit review
});
test("current active work wins over older failure; stale revision is never restored", async () => {
  for (const history of [[failed], [{ ...failed, revision_id: "rev_old" }]]) {
    receipts([{ ...failed, id: "job_active", status: "scheduled" }], history);
    const saved = await loadResearchRecovery("ven_a", "wsp_a");
    assert.equal(saved.jobId, "job_active"); assert.equal(saved.failure, undefined);
    assert.equal(researchStartDecision("rev_current", saved), "observe");
    assert.match(researchEmptyMessage(saved), /queued or running/);
  }
  receipts([{ ...failed, id: "job_old", status: "leased", revision_id: "rev_old" }], [{ ...failed, revision_id: "rev_old" }]);
  const saved = await loadResearchRecovery("ven_a", "wsp_a");
  assert.equal(saved.jobId, null); assert.equal(saved.failure, undefined);
  assert.match(researchEmptyMessage(saved), /current idea is confirmed/);
});
test("newer current success supersedes historical failure", async () => {
  receipts([], [{ ...failed, id: "job_success", status: "succeeded" }, failed]);
  const saved = await loadResearchRecovery("ven_a", "wsp_a");
  assert.equal(saved.failure, undefined); assert.equal(saved.jobId, null);
});
test("unconfirmed validation and personal route do not imply a new paid deep research request", async () => {
  receipts([], [{ ...failed, kind: "idea.validate" }], "validating");
  const validation = await loadResearchRecovery("ven_a", "wsp_a");
  assert.equal(validation.failure?.kind, "idea.validate");
  assert.equal(researchStartDecision("rev_current", validation), "review");
  const requests = receipts([], [failed], "confirmed", "personal");
  const personal = await loadResearchRecovery("ven_a", "wsp_a");
  assert.equal(personal.failure, undefined); assert.equal(requests.length, 2);
  assert.match(researchEmptyMessage(personal), /personal project/);
  assert.equal(researchStartDecision("rev_current", personal), "review");
});
test("scope/account changes during each saved-state await discard all delivered receipts", async () => {
  for (const stopAfter of [1, 2, 3]) {
    let completed = 0;
    const read = async () => { completed++; return { ok: true, data: completed === 1 ? { items: [{ id: "ses_a", state: "confirmed" }] } : { id: "ses_a", venture_id: "ven_a", workspace_id: "wsp_a", state: "confirmed", current_revision: { id: "rev_current" } } }; };
    const saved = await loadResearchRecovery("ven_a", "wsp_a", { read,
      current: () => completed < stopAfter,
      restore: async () => { completed++; return { id: null, error: "", lastFailure: failed }; } });
    assert.deepEqual(saved, unknownResearchRecovery());
    assert.equal(completed, stopAfter);
  }
});
test("foreign session or missing current revision fails closed with refresh recovery", async () => {
  for (const detail of [
    { id: "ses_a", venture_id: "ven_b", workspace_id: "wsp_a", state: "confirmed", current_revision: { id: "rev_current" } },
    { id: "ses_a", venture_id: "ven_a", workspace_id: "wsp_a", state: "confirmed" },
  ]) {
    let calls = 0;
    const saved = await loadResearchRecovery("ven_a", "wsp_a", { read: async () => ({ ok: true, data: ++calls === 1 ? { items: [{ id: "ses_a" }] } : detail }), restore: async () => { throw new Error("Must not restore"); } });
    assert.ok(saved.error); assert.equal(researchStartDecision("rev_current", saved), "review");
    assert.match(researchEmptyMessage(saved), /unavailable/);
  }
});
test("explicit request fresh reads reject changed revision, unknown status and personal scope", () => {
  const ready = { ...unknownResearchRecovery(), state: "confirmed", revisionId: "rev_current" };
  assert.match(researchEmptyMessage(ready), /Research uses credits\. We set them aside when it starts and nothing is published\./);
  assert.doesNotMatch(researchEmptyMessage(ready), /credit-metered|durable research job/);
  assert.equal(researchStartDecision("rev_current", ready), "start");
  for (const changed of [{ ...ready, revisionId: "rev_new" }, { ...ready, state: "clarifying" }, { ...ready, personal: true }, { ...ready, error: "Cannot check jobs" }, unknownResearchRecovery()]) {
    assert.equal(researchStartDecision("rev_current", changed), "review");
  }
});
test("terminal polling receipts cannot affect another venture, revision, job or account", () => {
  const scope = { id: failed.id, venture: "ven_a", revision: "rev_current", owner: "person_a" };
  assert.equal(researchReceiptMatches(scope, failed, "ven_a", "person_a"), true);
  for (const wrong of [{ ...failed, venture_id: "ven_b" }, { ...failed, revision_id: "rev_old" }, { ...failed, id: "job_other" }, { ...failed, kind: "plan.generate" }]) {
    assert.equal(researchReceiptMatches(scope, wrong, "ven_a", "person_a"), false);
  }
  assert.equal(researchReceiptMatches(scope, failed, "ven_a", "person_b"), false);
  assert.equal(researchReceiptMatches(scope, failed, "ven_b", "person_a"), false);
  assert.equal(researchReceiptMatches(null, failed, "ven_a", "person_a"), false);
});
