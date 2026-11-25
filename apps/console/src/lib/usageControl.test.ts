import assert from "node:assert/strict";
import test from "node:test";
import { announceUsageControl, contentGenerationConfirmControl, isMacReviewHref, macStartOrderId, USAGE_CONTROL_EVENT, usageControl, waitlistUsageControl } from "./usageControl.ts";

test("only closed control IDs become telemetry dimensions", () => {
  assert.deepEqual(usageControl("chat_send"), { controlId: "chat_send", feature: "chat" });
  assert.deepEqual(usageControl("website_publish_confirm"), { controlId: "website_publish_confirm", feature: "website" });
  assert.deepEqual(usageControl("computer_use_send"), { controlId: "computer_use_send", feature: "computer_use" });
  assert.deepEqual(usageControl("run_status_attention"), { controlId: "run_status_attention", feature: "notifications" });
  for (const id of ["idea_status_check", "idea_validation_retry", "idea_clarification_retry", "idea_credits_continue",
    "idea_validate_revision", "idea_confirm_personal", "idea_continue_research"]) {
    assert.deepEqual(usageControl(id), { controlId: id, feature: "idea" });
  }
  assert.deepEqual(usageControl("idea_review_local_build"), { controlId: "idea_review_local_build", feature: "builds" });
  for (const id of ["content_topics_entry", "content_topics_budget", "content_topics_confirm",
    "content_draft_entry", "content_draft_budget", "content_draft_confirm",
    "content_agent_cycle", "content_draft_edit", "content_draft_save"]) {
    assert.deepEqual(usageControl(id), { controlId: id, feature: "content" });
  }
  assert.equal(usageControl("please send my private prompt"), null);
  assert.equal(usageControl("unknown"), null);
  assert.equal(usageControl(null), null);
});

test("content confirmation distinguishes topic runs from draft runs", () => {
  assert.equal(contentGenerationConfirmControl("topics"), "content_topics_confirm");
  assert.equal(contentGenerationConfirmControl("draft"), "content_draft_confirm");
});

test("waitlist actions keep separate closed IDs on Website and Go to market", () => {
  for (const surface of ["website", "gtm"] as const) {
    for (const action of ["enable", "close", "test", "refresh", "copy"] as const) {
      const id = waitlistUsageControl(surface, action);
      assert.equal(id, `waitlist_${action}_${surface}`);
      assert.deepEqual(usageControl(id), { controlId: id, feature: surface });
    }
  }
});

test("Mac review link recognizes only a venture work order path", () => {
  assert.equal(isMacReviewHref("/v/ven_123/device-work-orders/ord_123"), true);
  assert.equal(isMacReviewHref("/v/ven_123/device-work-orders/ord_123?review=1"), true);
  assert.equal(isMacReviewHref("https://example.test/private"), false);
  assert.equal(isMacReviewHref("/v/ven_123/builds"), false);
});

test("explicit control announcement contains only a closed ID", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "window");
  const target = new EventTarget();
  const details: unknown[] = [];
  target.addEventListener(USAGE_CONTROL_EVENT, event => details.push((event as CustomEvent).detail));
  Object.defineProperty(globalThis, "window", { configurable: true, value: target });
  try {
    announceUsageControl("chat_send");
    announceUsageControl("private founder prompt");
    assert.deepEqual(details, [{ controlId: "chat_send" }]);
  } finally {
    if (original) Object.defineProperty(globalThis, "window", original);
    else Reflect.deleteProperty(globalThis, "window");
  }
});

test("Mac Start telemetry accepts only opaque work-order identifiers", () => {
  assert.equal(macStartOrderId("dwo_opaque-123"), "dwo_opaque-123");
  assert.equal(macStartOrderId("https://example.test/private"), null);
  assert.equal(macStartOrderId("dwo_private text"), null);
  assert.equal(macStartOrderId(null), null);
});
