import assert from "node:assert/strict";
import { test } from "node:test";
import {
  replayRequested, audioLevel, cornerTransform, decodeBase64, mascotPose, parseGreeting, shouldShowIntro, smoothLevel, speakWithSystemVoice, playbackFailure,
} from "./charlieGreeting.ts";

test("parseGreeting validates and first-launch eligibility keeps the once-per-account gate", () => {
  assert.equal(parseGreeting(null), null);
  assert.equal(parseGreeting({ text: " " }), null);
  assert.equal(parseGreeting({ text: "Hey", greeted_at: 123 }), null);
  assert.equal(parseGreeting({ text: "Hey", first_name: {} }), null);
  const g = parseGreeting({ text: "Hey Ada", first_name: "Ada", greeted_at: null });
  assert.deepEqual(g, { text: "Hey Ada", firstName: "Ada", greetedAt: null });
  assert.equal(shouldShowIntro(true, g), true);
  assert.equal(shouldShowIntro(false, g), false);
  assert.equal(shouldShowIntro(true, null), false);
  assert.equal(shouldShowIntro(true, parseGreeting({ text: "x", greeted_at: "2026-01-01" })), false);
});

test("audioLevel is 0 for silence, rises with amplitude and is capped", () => {
  assert.equal(audioLevel(new Uint8Array(0)), 0);
  assert.equal(audioLevel(new Uint8Array(64).fill(128)), 0);
  const quiet = audioLevel(new Uint8Array(64).fill(138));
  const loud = audioLevel(new Uint8Array(64).fill(200));
  assert.ok(quiet > 0 && loud > quiet);
  assert.equal(audioLevel(new Uint8Array(64).fill(255)), 1);
});

test("smoothLevel attacks faster than it releases; mascotPose stays gentle and clamped", () => {
  assert.ok(smoothLevel(0, 1) - 0 > 1 - smoothLevel(1, 0));
  assert.deepEqual(mascotPose(0), { scale: 1, bobPx: -0 });
  assert.equal(mascotPose(5).scale, mascotPose(1).scale);
  assert.ok(mascotPose(1).scale <= 1.1 + 1e-9);
});

test("cornerTransform lands the centered mascot in the bottom-right corner", () => {
  const t = cornerTransform({ width: 1000, height: 800 }, 320, 64, 24);
  assert.equal(t.scale, 0.2);
  assert.equal(500 + t.dx + 32, 1000 - 24);
  assert.equal(400 + t.dy + 32, 800 - 24);
});

test("decodeBase64 round-trips bytes", () => {
  assert.deepEqual([...decodeBase64("SUQzAP8=")], [0x49, 0x44, 0x33, 0x00, 0xff]);
});

test("replay override shows the intro for a greeted account and reads query or storage", () => {
  const greeted = parseGreeting({ text: "x", greeted_at: "2026-01-01" });
  assert.equal(shouldShowIntro(true, greeted, true), true);
  assert.equal(shouldShowIntro(false, greeted, true), false);
  assert.equal(replayRequested("?replayIntro=1", null), true);
  assert.equal(replayRequested("", "1"), true);
  assert.equal(replayRequested("?replayIntro=0", null), false);
});

test("speakWithSystemVoice speaks via the OS engine, and reports unavailability instead of failing silently", () => {
  const spoken: string[] = [];
  const synth = { speak: (u: unknown) => spoken.push((u as { t: string }).t), cancel() {} };
  class U { t: string; onend: unknown; onerror: unknown; constructor(t: string) { this.t = t; } }
  assert.equal(speakWithSystemVoice("Hey there", synth, U, () => {}), true);
  assert.deepEqual(spoken, ["Hey there"]);
  assert.equal(speakWithSystemVoice("Hey", undefined, U, () => {}), false);
  assert.equal(speakWithSystemVoice("Hey", synth, undefined, () => {}), false);
  assert.equal(speakWithSystemVoice("  ", synth, U, () => {}), false);
  const boom = { speak() { throw new Error("blocked"); }, cancel() {} };
  assert.equal(speakWithSystemVoice("Hey", boom, U, () => {}), false);
});

test("playbackFailure refuses silent playback and names the reason", () => {
  assert.equal(playbackFailure("suspended", true), "ctx_suspended");
  assert.equal(playbackFailure("interrupted", true), "ctx_suspended");
  assert.equal(playbackFailure("running", false), "decode_failed");
  assert.equal(playbackFailure("running", true), null);
});

test("system speech failures never invoke completion", () => {
  let completed = 0, failed = 0, utterance: {onend?: () => void; onerror?: () => void} = {};
  class Utterance { onend: unknown; onerror: unknown; constructor(_text: string) {} }
  assert.equal(speakWithSystemVoice("Hey Ada", {cancel() {}, speak: value => { utterance = value as typeof utterance; }},
    Utterance, () => { completed++; }, () => { failed++; }), true);
  utterance.onerror?.(); assert.equal(failed, 1); assert.equal(completed, 0);
  utterance.onend?.(); assert.equal(completed, 1);
});
