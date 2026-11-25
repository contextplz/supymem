import assert from "node:assert/strict";
import test from "node:test";
import { planSend, takeNext } from "./chatSendQueue.ts";

test("a message sent while another is in flight is queued, in order, not dropped", () => {
  let queue: string[] = [];
  assert.deepEqual(planSend(false, queue, "first"), { sendNow: true, queue: [] });
  ({ queue } = planSend(true, queue, " switch to Club Attendance "));
  ({ queue } = planSend(true, queue, "and then status"));
  assert.deepEqual(queue, ["switch to Club Attendance", "and then status"]);
  const one = takeNext(queue);
  assert.equal(one.next, "switch to Club Attendance");
  assert.equal(takeNext(one.queue).next, "and then status");
  assert.equal(takeNext([]).next, null);
});

test("empty text is never queued", () => {
  assert.deepEqual(planSend(true, [], "   "), { sendNow: false, queue: [] });
});
