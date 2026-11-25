import assert from "node:assert/strict";
import { test } from "node:test";
import { continueLabel, PERMISSIONS, rowAction, screenPermissionAction, macTaskPermissionsGranted, shouldAutoOpenHelperSetup, shouldShowFirstRun } from "./macPermissions.ts";

test("row action maps status to the single primary button", () => {
  assert.deepEqual(rowAction("granted", false), { granted: true, button: null });
  assert.equal(rowAction("not_determined", false).button, "Allow");
  assert.equal(rowAction("not_determined", true).button, "Open System Settings");
  assert.equal(rowAction("denied", false).button, "Open System Settings");
  assert.equal(rowAction("unknown", false).button, "Open System Settings");
});

test("Automation describes a permission for supported reviewed tasks, not an editing capability", () => {
  const automation = PERMISSIONS.find(permission => permission.name === "automation")!;
  assert.match(automation.reason, /Optional.*macOS app automation requests.*supported, reviewed task/);
  assert.doesNotMatch(automation.reason, /Cursor|press Send|ready|background editing/);
  assert.deepEqual(rowAction("granted", false), { granted: true, button: null });
});

test("continue label is limited until every permission is granted", () => {
  const all = Object.fromEntries(PERMISSIONS.map((p) => [p.name, "granted"] as const));
  assert.equal(continueLabel(all), "Continue");
  assert.equal(continueLabel({ ...all, microphone: "denied" }), "Continue (I can turn the rest on later)");
  assert.equal(continueLabel(null), "Continue (I can turn the rest on later)");
});

test("first run shows only in the desktop app, once, and not on the setup page", () => {
  assert.equal(shouldShowFirstRun(true, false, "/"), true);
  assert.equal(shouldShowFirstRun(false, false, "/"), false);
  assert.equal(shouldShowFirstRun(true, true, "/"), false);
  assert.equal(shouldShowFirstRun(true, false, "/setup/permissions"), false);
});

test("helper modal never auto-opens while status is unavailable or before first run is done", () => {
  const ok = { screen_recording: true, accessibility: true, event_synthesizing: true };
  assert.equal(shouldAutoOpenHelperSetup({ status_unavailable: true }, true), false);
  assert.equal(shouldAutoOpenHelperSetup(null, true), false);
  assert.equal(shouldAutoOpenHelperSetup({ ...ok, accessibility: false }, false), false);
  assert.equal(shouldAutoOpenHelperSetup(ok, true), false);
  assert.equal(shouldAutoOpenHelperSetup({ ...ok, accessibility: false }, true, true), true);
  // Beta default: computer use is off, so a missing permission never opens the modal.
  assert.equal(shouldAutoOpenHelperSetup({ ...ok, accessibility: false }, true), false);
});


test("screen permission only offers a first explicit grant, and unknown status stays passive", () => {
  assert.equal(screenPermissionAction("granted", false), "granted");
  assert.equal(screenPermissionAction("not_determined", false), "grant");
  assert.equal(screenPermissionAction("not_determined", true), "settings");
  assert.equal(screenPermissionAction("denied", false), "settings");
  assert.equal(screenPermissionAction("unknown", false), "retry");
  assert.equal(screenPermissionAction("unknown", true), "retry");
  assert.equal(macTaskPermissionsGranted({"screen-recording": "granted", accessibility: "granted"}), true);
  assert.equal(macTaskPermissionsGranted({"screen-recording": "granted"}), false);
  assert.equal(macTaskPermissionsGranted(null), false);
});
