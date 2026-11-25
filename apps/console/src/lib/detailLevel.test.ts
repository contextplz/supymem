import test from "node:test";
import assert from "node:assert/strict";
import { brief, readDetailLevel } from "./detailLevel.ts";

test("brief keeps two sentences", () => {
  const r = brief("One. Two! Three? Four.");
  assert.equal(r.text, "One. Two!");
  assert.equal(r.truncated, true);
});

test("brief leaves short text alone", () => {
  assert.deepEqual(brief("Just one."), { text: "Just one.", truncated: false });
});

test("brief caps length", () => {
  const r = brief("x".repeat(500), 50);
  assert.equal(r.text.length, 50);
  assert.equal(r.truncated, true);
});

test("readDetailLevel defaults to brief without storage", () => {
  assert.equal(readDetailLevel(), "brief");
});
