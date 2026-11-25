import assert from "node:assert/strict";
import test from "node:test";
import { ApproveTimeout, approveGate, withDeadline, briefForm, briefFromForm, briefGaps, draftGate, friendlyBriefError, publishOffer, quoteContentWithTopicRecovery } from "./contentFounder.ts";

const brief = {
  product: { name: "Scope", one_liner: "Catch scope creep", differentiators: ["a", "b"] },
  icp: { segments: [{ name: "Agency owners", pains: ["margin leaks"], role: "owner" }] },
  voice: { tone: "plain" }, claims: [{ text: "c" }], cadence: { value_to_promo: 4 },
};

test("brief becomes a form and back without losing untouched fields", () => {
  const form = briefForm(brief);
  assert.equal(form.audience, "Agency owners");
  assert.equal(form.differentiators, "a\nb");
  const saved = briefFromForm(brief, { ...form, oneLiner: "New line", problems: "x\n\ny" }) as any;
  assert.equal(saved.product.one_liner, "New line");
  assert.deepEqual(saved.icp.segments[0].pains, ["x", "y"]);
  assert.equal(saved.icp.segments[0].role, "owner");
  assert.deepEqual(saved.claims, [{ text: "c" }]);
});

test("missing brief fields are named in founder words", () => {
  assert.deepEqual(briefGaps(briefForm(null)), ["the product name", "a one-line description", "who it is for"]);
  assert.equal(friendlyBriefError("icp.segments[0].name is required."), "Say who this is for.");
});

test("every disabled draft state carries a reason", () => {
  const base = { hasVenture: true, hasBrief: true, busy: false, running: false, pending: false };
  assert.equal(draftGate(base).disabled, false);
  for (const off of [{ hasVenture: false }, { hasBrief: false }, { busy: true }, { running: true }, { pending: true }]) {
    const gate = draftGate({ ...base, ...off });
    assert.equal(gate.disabled, true);
    assert.ok(gate.reason.length > 10);
  }
});

test("publishing is offered only for a connected channel", () => {
  assert.equal(publishOffer("linkedin", []).canPublish, false);
  assert.equal(publishOffer("linkedin", []).hint, "Connect LinkedIn to post directly, or copy the draft.");
  assert.equal(publishOffer("linkedin", [{ platform: "linkedin", status: "revoked" }]).canPublish, false);
  assert.equal(publishOffer("linkedin", [{ platform: "linkedin", status: "active" }]).canPublish, true);
});

test("founder connection copy has no operator or vendor jargon", async () => {
  const { connectionCopy } = await import("../components/connections/connectionCopy.ts");
  for (const id of ["composio", "gmail", "linkedin", "discord", "reddit", "x", "unipile", "hunter", "unknown_thing"]) {
    const c = connectionCopy({ id } as never);
    assert.doesNotMatch(`${c.purpose} ${c.setup} ${c.action}`, /operator|composio|platform key|env/i, id);
  }
});

test("every connection card is one short plain line", async () => {
  const { connectionCopy } = await import("../components/connections/connectionCopy.ts");
  for (const id of ["composio", "gmail", "linkedin", "discord", "reddit", "x", "unipile", "hunter", "google_ads", "meta_ads", "unknown_thing"]) {
    const { purpose } = connectionCopy({ id } as never);
    assert.ok(purpose.length <= 60 && !/workflow|operations|founder-approved|paused/i.test(purpose), `${id}: ${purpose}`);
  }
});

test("approve is enabled for a fresh quote and always explains why it is not", () => {
  assert.equal(approveGate({ busy: false, sufficient: true, pending: false }).disabled, false);
  for (const i of [{ busy: true, sufficient: true, pending: false }, { busy: false, sufficient: false, pending: false }, { busy: false, sufficient: true, pending: true }]) {
    const g = approveGate(i);
    assert.equal(g.disabled, true);
    assert.ok(g.reason.length > 0);
  }
});

test("a request that never answers times out instead of hanging forever", async () => {
  await assert.rejects(withDeadline(new Promise(() => {}), 10), ApproveTimeout);
  assert.equal(await withDeadline(Promise.resolve(7), 50), 7);
  await assert.rejects(withDeadline(Promise.reject(new Error("x")), 50), /x/);
});


test("a stale topic count after editing the brief recovers with a topics quote only", async () => {
  const requests: any[] = [];
  const result = await quoteContentWithTopicRecovery({ kind: "draft" as "draft" | "topics", platform: "reddit", brief_id: "current" }, async request => {
    requests.push(request);
    if (request.kind === "draft") throw new Error("API 409: content_company_topic_required");
    return { ceiling: 123 };
  });
  assert.deepEqual(requests, [{ kind: "draft", platform: "reddit", brief_id: "current" }, { kind: "topics", platform: "reddit", brief_id: "current" }]);
  assert.equal(result.plannedTopics, true);
  assert.equal(result.request.kind, "topics");
  assert.deepEqual(result.quote, { ceiling: 123 });
});

test("other quote failures do not trigger another request", async () => {
  let calls = 0;
  await assert.rejects(quoteContentWithTopicRecovery({ kind: "draft" }, async () => { calls++; throw new Error("content_brief_required"); }), /content_brief_required/);
  assert.equal(calls, 1);
});
