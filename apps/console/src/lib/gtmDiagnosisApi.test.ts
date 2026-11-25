import assert from "node:assert/strict";
import test from "node:test";
import { createDiagnosisClient, DiagnosisApiError, founderProductContext, rejectedBeforeEnqueue } from "./gtmDiagnosisApi.ts";
import type { DiagnosisIntent } from "./gtmDiagnosisIntent.ts";
import { UI_DIAGNOSTIC_EVENT } from "./uiDiagnostics.ts";

const owner = { actorId: "alice", workspaceId: "workspace one", ventureId: "venture/one" };
const intent: DiagnosisIntent = { owner, idempotencyKey: "persisted-key", request: { kind: "diagnose", idea: "Idea", problem: "Problem", who: "Teams",
  users_count: 0, input_digest: "a".repeat(64), resource_digest: "b".repeat(64), ceiling_micro: 20000 } };

test("submission replay uses exact venture route, workspace and Idempotency-Key; status remains GET", async () => {
  const calls: { path: string; init?: RequestInit }[] = [];
  const returnedRun = { run_id: "run/one", venture_id: owner.ventureId, kind: "diagnose", state: "queued" };
  const client = createDiagnosisClient(async (path, init) => {
    calls.push({ path, init });
    return { ok: true, status: 202, data: init?.method === "GET" && path.includes("/runs?") ? { runs: [returnedRun] } : returnedRun };
  });
  await client.start(intent); await client.start(intent); await client.run(owner, "run/one"); await client.runs(owner);
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(calls[0].path, "/api/api/v1/ventures/venture%2Fone/gtm/god/runs?workspace_id=workspace%20one");
  assert.equal(new Headers(calls[0].init?.headers).get("Idempotency-Key"), "persisted-key");
  assert.equal(new Headers(calls[0].init?.headers).get("X-Workspace-ID"), owner.workspaceId);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), intent.request);
  for (const call of calls.slice(2)) { assert.equal(call.init?.method, "GET"); assert.equal(call.init?.body, undefined); }
  assert.ok(calls.every(call => !call.path.endsWith("/tick") && !call.path.endsWith("/start")));
});

test("stale quote and unknown transport outcomes retain distinct structured errors", async () => {
  for (const [status, code] of [[409, "gtm_quote_changed"], [409, "gtm_idempotency_conflict"], [402, "gtm_insufficient_credits"], [0, "gtm_request_unresolved"]] as const) {
    const client = createDiagnosisClient(async () => ({ ok: false, status, data: status ? { detail: { code } } : null }));
    await assert.rejects(client.start(intent), error => error instanceof DiagnosisApiError && error.status === status && error.code === code);
  }
});

test("missing company authority stops before transport; selection filters other workspace companies", async () => {
  let calls = 0;
  const client = createDiagnosisClient(async () => { calls++; return { ok: true, status: 200, data: { items: [
    { id: "owned", workspace_id: owner.workspaceId }, { id: "foreign", workspace_id: "other" }, { id: "unknown" },
  ] } }; });
  await assert.rejects(client.start({ ...intent, owner: { ...owner, actorId: "" } }));
  assert.equal(calls, 0);
  assert.deepEqual(await client.ventures(owner.workspaceId), [{ id: "owned", workspace_id: owner.workspaceId }]);
});

test("diagnosis context uses only the selected scoped company and its locked charter", async () => {
  const venture = { id: owner.ventureId, workspace_id: owner.workspaceId, raw_idea: "  Build a safer handoff  " };
  const client = createDiagnosisClient(async path => ({ ok: true, status: 200, data: path.endsWith("/charter")
    ? { content: { problem: "  Work falls through gaps ", audience: " Operations teams " } } : {} }));
  assert.deepEqual(await client.context(owner, venture), {
    idea: "Build a safer handoff", problem: "Work falls through gaps", who: "Operations teams", sources: ["idea", "charter"],
  });
  await assert.rejects(client.context(owner, { ...venture, workspace_id: "foreign" }), /authenticated company/);
});

test("diagnosis context keeps a scoped idea usable when no charter exists", async () => {
  const venture = { id: owner.ventureId, workspace_id: owner.workspaceId, raw_idea: "Idea only" };
  const client = createDiagnosisClient(async path => path.endsWith("/idea/sessions")
    ? { ok: true, status: 200, data: { items: [] } } : { ok: false, status: 404, data: null });
  assert.deepEqual(await client.context(owner, venture), { idea: "Idea only", problem: "", who: "", sources: ["idea"] });
});

