import assert from "node:assert/strict";
import test from "node:test";
import { beginChatNavigation, consumeChatNavigation } from "./chatNavigationUsage.ts";

test("counts only a confirmed, recent chat route change", () => {
  beginChatNavigation("/v/ven_one", "/v/ven_one/analytics", 1000);
  assert.equal(consumeChatNavigation("/v/ven_one", 1100), false);
  assert.equal(consumeChatNavigation("/v/ven_one/analytics", 1200), true);
  assert.equal(consumeChatNavigation("/v/ven_one/analytics", 1300), false);
});

test("an intermediate route cancels attribution to a later unrelated visit", () => {
  beginChatNavigation("/v/ven_one", "/v/ven_one/analytics", 1000);
  assert.equal(consumeChatNavigation("/v/ven_one/builds", 1100), false);
  assert.equal(consumeChatNavigation("/v/ven_one/analytics", 1200), false);
});

test("ignores same-route, unmapped, and expired navigation", () => {
  beginChatNavigation("/v/ven_one/analytics", "/v/ven_one/analytics", 1000);
  assert.equal(consumeChatNavigation("/v/ven_one/analytics", 1100), false);
  beginChatNavigation("/v/ven_one", "/not-a-venture-route", 1000);
  assert.equal(consumeChatNavigation("/not-a-venture-route", 1100), false);
  beginChatNavigation("/v/ven_one", "/v/ven_one/analytics", 1000);
  assert.equal(consumeChatNavigation("/v/ven_one/analytics", 31_001), false);
});
