import assert from "node:assert/strict";
import test from "node:test";

import { briefBody, dedupeParagraphs } from "./chatBrief.ts";

test("keeps the first two sentences", () => {
  assert.deepEqual(briefBody("One. Two! Three. Four. " + "More text here. ".repeat(60)), { text: "One. Two!", truncated: true });
});

test("short replies are untouched", () => {
  assert.deepEqual(briefBody("Done. Saved."), { text: "Done. Saved.", truncated: false });
});

test("caps long sentences at the char limit", () => {
  const r = briefBody("word ".repeat(200) + "end.");
  assert.ok(r.text.length <= 281 && r.text.endsWith("…") && r.truncated);
});

test("cuts before a code fence or list, never inside", () => {
  assert.equal(briefBody("Here you go.\n\n```\ncode. more. x.\n```" + "\nx".repeat(10)).text, "Here you go.");
  assert.equal(briefBody("Steps:\n- a. b.\n- c" + "\n- d".repeat(10)).text, "Steps:");
});

test("a reply that opens with code or a list is returned whole", () => {
  const code = "```\nx. y. z.\n```\nafter";
  assert.deepEqual(briefBody(code), { text: code, truncated: false });
  assert.equal(briefBody("1. one\n2. two").truncated, false);
});

test("questions and short replies are never shortened", () => {
  const q = "Who is this for, and what do they do today instead? Be specific about the customer. Mention the budget owner too.";
  assert.deepEqual(briefBody(q), { text: q, truncated: false });
});

test("a repeated paragraph is dropped, distinct ones kept", () => {
  assert.equal(dedupeParagraphs("Who is this for?\n\nSome context here.\n\nWho is this for?"), "Who is this for?\n\nSome context here.");
});

test("fenced drafts keep their repeated greetings and sign-offs across ventures", () => {
  for (const team of ["the PawRoute team", "the Ledgerly team"]) {
    const draft = (n: number) => "```text\nDraft " + n + "\n\nHi there,\n\nA question for you.\n\nThanks,\n" + team + "\n```";
    const md = "Here are the drafts.\n\n" + [1, 2, 3].map(draft).join("\n\n");
    assert.equal(dedupeParagraphs(md), md);
    assert.equal(dedupeParagraphs("Repeated prose line here.\n\n" + md + "\n\nRepeated prose line here."), "Repeated prose line here.\n\n" + md);
  }
});
