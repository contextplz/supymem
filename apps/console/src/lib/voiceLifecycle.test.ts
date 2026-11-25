import assert from "node:assert/strict";
import { test } from "node:test";
import { canHoldSpace, firstVoiceEvent, NEW_TRACK, parseRealtimeEvent, responseEnded } from "./voice.ts";

test("failed and incomplete completions never count as successful replies", () => {
  for (const status of ["failed", "incomplete"]) {
    const event = parseRealtimeEvent(JSON.stringify({type: "response.done", response: {id: "response", status}}));
    assert.deepEqual(event, {kind: "response", state: "done", responseId: "response", failed: true});
    assert.equal(responseEnded({...NEW_TRACK, responseActive: true}, false).t.hadReply, false);
  }
  assert.equal(responseEnded({...NEW_TRACK, responseActive: true}).t.hadReply, true);
  assert.deepEqual(parseRealtimeEvent('{"type":"output_audio_buffer.started","response_id":"filler"}'),
    {kind: "signal", signal: "reply_started", responseId: "filler"});
});

test("hold Space leaves inputs, editors, links, buttons and modified shortcuts alone", () => {
  const key = { code: "Space", repeat: false, ctrlKey: false, altKey: false, metaKey: false };
  assert.equal(canHoldSpace({ ...key, target: { closest: () => null } }), true);
  for (const matched of ["input", "textarea", "contenteditable", "role=textbox", "button", "a"]) {
    assert.equal(canHoldSpace({ ...key, target: { closest: () => matched } }), false);
  }
  for (const extra of [{ repeat: true }, { ctrlKey: true }, { altKey: true }, { metaKey: true }, { code: "Enter" }]) {
    assert.equal(canHoldSpace({ ...key, ...extra, target: null }), false);
  }
});

test("response identities and replay guard distinguish a new turn from duplicates", () => {
  const seen = new Set<string>();
  for (const key of ["turn:one", "tool:one", "done:resp_1"]) {
    assert.equal(firstVoiceEvent(seen, key), true);
    assert.equal(firstVoiceEvent(seen, key), false);
  }
  assert.equal(firstVoiceEvent(seen, "turn:two"), true);
  assert.equal(firstVoiceEvent(seen, ""), false);
  assert.deepEqual(parseRealtimeEvent('{"type":"response.done","response":{"id":"resp_1"}}'),
    { kind: "response", state: "done", responseId: "resp_1" });
  for (const raw of ["null", "1", '"speech"', "[]"]) assert.equal(parseRealtimeEvent(raw), null);
});
