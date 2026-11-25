import assert from "node:assert/strict";
import test from "node:test";
import { checkedJourney, createJourneyClient, journeyInitialStage, journeySendReason, safeEvidenceUrl, type GtmJourney } from "./gtmJourneyApi.ts";

const owner = { actorId: "person_fixture", workspaceId: "ws_fixture", ventureId: "ven_fixture" };
const fixture: GtmJourney = { venture_id: owner.ventureId, state: "ready", phase: "review", icp: { who: "Small clinic owners", basis: "Fixture", confirmed: true }, prospects: [], drafts: [], batches: [], results: { sent: 0, replied: 0, interested: 0, negative: 0, replies: [] }, gmail: { connected: false }, daily_cap: 5, estimate: { ceiling_micro: 0, basis: "Fixture" } };

test("suggestion deep link opens Review even before audience confirmation or after prior sends", () => {
  const journey = { ...fixture, icp: { ...fixture.icp, confirmed: false, mode: "consumer" as const }, results: { ...fixture.results, sent: 1 } };
  assert.equal(journeyInitialStage(journey, "?venture=ven_fixture&step=review"), 2);
  assert.equal(journeyInitialStage(journey, "?venture=other&step=review"), 0);
  assert.equal(journeyInitialStage(fixture, ""), 1);
});

test("discovery note travels only with the founder's scoped start request", async () => {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const client = createJourneyClient(async (path, init) => { calls.push({ path, init }); return { ok: true, status: 202, data: fixture }; });
  await client.get(owner);
  await client.prepare(owner, false, "Target clinics; avoid existing customers");
  assert.equal(calls[0].init?.method, "GET");
  assert.equal(calls[0].init?.body, undefined);
  assert.equal(calls[1].init?.method, "POST");
  assert.match(calls[1].path, /ven_fixture\/gtm\/journey\/prepare\?workspace_id=ws_fixture/);
  assert.deepEqual(JSON.parse(String(calls[1].init?.body)), { retry: false, note: "Target clinics; avoid existing customers" });
  await client.icp(owner, "Small clinic owners", undefined, "Avoid hospitals");
  assert.deepEqual(JSON.parse(String(calls[2].init?.body)), { who: "Small clinic owners", note: "Avoid hospitals" });
  await client.prepare(owner, false, "Bangalore parents", true);
  assert.deepEqual(JSON.parse(String(calls[3].init?.body)), { retry: false, note: "Bangalore parents", recruitment_opt_in: true });
  await client.icp(owner, "Bangalore parents", undefined, "Apartment communities", true);
  assert.deepEqual(JSON.parse(String(calls[4].init?.body)), { who: "Bangalore parents", note: "Apartment communities", recruitment_opt_in: true });
  await client.icp(owner, "Home espresso enthusiasts", undefined, "", true, "Finding fresh coffee for an espresso machine");
  assert.deepEqual(JSON.parse(String(calls[5].init?.body)), { who: "Home espresso enthusiasts", note: "", recruitment_opt_in: true, problem: "Finding fresh coffee for an espresso machine" });
});

test("journey requests bind workspace and exact approved batch payload", async () => {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const client = createJourneyClient(async (path, init) => { calls.push({ path, init }); return { ok: true, status: 200, data: fixture }; });
  await client.send(owner, { batch_id: "batch", approved_count: 1, approval_digest: "digest" }, ["doc"], 5);
  assert.equal(calls[0].path, "/api/api/v1/ventures/ven_fixture/gtm/journey/send?workspace_id=ws_fixture");
  assert.equal(new Headers(calls[0].init?.headers).get("X-Workspace-ID"), owner.workspaceId);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { batch_id: "batch", draft_ids: ["doc"], approval_digest: "digest", cap: 5 });
});

test("U46 community URLs become stable path-safe contact ids", async () => {
  const paths: string[] = [];
  const client = createJourneyClient(async path => { paths.push(path); return { ok: true, status: 200, data: fixture }; });
  await client.contact(owner, "https://www.reddit.com/r/puppy101/", "sent");
  const { createHash } = await import("node:crypto");
  const id = createHash("sha256").update("https://www.reddit.com/r/puppy101/").digest("hex").slice(0, 24);
  assert.match(paths[0], new RegExp(`/prospects/${id}/sent\\?`));
});

test("resume sending posts the reviewed acknowledgement in the owner's scope", async () => {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const client = createJourneyClient(async (path, init) => { calls.push({ path, init }); return { ok: true, status: 200, data: fixture }; });
  await client.resumeSending(owner);
  assert.equal(calls[0].path, "/api/api/v1/ventures/ven_fixture/gtm/journey/resume-sending?workspace_id=ws_fixture");
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(new Headers(calls[0].init?.headers).get("X-Workspace-ID"), owner.workspaceId);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { reviewed_bounced_addresses: true });
});

test("wrong venture and malformed results cannot populate the journey", () => {
  assert.throws(() => checkedJourney({ ...fixture, venture_id: "other" }, owner), /did not match/);
  assert.throws(() => checkedJourney({ ...fixture, results: { replies: null } }, owner), /did not match/);
  assert.throws(() => checkedJourney({ ...fixture, daily_cap: NaN }, owner), /did not match/);
  assert.throws(() => checkedJourney({ ...fixture, drafts: [null] }, owner), /did not match/);
  assert.throws(() => checkedJourney({ ...fixture, drafts: [{ doc_id: "unversioned" }] }, owner), /did not match/);
});

