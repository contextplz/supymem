import test from "node:test";
import assert from "node:assert/strict";
import { chooseNamedDesktopApp } from "./desktopAppMention.ts";

const apps = [
  { name: "Supymem", bundle_id: "com.companyos.app" },
  { name: "TextEdit", bundle_id: "com.apple.TextEdit" },
  { name: "Notes", bundle_id: "com.apple.Notes" },
];

test("explicit app command wins over an app name inside output text", () => {
  assert.equal(chooseNamedDesktopApp(
    "On this Mac, open TextEdit and type Supymem native action check. Do not save.", apps,
  )?.bundle_id, "com.apple.TextEdit");
});

test("one ordinary app mention remains enough", () => {
  assert.equal(chooseNamedDesktopApp("Check my Notes", apps)?.bundle_id, "com.apple.Notes");
  assert.equal(chooseNamedDesktopApp("In TextEdit, click Cancel on the visible Open dialog", apps)?.bundle_id,
    "com.apple.TextEdit");
  assert.equal(chooseNamedDesktopApp("If TextEdit is showing an Open dialog, click Cancel", apps)?.bundle_id,
    "com.apple.TextEdit");
  assert.equal(chooseNamedDesktopApp("In TextEdit's current Open dialog on this Mac, click Cancel", apps)?.bundle_id,
    "com.apple.TextEdit");
  assert.equal(chooseNamedDesktopApp("Inside Notes’ visible window, inspect the selection", apps)?.bundle_id,
    "com.apple.Notes");
  assert.equal(chooseNamedDesktopApp("In the current Open dialog in TextEdit, click Cancel", apps)?.bundle_id,
    "com.apple.TextEdit");
});

test("an app-scoped command with multiple installed apps cannot select either window", () => {
  assert.equal(chooseNamedDesktopApp("In TextEdit and Notes, click Cancel", apps), null);
  assert.equal(chooseNamedDesktopApp("In Finder, click Cancel in TextEdit", apps), null);
  assert.equal(chooseNamedDesktopApp("If Finder is showing a dialog, click Cancel in TextEdit", apps), null);
  assert.equal(chooseNamedDesktopApp("If TextEdit shows Notes, click Cancel", apps), null);
  assert.equal(chooseNamedDesktopApp("In TextEdit's Notes window, click Cancel", apps), null);
  assert.equal(chooseNamedDesktopApp("In the current dialog in Finder, click Cancel in TextEdit", apps), null);
  assert.equal(chooseNamedDesktopApp("In the document, click Cancel in TextEdit", apps), null);
});

test("two explicitly requested apps remain ambiguous at this single-app review boundary", () => {
  assert.equal(chooseNamedDesktopApp("Open TextEdit, then open Notes", apps), null);
});

test("an unavailable explicit app never turns an incidental Supymem mention into authority", () => {
  assert.equal(chooseNamedDesktopApp(
    "Open Finder and inspect Applications. Supymem is only the chat app.", apps,
  ), null);
});