test("legacy product answers are readable in GTM without rewriting saved ventures", () => {
  assert.equal(founderProductContext("Existing product: It helps rehearsal studios manage bookings. app · unsure · gtm · solo"), "It helps rehearsal studios manage bookings");
  assert.equal(founderProductContext("Existing product: app · unsure · gtm · solo"), "Existing app");
  assert.equal(founderProductContext("Build a safer handoff"), "Build a safer handoff");
});

test("diagnosis context carries the latest confirmed idea answers when no charter exists", async () => {
  const venture = { id: owner.ventureId, workspace_id: owner.workspaceId, raw_idea: "Build DueSoon" };
  const client = createDiagnosisClient(async path => {
    if (path.endsWith("/charter")) return { ok: false, status: 404, data: null };
    if (path.endsWith("/idea/sessions")) return { ok: true, status: 200, data: { items: [{ id: "session/new" }, { id: "older" }] } };
    return { ok: true, status: 200, data: { current_revision: { structured: {
      problem: "Agencies lose time chasing overdue invoices", customer: "Small service agencies",
    } } } };
  });
  assert.deepEqual(await client.context(owner, venture), {
    idea: "Build DueSoon", problem: "Agencies lose time chasing overdue invoices", who: "Small service agencies",
    sources: ["idea", "idea_session"],
  });
});


test("only definite pre-enqueue rejection allows a new quote; idempotency conflict remains recoverable", () => {
  for (const [status, code, expected] of [[402, "gtm_insufficient_credits", true], [422, "validation", true],
    [409, "gtm_quote_changed", true], [409, "gtm_idempotency_conflict", false], [409, "unknown", false],
    [401, "auth", false], [403, "revoked", false], [0, "transport", false], [500, "internal", false]] as const) {
    assert.equal(rejectedBeforeEnqueue(new DiagnosisApiError(status, code, code)), expected);
  }
});

test("foreign venture responses are rejected rather than displayed or accepted as submission receipts", async () => {
  const client = createDiagnosisClient(async () => ({ ok: true, status: 202,
    data: { run_id: "other", venture_id: "foreign", kind: "diagnose", state: "queued" } }));
  await assert.rejects(client.start(intent), /did not match this company/);
  await assert.rejects(client.run(owner, "other"), /did not match this company/);
});

test("invalid successful diagnosis responses emit only closed integrity diagnostics", async () => {
  const originalWindow = globalThis.window;
  const originalCustomEvent = globalThis.CustomEvent;
  const events: { type: string; detail: unknown }[] = [];
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    dispatchEvent: (event: { type: string; detail: unknown }) => { events.push(event); return true; },
  } });
  Object.defineProperty(globalThis, "CustomEvent", { configurable: true, value: class {
    type: string; detail: unknown;
    constructor(type: string, options: { detail: unknown }) { this.type = type; this.detail = options.detail; }
  } });
  try {
    const privateValue = "xoxb-private-founder-prompt";
    let body: unknown = { run_id: "run_good", venture_id: owner.ventureId, kind: "diagnose", state: "queued" };
    const client = createDiagnosisClient(async () => ({ ok: true, status: 200, data: body }));
    await client.start(intent);
    assert.equal(events.length, 0);
    body = { run_id: privateValue, venture_id: privateValue, kind: "diagnose", state: "queued" };
    await assert.rejects(client.start(intent), /did not match this company/);
    await assert.rejects(client.run(owner, "run_good"), /did not match this company/);
    body = { runs: [{ run_id: privateValue, venture_id: owner.ventureId, kind: "diagnose", state: privateValue }] };
    await assert.rejects(client.runs(owner), /did not match this company/);
    body = { runs: privateValue };
    await assert.rejects(client.runs(owner), /could not be verified/);
    assert.deepEqual(events.map(event => ({ type: event.type, detail: event.detail })), Array.from({ length: 4 }, () => ({ type: UI_DIAGNOSTIC_EVENT,
      detail: { code: "integrity_error", context: "integrity", text: "Integrity failure" } })));
    assert.ok(!JSON.stringify(events).includes(privateValue));
  } finally {
    Object.defineProperty(globalThis, "window", { configurable: true, value: originalWindow });
    Object.defineProperty(globalThis, "CustomEvent", { configurable: true, value: originalCustomEvent });
  }
});
