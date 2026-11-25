import assert from "node:assert/strict";
import test from "node:test";
import { emitVisibleActivation, qualifiesForExposure, syncExposureObservation, viewportVisibleRatio } from "./usageExposure.ts";

test("unrelated DOM updates do not restart a pending exposure observation", () => {
  const element = {};
  const observed = new WeakSet<object>();
  const calls: string[] = [];
  const observer = {
    observe: () => calls.push("observe"),
    unobserve: () => calls.push("unobserve"),
  };
  syncExposureObservation(element, observed, observer, true);
  syncExposureObservation(element, observed, observer, true);
  syncExposureObservation(element, observed, observer, true);
  assert.deepEqual(calls, ["observe"]);

  syncExposureObservation(element, observed, observer, false);
  syncExposureObservation(element, observed, observer, false);
  syncExposureObservation(element, observed, observer, true);
  assert.deepEqual(calls, ["observe", "unobserve", "observe"]);
});

test("an exposure requires at least a quarter of the control in the viewport", () => {
  assert.equal(qualifiesForExposure(true, 0.01), false);
  assert.equal(qualifiesForExposure(true, 0.24), false);
  assert.equal(qualifiesForExposure(true, 0.25), true);
  assert.equal(qualifiesForExposure(false, 1), false);
});

test("a trusted visible activation emits one exposure before its activation", () => {
  const exposed = new Set<string>();
  const events: string[] = [];
  const ratio = viewportVisibleRatio({ left: 80, top: 80, right: 120, bottom: 120, width: 40, height: 40 },
    { width: 100, height: 100 });
  assert.equal(ratio, 0.25);
  const activate = () => emitVisibleActivation("work_refresh", exposed, true, ratio,
    () => events.push("exposure"), () => events.push("activation"));
  activate();
  activate();
  assert.deepEqual(events, ["exposure", "activation", "activation"]);
});

test("programmatic, offscreen and order-scoped clicks do not invent exposure", () => {
  const cases = [
    { id: "work_refresh", trusted: false, ratio: 1 },
    { id: "work_refresh", trusted: true, ratio: 0.24 },
    { id: "mac_start", trusted: true, ratio: 1 },
  ];
  for (const { id, trusted, ratio } of cases) {
    const events: string[] = [];
    emitVisibleActivation(id, new Set(), trusted, ratio,
      () => events.push("exposure"), () => events.push("activation"));
    assert.deepEqual(events, ["activation"]);
  }
  assert.equal(viewportVisibleRatio({ left: 101, top: 0, right: 141, bottom: 40, width: 40, height: 40 },
    { width: 100, height: 100 }), 0);
});
