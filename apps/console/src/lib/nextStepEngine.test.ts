import assert from "node:assert/strict";
import test from "node:test";
import { ACTION_COMPONENTS, checkedNextStep, createNextStepClient, evidenceLabel, evidenceSummary, stepDestination, validCard, type ActionCard, type NextStepDecision } from "./nextStepEngine.ts";

const evidence = { booking: { id: "booking", kind: "call", summary: "Ada’s call tomorrow", contact_id: "contact" } };
const action: ActionCard = { action_id: "prep_call_brief", title: "Prepare for the call", component: "call_brief", risk: "none", draft_only: false,
  params: { contact_id: "contact" }, why: [{ text: "Call is booked tomorrow.", evidence_ids: ["booking"] }], required_evidence_ids: ["booking"] };
const decision: NextStepDecision = { decision_id: "decision", snapshot_hash: "a".repeat(64), venture_id: "ven_one", workspace_id: "ws_one",
  hero: action, alternatives: [], evidence, source: "rules", refining: false };

test("registry covers all eleven later actions and existing early steps", () => {
  for (const identifier of ["prep_call_brief", "log_call_learnings", "follow_up_commitment", "synthesize_learnings", "decide_pivot_persevere",
    "run_pricing_test", "landing_page_waitlist", "mvp_scope", "collect_first_payment", "ask_for_referral", "reengage_outreach",
    "clarify", "validate", "research", "find_customers", "review_leads"]) assert.ok(Object.hasOwn(ACTION_COMPONENTS, identifier), identifier);
  assert.equal(validCard(action, evidence), true);
  assert.equal(validCard({ ...action, action_id: "constructor" }, evidence), false);
  assert.equal(validCard({ ...action, component: "html" }, evidence), false);
  assert.equal(validCard({ ...action, component: "draft_editor" }, evidence), false);
  assert.equal(validCard({ ...action, risk: "sends", draft_only: false }, evidence), false);
});

test("every emitted evidence kind has a founder label and legacy summaries fail closed", () => {
  for (const kind of ["venture", "idea", "founder_fact", "review", "research", "journey", "lead", "sent", "reply", "page", "call",
    "call_schedule", "manual_outreach", "brief", "call_notes", "learnings", "pricing", "payment", "venture_decision", "mvp_scope",
    "learning_deadline", "waitlist", "credits", "website"]) {
    assert.notEqual(evidenceLabel(kind), kind);
    assert.notEqual(evidenceLabel(kind), "Saved evidence");
  }
  assert.equal(evidenceLabel("constructor"), "Saved evidence");
  assert.equal(evidenceLabel("new_pipeline_kind"), "Saved evidence");
  assert.equal(evidenceSummary({ ...evidence.booking, summary: "Idea state: confirmed; idv_4s726p0bm2gt" }), "Saved evidence supports this step.");
  assert.equal(evidenceSummary({ ...evidence.booking, display_summary: "Your idea is confirmed." }), "Your idea is confirmed.");
  assert.throws(() => checkedNextStep({ ...decision, evidence: { booking: { ...evidence.booking, display_summary: {} } } }, "ven_one"));
});

test("response validates venture, evidence, params and alternative count", () => {
  assert.equal(checkedNextStep(decision, "ven_one"), decision);
  for (const value of [{ ...decision, venture_id: "ven_other" }, { ...decision, snapshot_hash: "bad" }, { ...decision, alternatives: [action, action, action] },
    { ...decision, source: "model" }, { ...decision, refining: "yes" }, { ...decision, source: "llm", refining: true },
    { ...decision, hero: { ...action, params: { contact_id: "another_contact" } } },
    { ...decision, hero: { ...action, why: [{ text: "Trust me", evidence_ids: ["missing"] }] } }]) assert.throws(() => checkedNextStep(value, "ven_one"));
  assert.equal(validCard({ ...action, required_evidence_ids: ["missing"] }, evidence), false);
});

test("outcomes post only the offered action and scoped receipt", async () => {
  const requests: { path: string; init?: RequestInit }[] = [];
  const client = createNextStepClient(async (path, init) => { requests.push({ path, init }); return { ok: true, status: 200, data: decision }; });
  assert.deepEqual(await client.get("ven_one"), decision);
  assert.equal(requests[0].init?.cache, "no-store");
  await client.outcome(decision, action, "skip");
  assert.equal(requests[1].path, "/api/ventures/ven_one/next-step/outcome");
  assert.deepEqual(JSON.parse(requests[1].init?.body as string), { decision_id: "decision", action_id: "prep_call_brief", outcome: "skip", result: {} });
  await client.outcome(decision, action, "done", { viewed: true, text: "Prepared questions" });
  await client.outcome(decision, action, "not_relevant");
  await assert.rejects(client.outcome(decision, { ...action, action_id: "research" }, "done"));
  assert.equal(requests.length, 4);
  assert.equal(requests.some(request => /send|publish|charge/.test(request.path)), false);
});

test("failed outcome reports unconfirmed work instead of silently retrying", async () => {
  let calls = 0;
  const client = createNextStepClient(async () => { calls++; return { ok: false, status: 504, error: "timeout", data: null }; });
  await assert.rejects(client.outcome(decision, action, "done"), /may already be saved/);
  assert.equal(calls, 1);
});

test("destinations use fixed routes, never model links or markup", () => {
  assert.equal(stepDestination("review_leads", "ven_one"), "/gtm?venture=ven_one&step=review");
  assert.equal(stepDestination("landing_page_waitlist", "ven_one"), "/v/ven_one/site");
  assert.equal(stepDestination("clarify", "ven/one"), "/v/ven%2Fone/idea");
});
