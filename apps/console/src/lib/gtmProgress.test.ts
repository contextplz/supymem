import assert from "node:assert/strict";
import test from "node:test";
import { gtmProgressOwner, shareGtmProgress, subscribeGtmProgress } from "./gtmProgress.ts";

test("banner and card share monotonic progress, with owner isolation and new-run reset", () => {
  const owner = gtmProgressOwner("founder", 1, "physio");
  const base = { run_id: "run1", started_at: "2026-10-15T09:00:00Z", state: "running", step_label: "Preparing outreach drafts" };
  const banner: number[] = [], card: number[] = [], foreign: number[] = [];
  const stops = [subscribeGtmProgress(owner, progress => banner.push(progress.percent!)),
    subscribeGtmProgress(owner, progress => card.push(progress.percent!)),
    subscribeGtmProgress(gtmProgressOwner("other", 1, "physio"), progress => foreign.push(progress.percent!))];
  shareGtmProgress(owner, { ...base, percent: 94 });
  shareGtmProgress(owner, { ...base, percent: 99 });
  assert.equal(shareGtmProgress(owner, { ...base, percent: 95 }).percent, 99);
  assert.deepEqual(banner, card);
  assert.deepEqual(card, [94, 99, 99]);
  assert.deepEqual(foreign, []);
  const newRun = { ...base, run_id: "run2", started_at: "2026-10-15T09:10:00Z", percent: 0 };
  shareGtmProgress(owner, newRun);
  assert.equal(shareGtmProgress(owner, { ...base, percent: 100 }).percent, 0);
  assert.deepEqual(banner, card);
  stops.forEach(stop => stop());
});
