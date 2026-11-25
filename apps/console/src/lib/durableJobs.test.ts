import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { delayedJobNotice, ideaJobKindsForSession, isDurableJob, jobNeedsRecovery, reportJobFailure, restoreActiveJob, restoreIdeaSessionJob, statusObservationStalled, unexpectedJobFailure } from "./durableJobs.ts";
import { UI_DIAGNOSTIC_EVENT } from "./uiDiagnostics.ts";

const originalWindow = globalThis.window;

test("a saved prompt or proposal retires its initial clarification job", async () => {
  for (const state of ["clarifying", "validating", "reviewing"]) {
    const session = { state, initial_job_id: "old-job", turns: [{ role: "agent", kind: "question", options: { phase: "gstack" } }] };
    assert.deepEqual(ideaJobKindsForSession(session), []);
    assert.deepEqual(await restoreIdeaSessionJob("ven_a", session, "wsp_a"), { id: null, error: "", lastFailure: undefined });
  }
});
const originalFetch = globalThis.fetch;

test("confirmed personal projects do not restore obsolete research failures", () => {
  assert.deepEqual(ideaJobKindsForSession({ state: "confirmed", project_intent: "personal" }), []);
  assert.deepEqual(ideaJobKindsForSession({ state: "confirmed", project_intent: "startup" }), ["research.deep"]);
  assert.deepEqual(ideaJobKindsForSession({ state: "confirmed" }), ["research.deep"]);
  assert.deepEqual(ideaJobKindsForSession({ state: "validating", project_intent: "personal" }),
    ["idea.clarify_turn", "idea.validate"]);
});

test("confirmed personal projects ignore an unexpected initial job ID without querying old jobs", async () => {
  globalThis.fetch = async () => { throw new Error("Unexpected job lookup"); };
  assert.deepEqual(await restoreIdeaSessionJob("ven_a", {
    state: "confirmed", project_intent: "personal", initial_job_id: "job_old_clarification",
  }, "wsp_a"), { id: null, error: "", lastFailure: undefined });
  assert.deepEqual(await restoreIdeaSessionJob("ven_a", {
    state: "clarifying", project_intent: "personal", initial_job_id: "job_current_clarification",
  }, "wsp_a"), { id: "job_current_clarification", error: "", lastFailure: undefined });
});

test("delayed work is visible without changing or replaying its job", () => {
  const now = Date.parse("2026-09-28T12:00:00Z");
  const leased = { id: "job_1", kind: "idea.validate", status: "leased", locked_at: "2026-09-28T11:57:00Z" };
  assert.match(delayedJobNotice(leased, now) || "", /saved progress/);
  assert.equal(delayedJobNotice({ ...leased, locked_at: "2026-09-28T11:59:00Z" }, now), null);
  assert.equal(delayedJobNotice({ ...leased, status: "succeeded" }, now), null);
  assert.match(delayedJobNotice({ ...leased, status: "scheduled", scheduled_for: "2026-09-28T11:57:00Z" }, now) || "", /queued longer/);
  assert.equal(delayedJobNotice({ ...leased, status: "scheduled", scheduled_for: "2026-09-28T12:01:00Z" }, now), null);
  // Successful status reads must not leave a queued or leased job spinning forever.
  assert.equal(jobNeedsRecovery("", false, Boolean(delayedJobNotice(leased, now))), true);
  assert.equal(jobNeedsRecovery("", false, Boolean(delayedJobNotice({ ...leased, locked_at: "2026-09-28T11:59:00Z" }, now))), false);
});
test("a missing status response becomes a recoverable observation failure without ending the job", () => {
  const now = Date.parse("2026-09-29T00:00:45Z");
  const lastConfirmed = now - 45_000;
  const active = { id: "job_saved", kind: "idea.validate", status: "leased" };
  assert.equal(statusObservationStalled("job_saved", null, "", lastConfirmed, now - 1), false);
  assert.equal(statusObservationStalled("job_saved", null, "", lastConfirmed, now), true);
  assert.equal(statusObservationStalled("job_saved", active, "", lastConfirmed, now), true);
  assert.equal(statusObservationStalled("job_saved", active, "", now - 1, now), false);
  assert.equal(statusObservationStalled("job_saved", { ...active, status: "succeeded" }, "", lastConfirmed, now), false);
  assert.equal(statusObservationStalled(null, null, "", lastConfirmed, now), false);
  assert.equal(statusObservationStalled("job_saved", null, "Connection failed", lastConfirmed, now), false);
});
afterEach(() => {
  Object.defineProperty(globalThis, "window", { configurable: true, value: originalWindow });
  globalThis.fetch = originalFetch;
});

