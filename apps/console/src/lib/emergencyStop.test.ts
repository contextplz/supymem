import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { chatStopApplies, emergencyStop, isStopCommand, stopButtonVisible, stopNotice } from "./emergencyStop.ts";

const read = (path: string) => fs.readFileSync(new URL(path, import.meta.url), "utf8");

test("the red Stop shows only in the Mac app while a device task runs", () => {
  assert.equal(stopButtonVisible(true, true), true);
  assert.equal(stopButtonVisible(true, false), false);
  assert.equal(stopButtonVisible(false, true), false, "the web console cannot stop the Mac");
  assert.equal(stopButtonVisible(false, false), false);
});

test("stop and emergency stop are commands; sentences that merely contain the word are not", () => {
  for (const text of ["stop", "Stop", " STOP! ", "emergency stop", "Emergency-stop.", "please stop", "stop computer use",
    "stop the browser task", "stop the mac task", "stop everything", "stop now"]) assert.equal(isStopCommand(text), true, text);
  for (const text of ["", "don't stop", "stop the build", "can you stop at the pricing page", "bus stop locations", "stopwatch app idea",
    "emergency stop button design for my app"]) assert.equal(isStopCommand(text), false, text);
});

test("chat only treats stop as the emergency stop when something is running in the Mac app", () => {
  assert.equal(chatStopApplies("stop", true, true), true);
  assert.equal(chatStopApplies("stop", true, false), false, "idle: let chat answer");
  assert.equal(chatStopApplies("stop", false, true), false);
  assert.equal(chatStopApplies("hello", true, true), false);
});

test("every entry point calls the one native stop command with its source", async () => {
  const calls: unknown[][] = [];
  const invoke = (async (...args: unknown[]) => { calls.push(args); return { status: "stopped_by_user", source: "chat", bridge: "stopped", order_id: "o" }; }) as never;
  const receipt = await emergencyStop("chat", invoke);
  assert.deepEqual(calls, [["emergency_stop_computer_use", { source: "chat" }]]);
  assert.equal(receipt.status, "stopped_by_user");
  assert.match(stopNotice(receipt), /^Stopped/);
  assert.match(stopNotice({ ...receipt, bridge: "idle" }), /Nothing was running/);
  assert.match(stopNotice(null), /⌃⌥⌘\./);
});

test("watching computer use is quiet: an older Mac app without the command logs no native_error", () => {
  const src = read("./emergencyStop.ts");
  assert.match(src, /desktopInvokeQuiet<\{ active\?: boolean \}>\("computer_use_status"\)/);
  assert.doesNotMatch(src, /\bdesktopInvoke</);
});

test("the status bar and chat dock render the shared button, and chat routes stop to it", () => {
  assert.match(read("../components/layout/RunStatusBar.tsx"), /<EmergencyStopButton \/>/);
  const dock = read("../components/chat/ChatDock.tsx");
  assert.match(dock, /<EmergencyStopButton\b[^>]*\/>/);
  assert.match(dock, /<EmergencyStopButton className=\{homePanel \? styles\.emergencyStop : undefined\} \/>/);
  assert.match(dock, /chatStopApplies\(body \?\? composer\.draft, true, computerUseActive\)[\s\S]*emergencyStop\("chat"\)/);
  const button = read("../components/desktop/EmergencyStopButton.tsx");
  assert.match(button, /emergencyStop\("app"\)/);
  assert.match(button, /if \(!active && state === "idle"\) return null/);
});
