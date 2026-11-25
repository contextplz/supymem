import assert from "node:assert/strict";
import { test } from "node:test";
import { NEW_TRACK, isLongPress, isOverCap, isSilent, levelFromSamples, nextPhase, parseRealtimeEvent, requestResponse, requeueOnConflict, responseEnded, VAD_ON } from "./voice.ts";

test("response.create is queued while a response is active and sent once on response.done", () => {
  let t = NEW_TRACK;
  let r = requestResponse(t); t = r.t;
  assert.equal(r.send, true);
  r = requestResponse(t); t = r.t; // while active: queued, not sent
  assert.equal(r.send, false);
  r = requestResponse(t); t = r.t; // duplicates collapse into one
  assert.equal(r.send, false);
  r = responseEnded(t); t = r.t;
  assert.equal(r.send, true);
  assert.equal(t.responseActive, true);
  r = responseEnded(t); // queued response finished: nothing more
  assert.equal(r.send, false);
  assert.equal(r.t.responseActive, false);
});

test("a create that loses a race with a server-started response is re-queued and its error is silent", () => {
  const msg = "Conversation already has an active response in progress: resp_1. Wait until the response is finished before creating a new one.";
  const ev = parseRealtimeEvent(JSON.stringify({ type: "error", error: { code: "conversation_already_has_active_response", message: msg } }));
  assert.equal((ev as { benign: boolean }).benign, true);
  const t = requeueOnConflict(NEW_TRACK, msg);
  assert.deepEqual([t.responseActive, t.createQueued], [true, true]);
  assert.equal(responseEnded(t).send, true);
  assert.equal(requeueOnConflict(NEW_TRACK, "boom"), NEW_TRACK);
});

test("state machine walks a full turn and ends", () => {
  let p = nextPhase("idle", "connect");
  assert.equal(p, "connecting");
  p = nextPhase(p, "connected"); assert.equal(p, "listening");
  p = nextPhase(p, "speech_stopped"); assert.equal(p, "thinking");
  p = nextPhase(p, "reply_started"); assert.equal(p, "speaking");
  p = nextPhase(p, "speech_started"); assert.equal(p, "listening"); // barge-in
  p = nextPhase(p, "reply_started");
  p = nextPhase(p, "reply_stopped"); assert.equal(p, "listening");
  assert.equal(nextPhase("speaking", "end"), "idle");
  assert.equal(nextPhase("idle", "reply_started"), "idle");
  assert.equal(nextPhase("listening", "tool_start"), "thinking");
});

test("tap vs long press", () => {
  assert.equal(isLongPress(399), false);
  assert.equal(isLongPress(401), true);
});

test("silence and hard cap", () => {
  assert.equal(isSilent(59_999, 0), false);
  assert.equal(isSilent(60_000, 0), true);
  assert.equal(isOverCap(10 * 60_000 - 1, 0), false);
  assert.equal(isOverCap(10 * 60_000, 0), true);
  assert.equal(isOverCap(1000, 0), false);
});

test("levels and events", () => {
  assert.equal(levelFromSamples(new Uint8Array(8).fill(128)), 0);
  assert.ok(levelFromSamples(new Uint8Array([255, 0, 255, 0])) > 0.9);
  assert.deepEqual(parseRealtimeEvent('{"type":"input_audio_buffer.speech_started"}'), { kind: "signal", signal: "speech_started" });
  assert.equal(VAD_ON.type, "semantic_vad");
});

import { AUTO_END_MS, autoEndDue, bargeInEvents, isIdleAfterReply, track } from "./voice.ts";
import { applyCaption } from "./voiceTranscript.ts";

test("cancel only while a response is active; no-active-response error is benign", () => {
  assert.deepEqual(bargeInEvents(false).map((e) => e.type), ["output_audio_buffer.clear"]);
  assert.deepEqual(bargeInEvents(true).map((e) => e.type), ["response.cancel", "output_audio_buffer.clear"]);
  let t = track(NEW_TRACK, "response_created");
  assert.equal(t.responseActive, true);
  t = track(t, "response_done"); // audio still playing after response.done: race
  t = track(t, "audio_started");
  assert.equal(bargeInEvents(t.responseActive).length, 1);
  const ev = parseRealtimeEvent('{"type":"error","error":{"code":"response_cancel_not_active","message":"Cancellation failed: no active response found"}}');
  assert.deepEqual(ev, { kind: "error", message: "Cancellation failed: no active response found", benign: true, code: "response_cancel_not_active" });
  assert.equal((parseRealtimeEvent('{"type":"error","error":{"message":"boom"}}') as { benign: boolean }).benign, false);
  assert.deepEqual(parseRealtimeEvent('{"type":"response.done"}'), { kind: "response", state: "done" });
});

test("auto-end only after reply fully finished and nothing in flight", () => {
  let t = NEW_TRACK;
  assert.equal(isIdleAfterReply(t), false); // nothing said yet
  t = track(track(t, "response_created"), "audio_started");
  t = track(t, "response_done");
  assert.equal(isIdleAfterReply(t), false); // still speaking
  t = track(t, "audio_stopped");
  assert.equal(isIdleAfterReply(t), true);
  assert.equal(isIdleAfterReply(track(t, "tool_start")), false);
  assert.equal(isIdleAfterReply(track(t, "speech_started")), false);
  assert.equal(isIdleAfterReply(track(t, "response_created")), false);
  assert.equal(autoEndDue(null, 99_999), false);
  assert.equal(autoEndDue(1000, 1000 + AUTO_END_MS - 1), false);
  assert.equal(autoEndDue(1000, 1000 + AUTO_END_MS), true);
});

