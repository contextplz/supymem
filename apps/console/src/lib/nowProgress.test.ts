import assert from "node:assert/strict";
import test from "node:test";
import { currentBackgroundWork, stoppedRun } from "./nowProgress.ts";

const job = { job_id: "j1", stage: "Validation", activity: "Challenging the idea", state: "working" };

test("background work must belong to the venture and be well formed", () => {
  const ok = { venture: { id: "v1" }, run_status: { background_work: [job] } };
  assert.equal(currentBackgroundWork(ok, "v1")?.length, 1);
  assert.equal(currentBackgroundWork(ok, "v2"), null);
  assert.equal(currentBackgroundWork({ venture: { id: "v1" }, run_status: { background_work: [{ ...job, state: "x" }] } }, "v1"), null);
  assert.deepEqual(currentBackgroundWork({ venture: { id: "v1" }, run_status: { background_work: [] } }, "v1"), []);
});

test("failed and paused runs are surfaced, idle is not", () => {
  assert.equal(stoppedRun({ run_status: { state: "failed", stage: "Validation", activity: "Needs attention" } })?.state, "failed");
  assert.equal(stoppedRun({ run_status: { state: "idle" } }), null);
});

test("a failed run keeps its plain reason and a same-site action", () => {
  const run = (href: string) => stoppedRun({ run_status: { state: "failed", stage: "Clarification", activity: "You're out of credits.", action: { label: "Request credits", href } } });
  assert.deepEqual(run("/v/x/settings")?.action, { label: "Request credits", href: "/v/x/settings" });
  assert.equal(run("//evil.example")?.action, undefined);
});
