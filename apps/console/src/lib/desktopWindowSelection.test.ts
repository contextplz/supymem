import test from "node:test";
import assert from "node:assert/strict";
import { chooseDesktopWindow, type RunningWindow } from "./desktopWindowSelection.ts";

const window = (window_id: number, title: string): RunningWindow => ({
  window_id, title, on_screen: true, width: 1200, height: 800,
});

test("chooses a blank workspace instead of an unrelated account window", () => {
  assert.equal(chooseDesktopWindow([
    window(177, "Connect Supymem for Mac"), window(178, "New Tab"),
  ])?.window_id, 178);
});

test("refuses to select a sign-in window as the sole task target", () => {
  assert.equal(chooseDesktopWindow([window(177, "Connect Supymem for Mac")]), null);
  assert.equal(chooseDesktopWindow([window(178, "Sign in - Google Accounts")]), null);
});

test("retains ordinary app windows when no blank window exists", () => {
  assert.equal(chooseDesktopWindow([window(20, "Notes"), window(30, "Calculator")])?.window_id, 20);
});
