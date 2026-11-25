import assert from "node:assert/strict";
import test from "node:test";
import { AcpSessionProtocol } from "./acpSessionProtocol.ts";
const line = (value: unknown) => JSON.stringify(value) + "\n";
function opened() {
  const protocol = new AcpSessionProtocol("process-A");
  protocol.request(1, "initialize");
  protocol.feed("process-A", line({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1 } }));
  protocol.request(2, "session/new");
  protocol.feed("process-A", line({ jsonrpc: "2.0", id: 2, result: { sessionId: "session-A" } }));
  return protocol;
}
const permission = (id: number, sessionId = "session-A") => ({ jsonrpc: "2.0", id, method: "session/request_permission", params: {
  sessionId, toolCall: { title: "Navigate to the approved page" }, options: [
    { optionId: "provider-specific-allow-7", kind: "allow_once", name: "Allow once" },
    { optionId: "provider-deny", kind: "reject_once", name: "Reject" },
    { optionId: "always", kind: "allow_always", name: "Always allow" },
  ],
} });

test("fragmented replies wait for complete frames and correlate registered requests", () => {
  const protocol = new AcpSessionProtocol("process-A");
  protocol.request(2, "session/new");
  const reply = line({ jsonrpc: "2.0", id: 2, result: { sessionId: "session-A" } });
  assert.deepEqual(protocol.feed("process-A", reply.slice(0, 20)), []);
  assert.equal(protocol.feed("process-A", reply.slice(20))[0].kind, "reply");
  assert.deepEqual(protocol.feed("process-A", reply), []);
});

test("another process, unsolicited reply or stderr session spoof cannot establish a session", () => {
  const protocol = new AcpSessionProtocol("process-A");
  protocol.request(2, "session/new");
  assert.deepEqual(protocol.feed("process-B", line({ jsonrpc: "2.0", id: 2, result: { sessionId: "session-A" } })), []);
  assert.deepEqual(protocol.feed("process-A", '[stderr] Created ACP session session-A\n'), []);
  assert.deepEqual(protocol.feed("process-A", line({ jsonrpc: "2.0", id: 999, result: { sessionId: "session-A" } })), []);
  assert.deepEqual(protocol.feed("process-A", line(permission(5))), []);
});

test("batched permission requests are retained separately with exact one-shot option IDs", () => {
  const protocol = opened();
  const events = protocol.feed("process-A", line(permission(5)) + line(permission(6)));
  assert.equal(events.length, 2);
  assert.equal(events[0].kind, "permission");
  assert.throws(() => protocol.answer(5, "always"));
  assert.throws(() => protocol.answer(5, "allow-once"));
  const reply = JSON.parse(protocol.answer(5, "provider-specific-allow-7"));
  assert.equal(reply.result.outcome.optionId, "provider-specific-allow-7");
  assert.equal(JSON.parse(protocol.answer(6, null)).result.outcome.outcome, "cancelled");
  assert.throws(() => protocol.answer(5, "provider-specific-allow-7"));
});

test("foreign session permission is ignored and a closed process cannot authorize anything", () => {
  const protocol = opened();
  assert.deepEqual(protocol.feed("process-A", line(permission(5, "session-B"))), []);
  protocol.feed("process-A", line(permission(6)));
  protocol.close();
  assert.throws(() => protocol.answer(6, "provider-specific-allow-7"));
  assert.deepEqual(protocol.feed("process-A", line(permission(7))), []);
});

test("hidden reasoning is omitted; tool activity remains visible without raw tool arguments", () => {
  const protocol = opened();
  const events = protocol.feed("process-A", line({ jsonrpc: "2.0", method: "session/update", params: { sessionId: "session-A", update: { sessionUpdate: "agent_thought_chunk", content: { text: "private thought" } } } }) +
    line({ jsonrpc: "2.0", method: "session/update", params: { sessionId: "session-A", update: { sessionUpdate: "tool_call", title: "Read allowed page", toolCallId: "tool-1", status: "in_progress", rawInput: { secret: "hidden" } } } }));
  assert.equal(events.length, 1);
  assert.equal(events[0].kind, "activity");
  assert.doesNotMatch(JSON.stringify(events), /private thought|hidden|rawInput/);
});

test("oversized, invalid or duplicate permission IDs fail closed", () => {
  assert.equal(new AcpSessionProtocol("p", 30).feed("p", "x".repeat(31))[0].kind, "protocol_error");
  assert.equal(opened().feed("process-A", "not JSON\n")[0].kind, "protocol_error");
  const protocol = opened();
  const bad = permission(5);
  bad.params.options.push({ ...bad.params.options[0] });
  assert.equal(protocol.feed("process-A", line(bad))[0].kind, "protocol_error");
  assert.throws(() => protocol.answer(5, "provider-specific-allow-7"));
});

test("replaying a consumed permission cannot request a second grant", () => {
  const protocol = opened();
  protocol.feed("process-A", line(permission(5)));
  protocol.answer(5, "provider-specific-allow-7");
  assert.deepEqual(protocol.feed("process-A", line(permission(5))), []);
  assert.throws(() => protocol.answer(5, "provider-specific-allow-7"));
});