function responses(handler: (path: string) => { status: number; data: unknown }) {
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: async (command: string, args: { path: string }) => {
      assert.equal(command, "desktop_api_request"); return handler(args.path);
    } }, dispatchEvent: () => true,
  } });
  globalThis.fetch = async () => { throw new Error("Unexpected browser request"); };
}

test("reopening a plan recovers the persisted job with server-side scope filters", async () => {
  responses((path) => {
    if (path === "/api/ventures/ven_a") return { status: 200, data: { workspace_id: "wsp_a" } };
    const url = new URL(path, "https://gateway.example");
    assert.equal(url.pathname, "/api/workspaces/wsp_a/jobs");
    assert.equal(url.searchParams.get("venture_id"), "ven_a");
    assert.equal(url.searchParams.get("active_only"), "true");
    assert.deepEqual(url.searchParams.getAll("kinds"), ["plan.compile"]);
    return { status: 200, data: { items: [{ id: "job_a", venture_id: "ven_a", kind: "plan.compile", status: "leased" }] } };
  });
  assert.deepEqual(await restoreActiveJob("ven_a", ["plan.compile"]), { id: "job_a", error: "" });
});

test("a failed reconnect is not treated as an empty queue", async () => {
  responses(() => ({ status: 503, data: {} }));
  const result = await restoreActiveJob("ven_a", ["plan.compile"], "wsp_a");
  assert.equal(result.id, null);
  assert.ok(result.error);
});

test("malformed successful job responses produce recovery errors instead of stranding the loader", async () => {
  assert.equal(isDurableJob(null), false);
  assert.equal(isDurableJob({ id: "job_a", status: "leased" }), false);
  assert.equal(isDurableJob({ id: "job_a", status: "unknown", kind: "idea.validate" }), false);
  assert.equal(isDurableJob({ id: "job_a", status: "leased", kind: "idea.validate" }), true);
  for (const malformed of [null, { items: [null] }, { items: [{ id: "job_a", status: "leased" }] }]) {
    responses(() => ({ status: 200, data: malformed }));
    const restored = await restoreActiveJob("ven_a", ["idea.validate"], "wsp_a");
    assert.equal(restored.id, null);
    assert.match(restored.error, /could not be synchronized/);
  }
  responses((path) => ({ status: 200, data: {
    items: new URL(path, "https://gateway.example").searchParams.get("active_only") === "true" ? [] : [null],
  } }));
  const history = await restoreActiveJob("ven_a", ["idea.validate"], "wsp_a", true);
  assert.equal(history.id, null);
  assert.match(history.error, /could not be read/);
});

test("reloading validation follows the current revision instead of a leased older correction", async () => {
  const old = { id: "job_old", venture_id: "ven_a", kind: "idea.validate", status: "leased", revision_id: "rev_old" };
  const current = { id: "job_current", venture_id: "ven_a", kind: "idea.validate", status: "scheduled", revision_id: "rev_current" };
  responses((path) => {
    const query = new URL(path, "https://gateway.example").searchParams;
    assert.equal(query.get("active_only"), "true");
    assert.equal(query.get("limit"), "50");
    return { status: 200, data: { items: [old, current] } };
  });
  assert.deepEqual(await restoreIdeaSessionJob("ven_a", {
    state: "validating", current_revision: { id: "rev_current" },
  }, "wsp_a"), { id: "job_current", error: "" });
});

test("an older leased validation does not trap a corrected idea on reload", async () => {
  const old = { id: "job_old", venture_id: "ven_a", kind: "idea.validate", status: "leased", revision_id: "rev_old" };
  responses(() => ({ status: 200, data: { items: [old] } }));
  assert.deepEqual(await restoreIdeaSessionJob("ven_a", {
    state: "reviewing", current_revision: { id: "rev_current" },
  }, "wsp_a"), { id: null, error: "", lastFailure: undefined });
});

