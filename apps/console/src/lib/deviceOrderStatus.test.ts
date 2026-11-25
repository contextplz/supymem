import assert from "node:assert/strict";
import test from "node:test";
import { deviceOrderStatus } from "./deviceOrderStatus.ts";

test("device outcomes keep verification guidance without exposing billing across domains", () => {
  for (const domain of ["Supplier audits", "Family gardening"]) for (const credits_reconciled of [false, true]) {
    const status = deviceOrderStatus("stopped", {expired: false, receipt: {state: "stopped", task_verified: false, credits_reconciled}});
    assert.doesNotMatch(status, /credit|usage/i, domain);
    assert.match(status, /[Rr]eview|[Cc]heck/);
  }
});

test("a sleeping Mac has a readable resumable status", () => {
  assert.equal(deviceOrderStatus("waiting_for_device", { expired: false, receipt: null }),
    "Waiting for your Mac, it resumes when your Mac is awake");
});

test("a verified native stop outranks the stale approved server state", () => {
  assert.equal(deviceOrderStatus("approved", {
    expired: false,
    receipt: { state: "stopped", task_verified: true, credits_reconciled: true },
  }), "Your task finished and was independently verified. Review its saved result.");
});

test("a stale verified flag never hides native failure or uncertain effects", () => {
  for (const rejection of [
    { failure_code: "native_cleanup_failed" }, { effect_outcome_unconfirmed: true },
  ]) {
    assert.equal(deviceOrderStatus("stopped", { expired: false,
      receipt: { state: "stopped", task_verified: true, credits_reconciled: true, ...rejection },
    }), "Your task stopped. Review its saved result; its outcome remains unverified.");
    assert.equal(deviceOrderStatus("stopped", { expired: false,
      receipt: { state: "stopped", task_verified: true, credits_reconciled: false, ...rejection },
    }), "Your task stopped. Review its saved result; its outcome remains unverified.");
  }
});

test("pending, cancelled, and uncertain stops remain explicit", () => {
  assert.equal(deviceOrderStatus("pending", { expired: false, receipt: null }), "Awaiting your permission.");
  assert.equal(deviceOrderStatus("pending", { expired: true, receipt: null }), "This review expired. A new request is required.");
  assert.equal(deviceOrderStatus("cancelled", { expired: false, claimedAt: "now", receipt: null }),
    "Your cancellation is requested. Check the saved status; the task has not yet been verified as stopped.");
  assert.equal(deviceOrderStatus("approved", {
    expired: false,
    receipt: { state: "stopped", task_verified: false, credits_reconciled: false },
  }), "Your task stopped. Review its saved result; its outcome remains unverified.");
});

test("a stopped coding process asks for source review without claiming product completion", () => {
  assert.equal(deviceOrderStatus("stopped", { expired: false,
    receipt: { state: "stopped", tool: "coding", task_verified: false, credits_reconciled: true },
  }), "Your coding task stopped. Review the saved changes and checks; your product result is not yet verified.");
});

test("a refused claim says nothing ran and never implies an uncertain action", () => {
  assert.equal(deviceOrderStatus("approved", { expired: false,
    receipt: { state: "not_started", failure_code: "native_claim_rejected" },
  }), "Not started. This task was refused before it began, so nothing ran.");
});
