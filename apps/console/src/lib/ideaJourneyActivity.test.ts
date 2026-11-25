import assert from "node:assert/strict";
import { test } from "node:test";
import { ideaJourneyActivity } from "./ideaJourneyActivity.ts";

test("job completion remains a saved-status read until its next question has loaded", async () => {
  let state = { busy: false, jobId: "job_a" as string | null, reconciling: false };
  assert.equal(ideaJourneyActivity(state), "working");
  let release!: () => void;
  const read = new Promise<void>(resolve => { release = resolve; });
  const reload = async () => {
    state = { ...state, jobId: null, reconciling: true };
    try { await read; } finally { state = { ...state, reconciling: false }; }
  };
  const pending = reload();
  assert.equal(ideaJourneyActivity(state), "checking");
  release();
  await pending;
  assert.equal(ideaJourneyActivity(state), "idle");
});

test("cached session hydration does not imply no task exists before jobs were restored", () => {
  assert.equal(ideaJourneyActivity({ busy: false, jobId: null, reconciling: true }), "checking");
  assert.equal(ideaJourneyActivity({ busy: false, jobId: "job_restored", reconciling: true }), "working");
});

test("local save retains its concrete activity while reconciliation is in flight", () => {
  assert.equal(ideaJourneyActivity({ busy: true, jobId: null, reconciling: true }), "saving");
});

test("a visible question or result retires preparing copy but never hides an in-flight save", () => {
  const state = { busy: false, jobId: "old-job", reconciling: true, hasNextStep: true };
  assert.equal(ideaJourneyActivity(state), "idle");
  assert.equal(ideaJourneyActivity({ ...state, busy: true }), "saving");
});