test("live voice captions stream in place", () => {
  let n = 0;
  const id = () => `t${++n}`;
  let r = applyCaption([], "founder", "Hello ", false, id);
  r = applyCaption(r.turns, "founder", "there", false, id);
  assert.equal(r.turns.length, 1);
  assert.equal(r.turn.text, "Hello there");
  r = applyCaption(r.turns, "founder", "Hello there.", true, id);
  assert.equal(r.turn.final, true);
  r = applyCaption(r.turns, "assistant", "Hi", false, id);
  r = applyCaption(r.turns, "assistant", "Hi, founder.", true, id);
  assert.deepEqual(r.turns.map((t) => [t.role, t.text, t.final]), [["founder", "Hello there.", true], ["assistant", "Hi, founder.", true]]);
  r = applyCaption(r.turns, "founder", "Next", true, id); // a new final-only turn
  assert.equal(r.turns.length, 3);
});

import { founderTurn, isRealSpeech, shouldInterruptPlayback } from "./voice.ts";

test("noise never cuts Charlie off or makes him answer; real words do", () => {
  assert.deepEqual([VAD_ON.interrupt_response, VAD_ON.create_response], [false, false]);
  for (const noise of ["", " . ", "Uh.", "hmm", "um uh", "You", "thanks for watching", "a"]) assert.equal(isRealSpeech(noise), false, noise);
  for (const words of ["No", "stop", "OK", "Thank you.", "thanks", "bye", "ok bye", "What about pricing?", "¿Qué precio?"]) assert.equal(isRealSpeech(words), true, words);

  const talking = { ...track(NEW_TRACK, "audio_started"), responseActive: true };
  const noise = founderTurn(talking, "item_1", "hmm");
  assert.equal(noise.respond, false);
  assert.deepEqual(noise.events, [{ type: "conversation.item.delete", item_id: "item_1" }]);

  const cutIn = founderTurn(talking, "item_2", "Wait, what about pricing?");
  assert.equal(cutIn.respond, true);
  assert.deepEqual(cutIn.events.map((e) => (e as { type: string }).type), ["response.cancel", "output_audio_buffer.clear"]);

  // Audio still playing after the response finished: flush audio, nothing to cancel.
  const tail = founderTurn(track(NEW_TRACK, "audio_started"), "i", "next question");
  assert.deepEqual(tail.events.map((e) => (e as { type: string }).type), ["output_audio_buffer.clear"]);

  assert.deepEqual(founderTurn(NEW_TRACK, "i", "hello there"), { respond: true, events: [] });
  // Failed transcript never authorizes a response or write.
  assert.equal(founderTurn(NEW_TRACK, "i", null).respond, false);
  assert.equal(founderTurn(talking, "i", null).respond, false);
});

test("hallucinated acknowledgements are accepted only while Charlie is quiet and never barge in", () => {
  const alwaysNoise = ["You.", "Thanks for watching!"];
  const acknowledgements = ["Thank you.", "Thanks!", "Bye.", "OK bye"];
  const oldNoise = ["", " . ", "Uh.", "hmm", "um uh", "You", "Thank you.", "bye", "thanks for watching", "a"];
  for (const phrase of alwaysNoise) {
    assert.equal(isRealSpeech(phrase), false, phrase);
    assert.deepEqual(founderTurn(NEW_TRACK, "item", phrase),
      {respond: false, events: [{type: "conversation.item.delete", item_id: "item"}]});
  }
  for (const phrase of acknowledgements) {
    assert.deepEqual(founderTurn(NEW_TRACK, "item", phrase), {respond: true, events: []}, phrase);
  }
  for (const state of ["audioPlaying", "audioPending", "responseActive"] as const) {
    const talking = {...NEW_TRACK, [state]: true};
    for (const phrase of [...oldNoise, ...acknowledgements]) {
      assert.equal(isRealSpeech(phrase, true), false, `${state}: ${phrase}`);
      assert.deepEqual(founderTurn(talking, "item", phrase),
        {respond: false, events: [{type: "conversation.item.delete", item_id: "item"}]}, `${state}: ${phrase}`);
    }
  }
  for (const phrase of [...alwaysNoise, ...acknowledgements]) {
    for (const duration of [0, 600, 5000]) assert.equal(shouldInterruptPlayback(phrase, duration), false, phrase);
  }
  assert.equal(shouldInterruptPlayback("Thank you, change the price", 0), true);
});

test("transcripts carry the item id, and ignored noise returns to listening", () => {
  assert.deepEqual(parseRealtimeEvent('{"type":"conversation.item.input_audio_transcription.completed","item_id":"it","transcript":"hi"}'),
    { kind: "caption", text: "hi", final: true, itemId: "it" });
  assert.deepEqual(parseRealtimeEvent('{"type":"conversation.item.input_audio_transcription.failed","item_id":"it"}'),
    { kind: "caption", text: "", final: true, itemId: "it", failed: true });
  assert.equal(nextPhase("thinking", "noise"), "listening");
  assert.equal(nextPhase("speaking", "noise"), "speaking");
});
