import assert from "node:assert/strict";
import test from "node:test";
import { readScrollAnchor, restoreScrollAnchor, isNearBottom, scrollDecision } from "./chatScroll.ts";

test("following readers get the newest message", () => {
  assert.equal(scrollDecision({ newMessage: true, following: true, selecting: false }), "follow");
});

test("a reader of history is not yanked; they get a pill", () => {
  assert.equal(scrollDecision({ newMessage: true, following: false, selecting: false }), "pill");
});

test("a selection pauses following new messages", () => {
  assert.equal(scrollDecision({ newMessage: true, following: true, selecting: true }), "pill");
});

test("no new message while scrolled up does nothing", () => {
  assert.equal(scrollDecision({ newMessage: false, following: false, selecting: false }), "none");
});

test("a streaming reply keeps following a reader at the bottom, but not a selecting one", () => {
  assert.equal(scrollDecision({ newMessage: false, following: true, selecting: false, streaming: true }), "follow");
  assert.equal(scrollDecision({ newMessage: false, following: true, selecting: false }), "none");
  assert.equal(scrollDecision({ newMessage: false, following: true, selecting: true, streaming: true }), "none");
});

test("the founder's own send jumps down even when scrolled up", () => {
  assert.equal(scrollDecision({ newMessage: true, following: false, selecting: false, own: true }), "follow");
});

test("near-bottom threshold", () => {
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop: 500, clientHeight: 480 }), true);
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop: 100, clientHeight: 480 }), false);
});

test("a scrolled-up reader keeps the same bubble offset, with no write on an unchanged poll", () => {
  let top = 120, writes = 0, offset = 8;
  const row = { dataset: { messageId: "m" }, getBoundingClientRect: () => ({ top: 50 + offset - (top - 120), bottom: 100 + offset - (top - 120) }) };
  const el = { querySelectorAll: () => [row], getBoundingClientRect: () => ({ top: 50 }),
    get scrollTop() { return top; }, set scrollTop(value: number) { top = value; writes++; } } as unknown as HTMLElement;
  const anchor = readScrollAnchor(el);
  restoreScrollAnchor(el, anchor); assert.equal(writes, 0);
  offset += 34;
  restoreScrollAnchor(el, anchor); assert.equal(top, 154);
  restoreScrollAnchor(el, anchor); assert.equal(writes, 1);
});
