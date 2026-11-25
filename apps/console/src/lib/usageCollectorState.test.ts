import assert from "node:assert/strict";
import test from "node:test";
import { listenForUsageSubject, reportUsageCollectorState } from "./usageCollectorState.ts";

function sessionChange(subject: unknown): Event {
  const event = new Event("supymem-session-changed");
  Object.defineProperty(event, "detail", { value: { subject } });
  return event;
}

test("subject changes can restore tracking and a cleared session asks for recheck", () => {
  const target = new EventTarget();
  const received: Array<{ subject?: string; recheck: boolean }> = [];
  const stop = listenForUsageSubject(target as Window, (next) => received.push(next));
  target.dispatchEvent(sessionChange("usr_first"));
  target.dispatchEvent(sessionChange(null));
  target.dispatchEvent(sessionChange("usr_second"));
  target.dispatchEvent(sessionChange("founder"));
  stop();
  target.dispatchEvent(sessionChange("usr_ignored"));
  assert.deepEqual(received, [
    { subject: "usr_first", recheck: false },
    { subject: undefined, recheck: true },
    { subject: "usr_second", recheck: false },
    { subject: "founder", recheck: false },
  ]);
});

test("collector health survives a simulated restart and contains no identity or content", () => {
  const writes: Record<string, string> = {};
  const storage = {
    getItem: (key: string) => writes[key] || null,
    setItem: (key: string, value: string) => { writes[key] = value; },
  };
  reportUsageCollectorState("subject_mismatch", storage);
  reportUsageCollectorState("collecting", storage);
  const history = JSON.parse(writes["supymem.usage.health.v1"]);
  assert.deepEqual(history.map((entry: { state: string }) => entry.state), ["subject_mismatch", "collecting"]);
  for (const entry of history) {
    assert.deepEqual(Object.keys(entry).sort(), ["at", "state"]);
    assert.match(entry.at, /^\d{4}-\d\d-\d\dT/);
  }
});
