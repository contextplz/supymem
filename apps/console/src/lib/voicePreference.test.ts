import assert from "node:assert/strict";
import test from "node:test";
import { prefersVoice, rememberInputMode } from "./voicePreference.ts";

test("voice is the device default; an explicit typing choice survives reopening", () => {
  const values = new Map<string, string>();
  Object.assign(globalThis, {window: {localStorage: {getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value)}}});
  assert.equal(prefersVoice(), true);
  rememberInputMode("typing");
  assert.equal(prefersVoice(), false);
  rememberInputMode("voice");
  assert.equal(prefersVoice(), true);
});

test("blocked localStorage never prevents the founder from using chat", () => {
  Object.assign(globalThis, {window: {get localStorage() {throw new Error("blocked");}}});
  assert.equal(prefersVoice(), true);
  assert.doesNotThrow(() => rememberInputMode("typing"));
});
