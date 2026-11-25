import test from "node:test";
import assert from "node:assert/strict";
import { chatTaskRunId, chatTaskRunPath } from "./chatTaskRun.ts";

test("routes a saved chat task through the regular task runner", () => {
  const payload = { task_id: "tsk_abc123" };
  assert.equal(chatTaskRunId(payload), "tsk_abc123");
  assert.equal(
    chatTaskRunPath("ven_123", payload),
    "/api/ventures/ven_123/tasks/tsk_abc123/run",
  );
});

test("rejects missing and malformed task ids instead of calling chat confirm", () => {
  assert.equal(chatTaskRunPath("ven_123", undefined), null);
  assert.equal(chatTaskRunPath("ven_123", { task_id: "../../other" }), null);
  assert.equal(chatTaskRunPath("ven_123", { task_id: 42 }), null);
});
