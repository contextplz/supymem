import assert from "node:assert/strict";
import test from "node:test";
import { enqueueTelemetry, flushQueuedTelemetry, retryQueuedOnReconnect,
  terminalTelemetryStatus, telemetryRetryDelayMs, type QueuedTelemetry } from "./usageDelivery.ts";

test("only unrecoverable telemetry responses are terminal", () => {
  for (const status of [400, 403, 404, 409, 413, 422]) assert.equal(terminalTelemetryStatus(status), true);
  for (const status of [0, 200, 302, 401, 408, 425, 429, 500, 502, 503]) {
    assert.equal(terminalTelemetryStatus(status), false);
  }
});

test("retry delay grows but stays bounded across a long outage", () => {
  assert.equal(telemetryRetryDelayMs(1), 15_000);
  assert.equal(telemetryRetryDelayMs(2), 30_000);
  assert.equal(telemetryRetryDelayMs(3), 60_000);
  assert.equal(telemetryRetryDelayMs(100), 300_000);
});

test("four transient failures retain one nonce and reconnect delivers it once", async () => {
  let now = 1_000_000;
  let persisted = "";
  const queue: QueuedTelemetry[] = [{ body: { context_subject: "founder-1", event_id: "same-nonce" }, attempts: 0 }];
  const seen: unknown[] = [];
  let calls = 0;
  const options = {
    queue: () => queue, owner: () => "founder-1", now: () => now, online: () => true,
    persist: () => { persisted = JSON.stringify(queue); },
    send: async (body: Record<string, unknown>) => {
      seen.push(body.event_id);
      calls++;
      if (calls === 2 || calls === 4) throw new Error("temporary network failure");
      return calls < 5 ? { ok: false, status: 503 } : { ok: true, status: 200 };
    },
  };
  for (let failure = 0; failure < 4; failure++) {
    await flushQueuedTelemetry(options);
    assert.equal(queue.length, 1);
    assert.equal(JSON.parse(persisted)[0].body.event_id, "same-nonce");
    now = queue[0].nextAttemptAt || now;
  }
  retryQueuedOnReconnect(queue, options.persist);
  await flushQueuedTelemetry(options);
  assert.deepEqual(seen, Array(5).fill("same-nonce"));
  assert.equal(queue.length, 0);
  assert.equal(persisted, "[]");
});

test("bounded telemetry queue evicts the oldest item and rejects a changed account", async () => {
  const queue: QueuedTelemetry[] = [];
  for (let index = 0; index < 257; index++) {
    enqueueTelemetry(queue, { body: { context_subject: "founder-1", event_id: index }, attempts: 0 }, 256);
  }
  assert.equal(queue.length, 256);
  assert.equal(queue[0].body.event_id, 1);
  let sent = 0;
  await flushQueuedTelemetry({ queue: () => queue, owner: () => "founder-2", persist: () => {},
    now: () => 0, online: () => true, send: async () => { sent++; return { ok: true, status: 200 }; } });
  assert.equal(sent, 0);
  assert.equal(queue.length, 0);
});

test("offline queue retains diagnostics before passive usage at capacity", () => {
  const queue: QueuedTelemetry[] = [{ body: { event: "ui.error", event_id: "diagnostic" }, attempts: 0 }];
  enqueueTelemetry(queue, { body: { event: "ui.screen_view", event_id: "view-1" }, attempts: 0 }, 2);
  enqueueTelemetry(queue, { body: { event: "feature.used", event_id: "click-1" }, attempts: 0 }, 2);
  assert.deepEqual(queue.map(item => item.body.event_id), ["diagnostic", "click-1"]);
  enqueueTelemetry(queue, { body: { event: "ui.error", event_id: "diagnostic-2" }, attempts: 0 }, 2);
  assert.deepEqual(queue.map(item => item.body.event_id), ["diagnostic", "diagnostic-2"]);
  enqueueTelemetry(queue, { body: { event: "ui.screen_view", event_id: "view-2" }, attempts: 0 }, 2);
  assert.deepEqual(queue.map(item => item.body.event_id), ["diagnostic", "diagnostic-2"]);
});

test("fresh diagnostic bypasses an older view waiting on retry", async () => {
  const queue: QueuedTelemetry[] = [
    { body: { context_subject: "founder-1", event: "ui.screen_view", event_id: "old" }, attempts: 2, nextAttemptAt: 600_000 },
    { body: { context_subject: "founder-1", event: "ui.error", event_id: "urgent" }, attempts: 0 },
  ];
  const sent: unknown[] = [];
  await flushQueuedTelemetry({ queue: () => queue, owner: () => "founder-1", persist: () => {},
    now: () => 100_000, online: () => true, send: async body => { sent.push(body.event_id); return { ok: true, status: 200 }; } });
  assert.deepEqual(sent, ["urgent"]);
  assert.deepEqual(queue.map(item => item.body.event_id), ["old"]);
});
