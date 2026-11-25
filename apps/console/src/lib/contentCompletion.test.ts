import assert from "node:assert/strict";
import test from "node:test";
import { contentCompletionTracker, contentRefreshFence, contentResultKey, refreshContentViews } from "./contentCompletion.ts";

const run = { run_id: "run", state: "completed", result: { post_id: "post" } };

test("a terminal saved result refreshes once; repeated polling never generates or refreshes again", async () => {
  const tracker = contentCompletionTracker(); let reads = 0;
  const refresh = async () => { reads++; };
  assert.equal(contentResultKey({ ...run, state: "running" }), "");
  await tracker.refresh(contentResultKey(run), refresh, () => true);
  await tracker.refresh(contentResultKey(run), refresh, () => true);
  assert.equal(reads, 1);
});

test("failed list refresh can be retried for the same result without a generation request", async () => {
  const tracker = contentCompletionTracker(); let reads = 0;
  const refresh = async () => { if (++reads === 1) throw new Error("post read unavailable"); };
  await assert.rejects(tracker.refresh(contentResultKey(run), refresh, () => true));
  assert.equal(await tracker.refresh(contentResultKey(run), refresh, () => true), true);
  assert.equal(reads, 2);
});

test("an early terminal state without result cannot consume later completion for the same run", async () => {
  const tracker = contentCompletionTracker(); let reads = 0;
  const refresh = async () => { reads++; };
  await tracker.refresh(contentResultKey({ ...run, state: "failed", result: {} }), refresh, () => true);
  await tracker.refresh(contentResultKey(run), refresh, () => true);
  assert.equal(reads, 1);
});

test("concurrent completion effects share one pending read and invalidated scopes do not acknowledge it", async () => {
  const tracker = contentCompletionTracker(); let done!: () => void, current = true, reads = 0;
  const refresh = () => { reads++; return new Promise<void>(resolve => { done = resolve; }); };
  const first = tracker.refresh(contentResultKey(run), refresh, () => current);
  assert.equal(await tracker.refresh(contentResultKey(run), refresh, () => current), false);
  current = false; done(); assert.equal(await first, false); assert.equal(reads, 1);
});

test("slow initial refresh cannot overwrite a newer completed post list; account change invalidates reads", () => {
  const fence = contentRefreshFence(), initial = fence.next(), completion = fence.next();
  assert.equal(initial(), false); assert.equal(completion(), true);
  fence.invalidate(); assert.equal(completion(), false);
});


test("completed posts become visible without waiting for optional events", async () => {
  let visible: string[] = [], releaseEvents!: () => void;
  const completion = refreshContentViews({ status: Promise.resolve("status"), posts: Promise.resolve(["new saved post"]),
    events: new Promise<void>(resolve => { releaseEvents = resolve; }) },
    { status: () => {}, posts: value => { visible = value; }, events: () => {} }, () => true, true);
  assert.equal(await completion, null);
  assert.deepEqual(visible, ["new saved post"]);
  releaseEvents();
});

test("status failure cannot discard a successful post response, and failed required reads remain retryable", async () => {
  let visible: string[] = [];
  await assert.rejects(refreshContentViews({ status: Promise.reject(new Error("status unavailable")), posts: Promise.resolve(["saved"]),
    events: Promise.reject(new Error("events unavailable")) },
    { status: () => {}, posts: value => { visible = value; }, events: () => {} }, () => true, true), /could not be refreshed/);
  assert.deepEqual(visible, ["saved"]);
});
