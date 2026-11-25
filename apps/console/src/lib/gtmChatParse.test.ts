import assert from "node:assert/strict";
import test from "node:test";
import { contactIdsFromLeads, filterLeads, parseGtmChat } from "./gtmChatParse.ts";
import type { AiGtmContact, AiGtmLead } from "./opsApi.ts";

const lead = (fields: Partial<AiGtmLead>): AiGtmLead => ({ first_name: "", last_name: "", email: "", title: "", organization: "", industry: "", employees: "", linkedin_url: "", city: "", country: "", website: "", email_confidence: "", email_score: "", source: "", vertical: "", email_tier: "", confidence_pct: "", ...fields });

test("lead discovery retains the founder sector, small count and no-outreach constraint", () => {
  const text = "Find 5 rehearsal studios in London and do not send email outreach";
  const parsed = parseGtmChat(text);
  assert.equal(parsed.intent, "find");
  assert.equal(parsed.filters.query, text);
  assert.equal(parsed.filters.count, 5);
});

test("explicit draft commands are recognized while negated instructions never queue outreach", () => {
  assert.equal(parseGtmChat("email them").intent, "outreach_email");
  assert.equal(parseGtmChat("linkedin them").intent, "outreach_linkedin");
  assert.equal(parseGtmChat("prepare both").intent, "outreach_both");
  for (const text of ["do not send email outreach", "never message them on LinkedIn", "no outreach to both"]) {
    assert.ok(!parseGtmChat(text).intent.startsWith("outreach_"));
  }
});

test("an unknown location is not represented as a verified regional match", () => {
  const filters = { count: 1, query: "studios", region: "San Francisco" };
  const leads = [{ organization: "Unknown" }, { city: "San Diego" }, { city: "San Francisco" }, { city: "San Francisco" }].map(lead);
  assert.deepEqual(filterLeads(leads, filters), [leads[2]]);
});

test("outreach contact mapping cannot fall back to unrelated workspace contacts", () => {
  const contacts = [{ id: "a", email: "a@example.test" }, { id: "b", email: "b@example.test" }] as AiGtmContact[];
  assert.deepEqual(contactIdsFromLeads([], contacts), []);
  assert.deepEqual(contactIdsFromLeads([{ email: "missing@example.test" }] as AiGtmLead[], contacts), []);
  assert.deepEqual(contactIdsFromLeads([{ email: "A@example.test" }] as AiGtmLead[], contacts), ["a"]);
});
