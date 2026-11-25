import assert from "node:assert/strict";
import test from "node:test";
import { isReportableVoiceError, micErrorMessage, parseRealtimeEvent, toolResultEvents, voiceSessionError } from "./voice.ts";

test("B5 review: input item identity and predecessor survive parsing", () => {
  assert.deepEqual(parseRealtimeEvent(JSON.stringify({type: "conversation.item.input_audio_transcription.delta", item_id: "item_B", delta: "Architects"})),
    {kind: "caption", text: "Architects", final: false, itemId: "item_B"});
  assert.deepEqual(parseRealtimeEvent(JSON.stringify({type: "input_audio_buffer.committed", item_id: "item_B", previous_item_id: "item_A"})),
    {kind: "committed", itemId: "item_B", previousItemId: "item_A"});
  assert.deepEqual(parseRealtimeEvent(JSON.stringify({type: "input_audio_buffer.speech_started", item_id: "item_A"})),
    {kind: "signal", signal: "speech_started", itemId: "item_A"});
});

test("parses a supymem_chat tool call", () => {
  const ev = parseRealtimeEvent(JSON.stringify({
    type: "response.function_call_arguments.done", call_id: "c1", name: "supymem_chat",
    arguments: JSON.stringify({ text: " what is next? " }),
  }));
  assert.deepEqual(ev, { kind: "tool_call", callId: "c1", name: "supymem_chat", text: "what is next?", arguments: JSON.stringify({ text: " what is next? " }) });
});

test("shared tool arguments stay unchanged for server validation", () => {
  const args = '{"url":"https://example.com/reference"}';
  const ev = parseRealtimeEvent(JSON.stringify({ type: "response.function_call_arguments.done", call_id: "read_1", name: "read_url", arguments: args }));
  assert.deepEqual(ev, { kind: "tool_call", callId: "read_1", name: "read_url", text: "", arguments: args });
});

test("parses captions and ignores unknown or invalid events", () => {
  assert.deepEqual(parseRealtimeEvent('{"type":"conversation.item.input_audio_transcription.delta","delta":"hi"}'),
    { kind: "caption", text: "hi", final: false });
  assert.deepEqual(parseRealtimeEvent('{"type":"conversation.item.input_audio_transcription.completed","transcript":"hi there"}'),
    { kind: "caption", text: "hi there", final: true, itemId: "" });
  assert.equal(parseRealtimeEvent('{"type":"session.created"}'), null);
  assert.equal(parseRealtimeEvent("nope"), null);
});

test("tool result returns output then asks for a spoken response", () => {
  const [item, resp] = toolResultEvents("c1", { body_md: "Done.", actions: [{ label: "Approve" }] });
  assert.equal(resp.type, "response.create");
  const it = item as { item: { call_id: string; output: string } };
  assert.equal(it.item.call_id, "c1");
  assert.match(it.item.output, /Done\.[\s\S]*Approve/);
  assert.match((toolResultEvents("c2", null)[0] as { item: { output: string } }).item.output, /could not answer/);
});

test("error messages", () => {
  assert.match(voiceSessionError(402), /usage limit.*Buy more/);
  assert.match(voiceSessionError(404), /could not be opened/);
  assert.match(micErrorMessage({ name: "NotAllowedError" }), /denied/);
});

test("a realtime error event keeps its code and the response id so it can be reported", () => {
  const message = "Conversation already has an active response in progress: resp_D4x9Qw. Wait until the response is finished before creating a new one.";
  const ev = parseRealtimeEvent(JSON.stringify({ type: "error", event_id: "event_1", error: { type: "invalid_request_error", code: "conversation_already_has_active_response", message } }));
  assert.deepEqual(ev, { kind: "error", message, benign: true, code: "conversation_already_has_active_response", eventId: "resp_D4x9Qw" });
  assert.equal(isReportableVoiceError("conversation_already_has_active_response", message), true); // recovered silently, still reported
  const cancel = parseRealtimeEvent(JSON.stringify({ type: "error", error: { code: "response_cancel_not_active", message: "x" } }));
  assert.equal(isReportableVoiceError("response_cancel_not_active", (cancel as { message: string }).message), false); // a cancel with nothing to cancel is noise
});
