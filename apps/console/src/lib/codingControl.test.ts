import assert from "node:assert/strict";
import { test } from "node:test";
import { extractAcpSessionId, parseControlChunk, parseControlLine } from "./codingControl.ts";

const SID = "c62f09c6-02cd-4fd6-a474-64a75d85d316";

test("extracts session id from Hermes log line", () => {
  const line = `[stderr] 2026-09-08 16:26:01 [INFO] acp_adapter.session: Created ACP session ${SID} (cwd=.)`;
  assert.equal(extractAcpSessionId(line), SID);
  assert.equal(parseControlLine("hermes", line).sessionId, SID);
});

test("extracts session id from JSON-RPC result", () => {
  const line = JSON.stringify({ jsonrpc: "2.0", id: 2, result: { sessionId: SID } });
  assert.equal(parseControlLine("hermes", line).sessionId, SID);
});

test("extracts nested session id", () => {
  const line = JSON.stringify({ jsonrpc: "2.0", id: 2, result: { session: { sessionId: SID } } });
  assert.equal(parseControlLine("hermes", line).sessionId, SID);
});

test("chunk with logs plus JSON still finds the session", () => {
  const chunk = [
    `[stderr] tools.registry: check_fn check_web_api_key returned False`,
    `[stderr] acp_adapter.server: New session ${SID} (cwd=.)`,
    JSON.stringify({ jsonrpc: "2.0", id: 2, result: { sessionId: SID } }),
  ].join("\n");
  assert.equal(parseControlChunk("hermes", chunk).sessionId, SID);
});
