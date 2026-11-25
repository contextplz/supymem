import assert from "node:assert/strict";
import test from "node:test";
import { chatFlight, waitForChatRequest } from "./chatStop.ts";

test("Stop aborts the old request and immediately accepts a correction; late completion cannot own it", () => {
  const flight = chatFlight();
  const old = flight.begin();
  flight.stop();
  const corrected = flight.begin();
  assert.equal(old.signal.aborted, true);
  assert.equal(flight.owns(old), false);
  flight.finish(old);
  assert.equal(flight.owns(corrected), true);
  flight.finish(corrected);
  assert.equal(flight.owns(corrected), false);
});

test("Stop releases a blocked native wait without dispatching again or losing its original outcome", async () => {
  const controller = new AbortController();
  let finish: (result: string) => void = () => {};
  const original = new Promise<string>(resolve => {finish = resolve;});
  const waiting = waitForChatRequest(original, controller.signal);
  controller.abort();
  await assert.rejects(waiting, {name: "AbortError"});
  finish("settled once");
  assert.equal(await original, "settled once");
});
