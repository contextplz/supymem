import assert from "node:assert/strict";
import test from "node:test";
import { historyScrollPosition } from "./chatHistoryScroll.ts";

test("an older page arriving after the visible-count update preserves reading position", () => {
  const anchor = { firstId: "msg_100", scrollHeight: 1200, scrollTop: 200 };
  assert.equal(historyScrollPosition(anchor, "msg_100", 1200), null);
  assert.equal(historyScrollPosition(anchor, "msg_050", 2000), 1000);
});

test("expanding already-loaded history also keeps the current message in place", () => {
  assert.equal(historyScrollPosition({ firstId: "msg_100", scrollHeight: 900, scrollTop: 100 }, "msg_076", 1300), 500);
});
