import assert from "node:assert/strict";
import { test } from "node:test";
import { getCharlieMascotState, setCharlieMascot } from "./charlieMascot.ts";

test("mascot bridge is desktop-only, clamps levels, throttles updates, and always sends idle", async () => {
  const originalWindow = globalThis.window;
  const originalPerformance = globalThis.performance;
  let now = 0;
  const calls: { command: string; args?: Record<string, unknown> }[] = [];
  try {
    Object.defineProperty(globalThis, "performance", {configurable: true, value: {now: () => now}});
    delete (globalThis as {window?: unknown}).window;
    setCharlieMascot("speaking", 0.2);
    assert.equal(calls.length, 0);
    const bridge = {
      invoke: async (command: string, args?: Record<string, unknown>) => {calls.push({command, args});},
    };
    Object.defineProperty(globalThis, "window", {configurable: true, value: {__TAURI_INTERNALS__: bridge, dispatchEvent() {}}});
    setCharlieMascot("listening");
    setCharlieMascot("listening");
    assert.equal(calls.length, 1);
    setCharlieMascot("speaking", 2);
    assert.deepEqual(calls.at(-1), {command: "charlie_mascot_state", args: {state: "speaking", level: 1}});
    now = 49;
    setCharlieMascot("speaking", 0.4);
    assert.equal(calls.length, 2);
    now = 50;
    setCharlieMascot("speaking", -1);
    assert.equal(calls.at(-1)?.args?.level, 0);
    setCharlieMascot("idle");
    assert.equal(calls.at(-1)?.args?.state, "idle");
    now = 100;
    setCharlieMascot("speaking", NaN);
    assert.equal(calls.at(-1)?.args?.level, null);
    bridge.invoke = async () => {throw new Error("older desktop");};
    setCharlieMascot("idle");
    await Promise.resolve();
    const updates: {state: string; level: number | null}[] = [];
    Object.defineProperty(globalThis, "window", {configurable: true, value: {dispatchEvent(event: CustomEvent) {updates.push(event.detail);}}});
    for (const audience of ["Enterprise inventory analysts", "Family recipe collectors"]) {
      for (const state of ["listening", "speaking", "idle"] as const) {
        now += 100;
        setCharlieMascot(state, state === "speaking" ? 0.4 : null);
        assert.equal(updates.at(-1)?.state, state, audience);
        assert.deepEqual(getCharlieMascotState(), updates.at(-1));
      }
    }
    bridge.invoke = async (command: string, args?: Record<string, unknown>) => {calls.push({command, args});};
    Object.defineProperty(globalThis, "window", {configurable: true, value: {__TAURI_INTERNALS__: bridge, dispatchEvent() {}}});
    const count = calls.length;
    setCharlieMascot("idle");
    assert.equal(calls.length, count + 1);
  } finally {
    Object.defineProperty(globalThis, "window", {configurable: true, value: originalWindow});
    Object.defineProperty(globalThis, "performance", {configurable: true, value: originalPerformance});
  }
});
