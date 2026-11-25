import assert from "node:assert/strict";
import test from "node:test";
import { pausedReasonLabel, teamCountLabel, ventureStatusLabel } from "./format.ts";

test("server status wins over a stale draft stage; paused reasons are never raw codes", () => {
  assert.equal(ventureStatusLabel("draft", { label: "Landing page live" }), "Landing page live");
  assert.equal(pausedReasonLabel("awaiting_some_new_thing"), "awaiting some new thing");
  assert.doesNotMatch(pausedReasonLabel("awaiting_approval"), /_/);
});

test("team counts use founder language without claiming idle teammates are working", () => {
  assert.equal(teamCountLabel(), "No team yet");
  assert.equal(teamCountLabel(0, 0), "No team yet");
  assert.equal(teamCountLabel(1), "1 teammate");
  assert.equal(teamCountLabel(4, 0), "4 teammates");
  assert.equal(teamCountLabel(4, 2), "2 of 4 teammates working");
  assert.equal(teamCountLabel(1, 1), "1 of 1 teammate working");
});