test("reloading a failed validation ignores an older revision's result", async () => {
  const old = { id: "job_old", venture_id: "ven_a", kind: "idea.validate", status: "failed_dead", revision_id: "rev_old", error_code: "research_invalid_output" };
  const current = { id: "job_current", venture_id: "ven_a", kind: "idea.validate", status: "failed_dead", revision_id: "rev_current", error_code: "research_output_truncated" };
  responses((path) => ({ status: 200, data: { items: new URL(path, "https://gateway.example").searchParams.get("active_only") === "true" ? [] : [old, current] } }));
  assert.deepEqual(await restoreIdeaSessionJob("ven_a", {
    state: "validating", current_revision: { id: "rev_current" },
  }, "wsp_a"), { id: null, error: "", lastFailure: current });
});

test("foreign and completed jobs cannot be restored as this company's active work", async () => {
  for (const job of [
    { venture_id: "ven_b", kind: "plan.compile", status: "leased" },
    { venture_id: "ven_a", kind: "plan.compile", status: "succeeded" },
  ]) {
    responses(() => ({ status: 200, data: { items: [{ id: "job_x", ...job }] } }));
    const result = await restoreActiveJob("ven_a", ["plan.compile"], "wsp_a");
    assert.equal(result.id, null);
    assert.ok(result.error);
  }
});

test("reopening an idea exposes its failed result without restarting paid work", async () => {
  let requests = 0;
  const events: CustomEvent[] = [];
  const failed = { id: "job_failed", venture_id: "ven_a", kind: "idea.validate", status: "failed_dead", revision_id: "rev_a", error_code: "research_invalid_output" };
  responses(path => {
    requests++;
    const query = new URL(path, "https://gateway.example").searchParams;
    return { status: 200, data: { items: query.get("active_only") === "true" ? [] : [failed] } };
  });
  window.dispatchEvent = (event: Event) => { events.push(event as CustomEvent); return true; };
  assert.deepEqual(await restoreActiveJob("ven_a", ["idea.validate"], "wsp_a", true, reportJobFailure), { id: null, error: "", lastFailure: failed });
  assert.equal(requests, 2);
  assert.deepEqual(events.map(event => ({ type: event.type, detail: event.detail })), [
    { type: UI_DIAGNOSTIC_EVENT, detail: { code: "job_error", context: "job", text: "Job failure", jobId: "job_failed" } },
  ]);
});

test("reopening a job that needs founder setup does not report an operator error", async () => {
  const events: Event[] = [];
  const failed = { id: "job_setup", venture_id: "ven_a", kind: "plan.compile", status: "failed", error_code: "plan_missing_key" };
  responses(path => ({ status: 200, data: { items: new URL(path, "https://gateway.example").searchParams.get("active_only") === "true" ? [] : [failed] } }));
  window.dispatchEvent = (event: Event) => { events.push(event); return true; };
  assert.deepEqual(await restoreActiveJob("ven_a", ["plan.compile"], "wsp_a", true, reportJobFailure), { id: null, error: "", lastFailure: failed });
  assert.deepEqual(events, []);
});

test("unexpected terminal jobs emit one closed diagnostic, while setup outcomes do not", () => {
  const events: CustomEvent[] = [];
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    dispatchEvent: (event: CustomEvent) => { events.push(event); return true; },
  } });
  const job = { id: "job_private", kind: "plan.compile", status: "failed_dead", error_code: "plan_invalid_output" };
  assert.equal(unexpectedJobFailure(job), true);
  reportJobFailure(job);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, UI_DIAGNOSTIC_EVENT);
  assert.deepEqual(events[0].detail, { code: "job_error", context: "job", text: "Job failure", jobId: "job_private" });
  for (const error_code of ["research_insufficient_credits", "plan_inputs_changed", "clarification_byok_unavailable", "job_authority_revoked",
    "research_provider_missing_key", "plan_not_connected", "research_reconnect_required", "plan_permission_required",
    "research_permission_denied", "plan_approval_required", "research_setup_required"]) {
    assert.equal(unexpectedJobFailure({ ...job, error_code }), false);
    reportJobFailure({ ...job, error_code });
  }
  reportJobFailure({ ...job, status: "cancelled" });
  assert.equal(events.length, 1);
});
