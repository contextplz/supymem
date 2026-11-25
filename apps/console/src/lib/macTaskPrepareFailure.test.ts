import assert from "node:assert/strict";
import test from "node:test";
import { activeMacTaskReviewPath, macTaskPrepareFailure } from "./macTaskPrepareFailure.ts";
import type { ApiResult } from "./client.ts";

function result(detail: Record<string, unknown>): ApiResult {
  return { ok: false, status: 409, data: { detail } };
}

test("prefers the server refusal message when present", () => {
  assert.equal(
    macTaskPrepareFailure(result({
      code: "device_chat_intent_unsupported",
      message: "That Mac task is not supported yet. Supported tasks: Open Builds.",
    })),
    "That Mac task is not supported yet. Supported tasks: Open Builds.",
  );
});

test("falls back to known Mac-task codes", () => {
  assert.match(
    macTaskPrepareFailure(result({ code: "device_coding_task_unavailable" })),
    /not a coding task/,
  );
  assert.match(
    macTaskPrepareFailure(result({ code: "device_chat_intent_unsupported" })),
    /Supported tasks/,
  );
  assert.equal(
    macTaskPrepareFailure(result({ code: "device_task_has_active_order" })),
    "Finish or cancel the current Mac task first.",
  );
  assert.match(
    macTaskPrepareFailure(result({ code: "device_chat_email_required" })),
    /exact address to use/,
  );
  assert.match(
    macTaskPrepareFailure(result({ code: "device_chat_secret_use_form" })),
    /secure connection form/,
  );
  assert.match(
    macTaskPrepareFailure(result({ code: "device_personal_build_unavailable" })),
    /Confirm the current personal project idea/,
  );
  assert.match(
    macTaskPrepareFailure(result({ code: "device_personal_build_app_ambiguous" })),
    /Open Builds to choose/,
  );
});

test("code-only browser preparation refusals explain the specific block", () => {
  for (const [code, expected] of [
    ["device_terminal_task_unavailable", /open approved plan task/],
    ["device_terminal_task_ambiguous", /More than one open plan task/],
    ["device_browser_ai_configuration_unavailable", /Refresh Browser tasks in Settings/],
    ["device_browser_ai_request_invalid", /exact HTTPS websites/],
    ["device_browser_ai_request_bounds", /supported limits/],
    ["device_read_only_browser_task_unavailable", /cannot enforce this read-only goal/],
    ["device_desktop_execution_unavailable", /does not enable native app control/],
    ["device_budget_unavailable", /budget could not be confirmed/],
    ["device_task_not_ready", /task or its review changed/i],
    ["device_order_task_changed", /task or its review changed/i],
    ["device_order_review_changed", /task or its review changed/i],
  ] as const) {
    const copy = macTaskPrepareFailure(result({code}));
    assert.match(copy, expected);
    assert.doesNotMatch(copy, /workflow state changed/);
  }
});

test("unknown task or charge outcomes do not invite a repeat or promise a refund", () => {
  for (const code of ["device_budget_requires_reconciliation", "device_effect_requires_reconciliation",
    "device_browser_ai_provider_or_usage_unconfirmed"]) {
    const copy = macTaskPrepareFailure(result({code}));
    assert.match(copy, /reservation may remain held/);
    assert.match(copy, /do not repeat/);
    assert.doesNotMatch(copy, /refunded|released|try again/);
  }
  assert.match(macTaskPrepareFailure(result({code: "unrecognized_conflict"})), /workflow state changed/);
});

test("exposes the blocking Mac task review path for chat links", () => {
  const path = "/v/ven_1/device-work-orders/dwo_exact-2";
  assert.equal(
    activeMacTaskReviewPath(result({
      code: "device_task_has_active_order",
      message: "Finish or cancel the current Mac task first.",
      review_path: path,
      order_id: "dwo_exact-2",
    })),
    path,
  );
  assert.equal(
    activeMacTaskReviewPath(result({
      code: "device_task_has_active_order",
      review_path: "/v/ven_1/device-work-orders/dwo_good?next=https://evil.test",
    })),
    undefined,
  );
  assert.equal(
    activeMacTaskReviewPath(result({ code: "device_chat_intent_unsupported" })),
    undefined,
  );
});


test("unavailable general goals are not advertised as supported signup work", () => {
  const refusal = macTaskPrepareFailure(result({ code: "device_general_browser_execution_unavailable" }));
  assert.match(refusal, /unavailable in this beta/);
  assert.match(refusal, /No new task was prepared or started/);
  assert.doesNotMatch(macTaskPrepareFailure(result({ code: "device_chat_intent_unsupported" })), /signup-style/);
});
