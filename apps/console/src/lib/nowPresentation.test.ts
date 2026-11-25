import assert from "node:assert/strict";
import test from "node:test";
import { idleWorkMessage } from "./nowPresentation.ts";

test("an open plan item does not imply work has started", () => {
  const text = idleWorkMessage({
    nextTitle: "Launch the waitlist test",
    statusCode: "readiness_review",
  });
  assert.match(text, /saved plan/i);
  assert.match(text, /no task is running/i);
  assert.doesNotMatch(text, /autopilot/i);
});

test("a live landing page does not start remaining plan work", () => {
  const text = idleWorkMessage({ nextTitle: "Review signups", statusCode: "live" });
  assert.match(text, /saved plan/i);
  assert.doesNotMatch(text, /autopilot|queued/i);
});

test("empty queues and paused ventures are explicit", () => {
  assert.equal(idleWorkMessage({}), "The queue is empty.");
  assert.match(
    idleWorkMessage({ nextTitle: "Launch", statusCode: "paused" }),
    /paused/i,
  );
});
