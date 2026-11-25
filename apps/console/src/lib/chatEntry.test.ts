import assert from "node:assert/strict";
import test from "node:test";
import { committedChatHref, committedEntryHref, entryError, entryReviewLabel, founderWords, type EntryCommit } from "./chatEntry.ts";
import { entryConfirmationError } from "./chatEntry.ts";
import { USAGE_LIMIT_MESSAGE } from "./credits.ts";

test("entry limits give only the buy-more notice across unrelated domains", () => {
  for (const domain of ["Supplier audits", "Family gardening"]) {
    const data = {detail: {error: {code: "insufficient_credits", message: `${domain}: 120 credits required`}}};
    assert.equal(entryError(data, "Try again."), USAGE_LIMIT_MESSAGE);
    assert.equal(entryConfirmationError(data), USAGE_LIMIT_MESSAGE);
  }
});

test("founder onboarding words keep readable lines without internal labels", () => {
  const saved = "Coffee subscriptions\nAudience: local coffee drinkers\nClarification: I already have paying customers";
  assert.equal(founderWords(saved), "Coffee subscriptions\nlocal coffee drinkers\nI already have paying customers");
  assert.equal(founderWords("We call it Audience: first"), "We call it Audience: first");
  assert.match(saved, /Audience:/);
});

test("a skipped question shows the choice the founder made, across answers", () => {
  const chose = "I'm not sure, continue with what I shared";
  assert.equal(founderWords("A clinic rota\nAudience: Skip this question", "A clinic rota"), chose);
  assert.equal(founderWords("A recipe journal\nClarification: for weeknights\nSkip this question", "A recipe journal"), `for weeknights\n${chose}`);
  assert.equal(founderWords("We never Skip this question"), "We never Skip this question");
});

function receipt(kind: EntryCommit["action"]["kind"], href: string): EntryCommit {
  return {
    status: "committed", route: kind === "gtm_review" ? "gtm" : kind === "build_review" ? "personal_build" : "startup",
    venture_id: "ven_123", workspace_id: "ws_123", next_step: kind === "gtm_review" ? "gtm" : kind === "build_review" ? "build" : "idea", message: "Continue in chat",
    action: { kind, venture_id: "ven_123", href },
  };
}

test("a committed proposal opens its real venture action with chat visible", () => {
  assert.equal(committedEntryHref(receipt("gtm_review", "/gtm?venture=ven_123")), "/gtm?venture=ven_123&chat=1");
  assert.equal(committedEntryHref(receipt("build_review", "/v/ven_123/idea")), "/v/ven_123/idea?chat=1");
  assert.equal(committedEntryHref(receipt("idea", "/v/ven_123/idea")), "/v/ven_123/idea?chat=1");
});

test("all confirmed routes continue in the same venture chat and leave detailed review optional", () => {
  for (const [kind, href, label] of [
    ["gtm_review", "/gtm?venture=ven_123", "Review go-to-market details"],
    ["build_review", "/v/ven_123/idea", "Review the build direction"],
    ["idea", "/v/ven_123/idea", "Review idea details"],
  ] as const) {
    const committed = receipt(kind, href);
    assert.equal(committedChatHref(committed), "/new?venture=ven_123");
    assert.equal(entryReviewLabel(committed), label);
  }
});

test("a committed proposal cannot send the founder to another venture or an external site", () => {
  assert.throws(() => committedEntryHref(receipt("gtm_review", "/gtm?venture=ven_other")));
  assert.throws(() => committedEntryHref(receipt("idea", "https://example.com")));
  assert.throws(() => committedEntryHref({ ...receipt("idea", "/v/ven_123/idea"), action: { kind: "idea", venture_id: "ven_other", href: "/v/ven_123/idea" } }));
  assert.throws(() => committedChatHref(receipt("idea", "https://example.com")));
  assert.throws(() => committedEntryHref({ ...receipt("idea", "/v/ven_123/idea"), next_step: "build" }));
  assert.throws(() => committedEntryHref({ ...receipt("build_review", "/v/ven_123/idea"), route: "startup" }));
});

test("the API error message is readable from FastAPI's nested response", () => {
  assert.equal(entryError({ detail: { error: { message: "Proposal expired" } } }, "Try again"), "Proposal expired");
  assert.equal(entryError(null, "Try again"), "Try again");
});

test("a new venture starts blank while earlier committed conversations stay available", async () => {
  const { splitEntryHistory } = await import("./chatEntry.ts");
  const item = (id: string, state: string, receipt?: boolean) => ({ text: id, proposal: { id, state, ...(receipt ? { receipt: { message: "ok" } } : {}) } }) as never;
  const items = [item("a", "committed", true), item("b", "committed", true), item("c", "pending")];
  const { earlier, fresh } = splitEntryHistory(items);
  assert.equal(earlier.length, 2);
  assert.equal(fresh.length, 1);
  assert.equal(splitEntryHistory([item("x", "clarify")]).earlier.length, 0);
});
