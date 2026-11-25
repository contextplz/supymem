import assert from "node:assert/strict";
import test from "node:test";
import { reachAction, guessedEmailWarning } from "./reachableLeads.ts";
import type { ReachableLead } from "./gtmJourneyApi.ts";
import { usageControl } from "./usageControl.ts";

const lead: ReachableLead = { id: "one", name: "Jane Doe", company: "Dental Practice", role: "Owner", location: "US", fit: 90, why: "Workflow fit", quote: "Independent dental practice", source_url: "https://practice.example", routes: [], draft: "Open to a 15-minute call? Reply with a time.", connection_note: "a".repeat(350) };

test("guessed routes show confidence and a bounce warning; published routes stay unchanged", () => {
  const route = { kind: "email" as const, email: "jane.doe@practice.example", source_url: lead.source_url, quote: "Jane Doe, Owner", route_type: "pattern" as const, confidence: 80 };
  assert.equal(guessedEmailWarning(route), "We guessed this address (80% confidence). It may bounce.");
  assert.equal(guessedEmailWarning({ ...route, route_type: "guess", confidence: 40 }), "We guessed this address (40% confidence). It may bounce.");
  assert.equal(guessedEmailWarning({ ...route, route_type: "published", confidence: 100 }), null);
  assert.equal(reachAction(lead, route)?.label, "Copy email + open mail");
});

test("reach actions encode unsent drafts and limit connection notes", () => {
  const mail = reachAction(lead, { kind: "email", email: "jane@practice.example", source_url: lead.source_url, quote: "jane@practice.example" })!;
  assert.equal(decodeURIComponent(mail.url.split("&body=")[1]), lead.draft);
  assert.equal(mail.label, "Copy email + open mail");
  assert.equal(reachAction(lead, { kind: "email", email: "jane@example.com?bcc=bad", source_url: "", quote: "" }), null);
  const profile = reachAction(lead, { kind: "linkedin", url: "https://www.linkedin.com/in/jane", source_url: "", quote: "" })!;
  assert.equal(profile.text.length, 300);
  assert.equal(reachAction(lead, { kind: "linkedin", url: "https://linkedin.com.evil.example/in/jane", source_url: "", quote: "" }), null);
  assert.equal(reachAction(lead, { kind: "contact_form", url: "javascript:alert(1)", source_url: "", quote: "" }), null);
  assert.equal(reachAction(lead, { kind: "forum", url: "https://reddit.com/r/espresso/comments/abc", source_url: "", quote: "" })?.label, "Reply on Reddit");
  for (const host of ["x.com", "twitter.com", "www.x.com"]) {
    const dm = reachAction({ ...lead, x_note: "b".repeat(350) }, { kind: "x", url: `https://${host}/janedoe`, source_url: "", quote: "" })!;
    assert.equal(dm.label, "Open X");
    assert.equal(dm.text.length, 280);
  }
  for (const url of ["https://x.com.evil.example/jane", "https://x.com/intent", "https://x.com/jane/status/123", "javascript:alert(1)"]) {
    assert.equal(reachAction(lead, { kind: "x", url, source_url: "", quote: "" }), null);
  }
});

test("manual reach controls have registered usage dimensions", () => {
  for (const kind of ["email", "contact_form", "linkedin", "x", "forum", "phone", "website", "copy"]) {
    assert.deepEqual(usageControl(`gtm_reach_${kind}`), { controlId: `gtm_reach_${kind}`, feature: "gtm" });
  }
});

test("published registry phone opens the dialler without sending", () => {
  const route = { kind: "phone" as const, value: "+1 (512) 555-0100", source_url: "https://npiregistry.cms.hhs.gov/provider-view/123", quote: "5125550100" };
  assert.equal(reachAction(lead, route)?.url, "tel:+15125550100");
  assert.equal(reachAction(lead, { ...route, value: "123?send=1" }), null);
});

test("specialist inboxes from older saved results cannot open outreach mail", () => {
  for (const local of "careers jobs job recruitment recruit vacancies hr feedback complaints privacy dpo gdpr data accounts billing invoices finance payroll noreply no-reply donotreply abuse webmaster postmaster press media marketing-unsubscribe".split(" ")) {
    assert.equal(reachAction(lead, { kind: "email", email: `${local}@practice.example`, source_url: lead.source_url, quote: "Published inbox" }), null);
  }
  for (const local of ["jane", "info", "hello", "contact", "enquiries", "reception", "office", "practice", "admin"]) {
    assert.ok(reachAction(lead, { kind: "email", email: `${local}@practice.example`, source_url: lead.source_url, quote: "Published inbox" }));
  }
});
