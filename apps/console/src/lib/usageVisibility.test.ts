import assert from "node:assert/strict";
import test from "node:test";
import { interactionBackedScreenView, shouldCollectUsageEvent } from "./usageVisibility.ts";

test("background Mac clicks count without treating hidden time as active use", () => {
  assert.equal(shouldCollectUsageEvent(true, "feature.used"), true);
  assert.equal(shouldCollectUsageEvent(true, "ui.control_activate"), true);
  assert.equal(shouldCollectUsageEvent(true, "ui.navigation"), true);
  assert.equal(shouldCollectUsageEvent(true, "ui.screen_view"), false);
  assert.equal(shouldCollectUsageEvent(true, "session.active"), false);
  assert.equal(shouldCollectUsageEvent(true, "ui.control_exposure"), false);
  assert.equal(shouldCollectUsageEvent(true, "ui.control_exposure", true), true);
  assert.equal(shouldCollectUsageEvent(true, "ui.screen_view", true), true);
  assert.equal(shouldCollectUsageEvent(false, "ui.screen_view"), true);
});

test("a recent trusted native interaction permits only a bounded hidden route view", () => {
  assert.equal(interactionBackedScreenView(true, true, 1000, 11000), true);
  assert.equal(interactionBackedScreenView(true, true, 1000, 11001), false);
  assert.equal(interactionBackedScreenView(true, false, 1000, 1001), false);
  assert.equal(interactionBackedScreenView(false, true, 1000, 1001), false);
  assert.equal(interactionBackedScreenView(true, true, 0, 1001), false);
  assert.equal(interactionBackedScreenView(true, true, 2000, 1001), false);
  assert.equal(shouldCollectUsageEvent(true, "session.active", true), false);
});
