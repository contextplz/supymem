import test from "node:test";
import assert from "node:assert/strict";
import { connectedAccountChannels, connectionState } from "../components/connections/connectionCopy.ts";

test("platform setup does not inflate connected account channels", () => {
  const cards = [
    { id: "composio", ready: true },
    { id: "hunter", ready: true },
    { id: "gmail", ready: true, connected: true },
    { id: "linkedin", ready: false, connected: true },
    { id: "reddit", ready: false, connected: false },
  ];
  assert.deepEqual(connectedAccountChannels(cards), { connected: 1, total: 3 });
  assert.equal(connectionState(cards[0]), "Configured");
  assert.equal(connectionState(cards[2]), "Connected");
  assert.equal(connectionState(cards[3]), "Needs reconnect");
  assert.equal(connectionState(cards[4]), "Not connected");
});

test("a ready account channel without a linked account is not called connected", () => {
  const card = { id: "gmail", ready: true, connected: false };
  assert.deepEqual(connectedAccountChannels([card]), { connected: 0, total: 1 });
  assert.equal(connectionState(card), "Ready to connect");
});
