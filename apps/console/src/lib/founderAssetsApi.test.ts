import assert from "node:assert/strict";
import test from "node:test";
import { checkedContactPreview, checkedFounderStage, checkedSiteImport, contactsImportRequested, createFounderAssetsClient, siteImportFromSearch, type ContactPreview } from "./founderAssetsApi.ts";

const owner = { actorId: "actor_fixture", workspaceId: "workspace_fixture", ventureId: "venture fixture" };
const stage = { stage: "page_live", confidence: 0.9, evidence: [], confirmed_by_founder: false, updated_at: "2026-10-08T00:00:00Z", history: [] };
const preview: ContactPreview = { preview_id: "preview_fixture", columns: ["Email"], mapping: { email: "Email" }, rows: [{ row_number: 1, name: "Synthetic contact", email: "qa@example.test", company: "Fixture", status: "contacted", email_check: "unknown", duplicate: false, errors: [] }], total_rows: 1, valid_rows: 1, duplicates: 0, invalid_rows: 0, expires_at: "2099-10-08T00:00:00Z" };

test("founder stages use one closed vocabulary and reject ungrounded or malformed records", () => {
  assert.equal(checkedFounderStage(stage).stage, "page_live");
  for (const bad of [{ ...stage, stage: "live" }, { ...stage, confidence: 2 }, { ...stage, evidence: null }, { ...stage, confirmed_by_founder: "yes" }]) assert.throws(() => checkedFounderStage(bad));
});
test("unverified sites stay visible for retry and verified sites require an audit", () => {
  const pending = { site: { url: "https://example.test", status: "unverified", audit: null }, founder_stage: stage };
  assert.equal(checkedSiteImport(pending).site?.status, "unverified");
  assert.equal(checkedSiteImport({ site: null, founder_stage: stage }).site, null);
  assert.throws(() => checkedSiteImport({ ...pending, site: { ...pending.site, status: "verified" } }));
  assert.throws(() => checkedSiteImport({ ...pending, site: { ...pending.site, url: "javascript:alert(1)" } }));
});
test("chat destination queries only prefill a safe website or open the contact importer", () => {
  assert.equal(siteImportFromSearch("?import=https%3A%2F%2Fexample.test%2Fpage"), "https://example.test/page");
  for (const url of ["javascript:alert(1)", "https://name:password@example.test", "file:///private/test", "//example.test"]) assert.equal(siteImportFromSearch(`?import=${encodeURIComponent(url)}`), undefined);
  assert.equal(contactsImportRequested("?venture=fixture&import=1"), true);
  assert.equal(contactsImportRequested("?venture=fixture&import=0&text=ignored"), false);
});
test("stage confirmation and site import carry venture encoding and workspace scope", async () => {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const client = createFounderAssetsClient(async (path, init) => { calls.push({ path, init }); return { ok: true, status: 200, data: path.endsWith("founder-stage") ? stage : { site: null, founder_stage: stage } }; });
  await client.confirmStage(owner, "outreach"); await client.importSite(owner, "https://example.test");
  assert.equal(calls[0].path, "/api/ventures/venture%20fixture/founder-stage");
  assert.equal(calls[0].init?.method, "PATCH"); assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { stage: "outreach", confirmed: true });
  assert.equal(new Headers(calls[1].init?.headers).get("X-Workspace-ID"), owner.workspaceId);
  await assert.rejects(client.importSite(owner, "https://name:password@example.test")); assert.equal(calls.length, 2);
  await assert.rejects(client.stage({ ...owner, actorId: "" })); assert.equal(calls.length, 2);
});
test("contact previews retain mapping and validate the immutable token receipt", async () => {
  let body: unknown;
  const client = createFounderAssetsClient(async (_path, init) => { body = JSON.parse(String(init?.body)); return { ok: true, status: 200, data: preview }; });
  await client.previewContacts(owner, "Email\nqa@example.test", "csv", { email: "Email" });
  assert.deepEqual(body, { text: "Email\nqa@example.test", format: "csv", mapping: { email: "Email" } });
  assert.throws(() => checkedContactPreview({ ...preview, expires_at: "invalid" }));
  assert.throws(() => checkedContactPreview({ ...preview, rows: [{ ...preview.rows[0], errors: "missing" }] }));
});
test("contact commit refuses missing consent and expired previews before transport", async () => {
  let calls = 0;
  const client = createFounderAssetsClient(async () => { calls++; return { ok: false, status: 400, data: {} }; });
  await assert.rejects(client.importContacts(owner, preview, false, "consent", "Synthetic fixture"));
  await assert.rejects(client.importContacts(owner, { ...preview, expires_at: "2000-01-01T00:00:00Z" }, true, "consent", "Synthetic fixture"));
  assert.equal(calls, 0);
});
test("imported history patches only the selected scoped contact and checks the refreshed journey", async () => {
  let call: { path: string; init?: RequestInit } | undefined;
  const journey = { venture_id: owner.ventureId, icp: {}, prospects: [], drafts: [], batches: [], gmail: {}, results: { replies: [] }, estimate: {}, daily_cap: 5 };
  const client = createFounderAssetsClient(async (path, init) => { call = { path, init }; return { ok: true, status: 200, data: { journey, founder_stage: stage } }; });
  await client.contactHistory(owner, "contact/fixture", { status: "contacted", last_contacted: null });
  assert.equal(call?.path, "/api/ventures/venture%20fixture/gtm/contacts/contact%2Ffixture/history");
  assert.equal(call?.init?.method, "PATCH"); assert.deepEqual(JSON.parse(String(call?.init?.body)), { status: "contacted", last_contacted: null });
  assert.equal(new Headers(call?.init?.headers).get("X-Workspace-ID"), owner.workspaceId);
  const wrong = createFounderAssetsClient(async () => ({ ok: true, status: 200, data: { journey: { ...journey, venture_id: "foreign_fixture" }, founder_stage: stage } }));
  await assert.rejects(wrong.contactHistory(owner, "contact_fixture", { status: "replied", last_contacted: "2026-10-01" }));
});
test("email verification retries only the scoped imported contact and never invokes sending", async () => {
  const calls: { path: string; init?: RequestInit }[] = [];
  const journey = { venture_id: owner.ventureId, icp: {}, prospects: [], drafts: [], batches: [], gmail: {}, results: { replies: [] }, estimate: {}, daily_cap: 5 };
  const client = createFounderAssetsClient(async (path, init) => { calls.push({ path, init }); return { ok: true, status: 200, data: { journey, founder_stage: stage } }; });
  await client.verifyContactEmail(owner, "contact/fixture");
  assert.equal(calls.length, 1); assert.equal(calls[0].path, "/api/ventures/venture%20fixture/gtm/contacts/contact%2Ffixture/verify-email"); assert.equal(calls[0].init?.method, "POST");
  assert.equal(new Headers(calls[0].init?.headers).get("X-Workspace-ID"), owner.workspaceId);
});
test("failed imports show plain recovery copy without exposing server internals", async () => {
  const client = createFounderAssetsClient(async () => ({ ok: false, status: 400, data: { detail: { code: "unexpected_internal_id_fixture", message: "private internal debug" } } }));
  await assert.rejects(client.stage(owner), error => error instanceof Error && !/internal|fixture|debug/.test(error.message));
});
