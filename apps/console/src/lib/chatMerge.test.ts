import assert from "node:assert/strict";
import test from "node:test";
import { mergeChat } from "./chatMerge.ts";

const founder = { id: "local-out-1", role: "founder" as const, body_md: "what did you do" };
const reply = { id: "msg_2", role: "assistant" as const, body_md: "I did X" };
const savedFounder = { id: "msg_1", role: "user" as const, body_md: "what did you do" };
const ids = (list: Array<{ id: string }>) => list.map((m) => m.id);

test("send then reply: poll without the reply keeps it; with the reply shows it once", () => {
  const afterSend = [founder, reply];
  assert.deepEqual(ids(mergeChat(afterSend, [])), ["local-out-1", "msg_2"]);
  assert.deepEqual(ids(mergeChat(afterSend, [savedFounder])), ["msg_1", "msg_2"]);
  assert.deepEqual(ids(mergeChat(afterSend, [savedFounder, reply])), ["msg_1", "msg_2"]);
  assert.deepEqual(ids(mergeChat(afterSend, [savedFounder, reply], true)), ["msg_1", "msg_2"]);
});

test("reply that lands later is appended after a poll", () => {
  assert.deepEqual(ids(mergeChat([founder], [savedFounder, reply])), ["msg_1", "msg_2"]);
});

test("local Mac card and its saved twin collapse to one", () => {
  const actions = [{ label: "Review Mac task", href: "/v/v1/device-work-orders/dwo_1" }];
  const local = { id: "local-mac-1", role: "assistant" as const, body_md: "I prepared (local)", actions };
  const server = { id: "msg_9", role: "assistant" as const, body_md: "I prepared (server)", actions };
  assert.deepEqual(ids(mergeChat([local], [server])), ["msg_9"]);
  assert.deepEqual(mergeChat([server], [server]), [server]);
});

test("local errors and older loaded history survive a refresh", () => {
  const err = { id: "local-err-1", role: "system" as const, body_md: "Couldn’t send that." };
  const older = { id: "msg_0", role: "assistant" as const, body_md: "old" };
  assert.deepEqual(ids(mergeChat([older, err], [reply], true)), ["msg_0", "local-err-1", "msg_2"]);
});

test("two identical founder lines claim two saved twins", () => {
  const a = { ...founder, id: "local-out-1" }, b = { ...founder, id: "local-out-2" };
  const s2 = { ...savedFounder, id: "msg_3" };
  assert.deepEqual(ids(mergeChat([a, b], [savedFounder, s2])), ["msg_1", "msg_3"]);
});

test("refreshed saved blocker replaces its obsolete setup actions without duplicating the card", () => {
  const open = { id: "msg_blocker", role: "assistant" as const, kind: "needs_you", body_md: "Connect Gmail",
    actions: [{ label: "Connect Gmail", action: "chat.reply", text: "connect my gmail" }] };
  const resolved = { ...open, kind: "needs_you_resolved", body_md: "Resolved: Connect Gmail", actions: [] };
  assert.deepEqual(mergeChat([open], [resolved], true), [resolved]);
});

test("saved twin already on screen: local copy dropped when the nonce matches", () => {
  const local = { id: "local-out-1", role: "founder" as const, body_md: "yes", client_request_id: "n1" };
  const saved = { id: "msg_1", role: "user" as const, body_md: "yes", client_request_id: "n1" };
  assert.deepEqual(ids(mergeChat([local, saved], [saved])), ["msg_1"]);
  assert.deepEqual(ids(mergeChat([local], [saved])), ["msg_1"]);
});

test("repeated saved id appears once", () => {
  assert.deepEqual(ids(mergeChat([savedFounder, savedFounder], [])), ["msg_1"]);
});

test("genuinely repeated identical answers with different ids both stay", () => {
  const second = { id: "msg_3", role: "user" as const, body_md: "what did you do" };
  assert.deepEqual(ids(mergeChat([savedFounder], [savedFounder, second])), ["msg_1", "msg_3"]);
});

test("a finished idea step clears only its obsolete local wait message", () => {
  const pending = { id: "pending", role: "assistant" as const, kind: "idea_clarification_pending" };
  const wait = { id: "local-idea-wait-1", role: "system" as const, body_md: "Taking longer" };
  const error = { ...wait, id: "local-err-1" };
  for (const kind of ["idea_clarification_ready", "idea_validation_ready", "idea_validation_failed"]) {
    const ready = { id: "ready", role: "assistant" as const, kind };
    assert.deepEqual(ids(mergeChat([pending, wait, error], [pending, ready], true)), ["pending", "local-err-1", "ready"]);
    assert.deepEqual(ids(mergeChat([pending, ready, wait], [])), ["pending", "ready"]);
    assert.ok(ids(mergeChat([ready, pending, wait], [])).includes(wait.id));
  }
});

test("a started message stays before its result when a replay anchors it to the newest message", () => {
  for (const domain of ["Factory inventory planning", "Family weekend walks"]) {
    const question = { id: "question", role: "user" as const, body_md: domain, created_at: "2026-10-20T12:00:00Z" };
    const started = { id: "started", role: "assistant" as const, kind: "gtm_search_progress", body_md: `Searching for ${domain}`, created_at: "2026-10-20T12:00:01Z" };
    const result = { id: "result", role: "assistant" as const, body_md: `Results for ${domain}`, created_at: "2026-10-20T12:05:00Z" };
    const replayed = { ...started, client_request_id: "second-click", reply_to_message_id: result.id };
    assert.deepEqual(ids(mergeChat([question, started, { ...result, reply_request_ids: ["second-click"] }], [replayed], true)), ["question", "started", "result"]);
    assert.deepEqual(ids(mergeChat([question, replayed, result], [question, started, result], true)), ["question", "started", "result"]);
  }
});

test("a reply still follows the older card it answers", () => {
  const card = { id: "card", role: "assistant" as const, body_md: "Confirm", created_at: "2026-10-20T12:00:00Z" };
  const later = { id: "later", role: "assistant" as const, body_md: "Unrelated", created_at: "2026-10-20T12:00:01Z" };
  const reply = { id: "reply", role: "assistant" as const, body_md: "Started", reply_to_message_id: card.id, created_at: "2026-10-20T12:00:02Z" };
  assert.deepEqual(ids(mergeChat([card, later], [reply])), ["card", "reply", "later"]);
});

test("an unchanged poll returns the same list and bubble objects", () => {
  const rows = [{ id: "a", role: "user" as const, body_md: "Hi", client_request_id: "r", created_at: "2026-10-20T12:00:00Z" },
    { id: "b", role: "assistant" as const, body_md: "Hello", client_request_id: "r", reply_to_message_id: "a", created_at: "2026-10-20T12:00:01Z" }];
  const shown = mergeChat([], rows, true);
  assert.equal(mergeChat(shown, JSON.parse(JSON.stringify(rows)), true), shown);
  const changed = mergeChat(shown, [rows[0], { ...rows[1], body_md: "Hello again" }], true);
  assert.notEqual(changed, shown);
  assert.equal(changed[0], shown[0]);
});