test("editing sends a new draft decision and clears approval", async () => {
  let sent: RequestInit | undefined;
  const client = createJourneyClient(async (_path, init) => { sent = init; return { ok: true, status: 200, data: fixture }; });
  await client.review(owner, { doc_id: "draft", contact_id: "contact", to: "fixture@example.test", subject: "Subject", body: "Body", state: "draft", review_state: "approved", batch_id: "batch", revision: "a".repeat(64) }, "draft", { subject: "Edited", body: "New body" });
  assert.equal(sent?.method, "PATCH");
  assert.deepEqual(JSON.parse(String(sent?.body)), { expected_revision: "a".repeat(64), decision: "draft", subject: "Edited", body: "New body" });
});

test("evidence links admit only ordinary HTTP URLs", () => {
  assert.equal(safeEvidenceUrl("javascript:alert(1)"), undefined);
  assert.equal(safeEvidenceUrl("https://user:secret@example.test"), undefined);
  assert.equal(safeEvidenceUrl("not a URL"), undefined);
  assert.equal(safeEvidenceUrl("https://example.test/source"), "https://example.test/source");
});

test("missing ownership fails before transport and stale approval has actionable copy", async () => {
  let called = false;
  const client = createJourneyClient(async () => { called = true; return { ok: false, status: 409, data: { detail: { code: "gtm_stale_approval" } } }; });
  await assert.rejects(client.get({ ...owner, actorId: "" }), /authenticated/);
  assert.equal(called, false);
  await assert.rejects(client.prepare(owner), /batch changed/);
});

test("timeouts distinguish safe GET retries from unconfirmed actions", async () => {
  for (const result of [
    { ok: false, status: 504, data: {} },
    { ok: false, status: 502, data: { error: { code: "upstream_timeout" } } },
    { ok: false, status: 0, data: {}, error: "request timed out" },
  ]) {
    const client = createJourneyClient(async () => result);
    await assert.rejects(client.get(owner), /took too long.*retry loading/);
    await assert.rejects(client.prepare(owner), /timed out.*may already have completed/);
  }
  const unavailable = createJourneyClient(async () => ({ ok: false, status: 502, data: { error: { code: "upstream_unavailable" } } }));
  await assert.rejects(unavailable.get(owner), /could not be confirmed/);
});

test("edited queries travel with the audience", async () => {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const client = createJourneyClient(async (path, init) => { calls.push({ path, init }); return { ok: true, status: 200, data: path.endsWith("/page-choice") ? { choice: "skip" } : fixture }; });
  await client.icp(owner, "Small clinic owners", ["clinic owners Pune", "clinic appointments"]);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { who: "Small clinic owners", search_queries: ["clinic owners Pune", "clinic appointments"] });
});

test("contact actions, booking link and the sending request use exact scoped routes", async () => {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const client = createJourneyClient(async (path, init) => { calls.push({ path, init }); return { ok: true, status: 200, data: fixture }; });
  await client.contact(owner, "con 1", "reply", { text: "Yes please", outcome: "interested" });
  await client.contact(owner, "con1", "call-notes", { text: "Notes" });
  await client.bookingLink(owner, "https://cal.com/ada");
  await client.askSending(owner);
  assert.deepEqual(calls.map(c => [c.init?.method, c.path.split("?")[0].replace("/api/api/v1/ventures/ven_fixture/gtm/journey", "")]),
    [["POST", "/prospects/con%201/reply"], ["POST", "/prospects/con1/call-notes"], ["PATCH", "/settings"], ["POST", "/ask-sending"]]);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { text: "Yes please", outcome: "interested" });
  assert.deepEqual(JSON.parse(String(calls[2].init?.body)), { booking_link: "https://cal.com/ada" });
});

test("sending dead ends use plain language and codes for the new actions are explained", async () => {
  assert.match(journeySendReason("outbound_execution_disabled"), /not switched on/);
  assert.match(journeySendReason("outbound_rate_configuration_missing"), /not ready/);
  assert.doesNotMatch(journeySendReason("outbound_rate_unconfigured"), /paused|rate/i);
  const client = createJourneyClient(async () => ({ ok: false, status: 422, data: { detail: { code: "gtm_booking_link_invalid" } } }));
  await assert.rejects(client.bookingLink(owner, "http://x"), /full https link/);
});

test("bulk country confirmation posts once and rejected countries and notes get plain copy", async () => {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const ok = createJourneyClient(async (path, init) => { calls.push({ path, init }); return { ok: true, status: 200, data: fixture }; });
  await ok.confirmProspects(owner, [{ contact_id: "c1", country: "US", lawful_basis: "legitimate_interest" }, { contact_id: "c2", skip: true }]);
  assert.equal(calls[0].path, "/api/api/v1/ventures/ven_fixture/gtm/journey/prospects/confirm?workspace_id=ws_fixture");
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(JSON.parse(String(calls[0].init?.body)).items.length, 2);
  for (const [code, copy] of [["gtm_country_unsupported", /cannot send to that country/], ["gtm_lawful_basis_note_required", /how the contact agreed/]] as const) {
    const bad = createJourneyClient(async () => ({ ok: false, status: 422, data: { detail: { code } } }));
    await assert.rejects(() => bad.eligibility(owner, "c1", { country: "AU" }), copy);
  }
});
