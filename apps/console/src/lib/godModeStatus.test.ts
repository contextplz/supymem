import assert from "node:assert/strict";
import test from "node:test";
import { godModeStatusText } from "./godModeStatus.ts";

test("enabled God mode does not imply it is executing while paused or waiting", () => {
  for (const state of ["paused", "capped", "waiting_on_you", "off", "future_state"]) {
    assert.doesNotMatch(godModeStatusText({ state }), /is working on/);
  }
  assert.equal(godModeStatusText({ state: "paused", working_on: "Paused. Nothing new starts until you resume." }),
    "Paused. Nothing new starts until you resume.");
  assert.equal(godModeStatusText({ state: "capped", working_on: "Daily credit cap reached." }), "Supymem's current activity could not be confirmed.");
  assert.equal(godModeStatusText({ state: "waiting_on_you", working_on: "Waiting on you: Connect Gmail." }), "Waiting on you: Connect Gmail.");
  assert.equal(godModeStatusText({ state: "future_state", working_on: "done" }), "Supymem's current activity could not be confirmed.");
});

test("only a working state uses active work copy and its real next step", () => {
  assert.equal(godModeStatusText({ state: "working", working_on: "Reviewing the research" }), "Supymem is working on: Reviewing the research");
  assert.equal(godModeStatusText({ state: "working", working_on: "  " }), "Supymem is working on: looking for the next step");
});


test("durable queue, failure and idle states do not become active execution claims", () => {
  for (const state of ["queued", "idle", "failed", "needs_you", "cancelled", "completed"]) {
    const detail = `Observed ${state}`;
    assert.equal(godModeStatusText({ state, working_on: detail }), detail);
    assert.doesNotMatch(godModeStatusText({ state }), /is working on/);
  }
});
