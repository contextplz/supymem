import assert from "node:assert/strict";
import test from "node:test";
import { closedNativeAuthTransition, retainNativeTelemetryOwner } from "./nativeAuthTransitionDiagnostic.ts";

test("records a closed refresh reason and response class", () => {
  assert.deepEqual(closedNativeAuthTransition({ state: "reauth_required",
    error: "refresh_rejected_or_uncertain", http_status_class: "5xx",
    subject: "usr_private", login_url: "https://example.com/?token=secret" }),
  { reason: "refresh_rejected_or_uncertain", httpClass: "5xx" });
});

test("ignores ordinary connection and sign-out states and refuses arbitrary reason text", () => {
  assert.equal(closedNativeAuthTransition({ state: "connected", error: "refresh_outcome_unknown" }), null);
  assert.equal(closedNativeAuthTransition({ state: "signed_out" }), null);
  assert.deepEqual(closedNativeAuthTransition({ state: "reauth_required", error: "token=secret",
    http_status_class: "503 private" }), { reason: "unclassified", httpClass: "unknown" });
});

test("retains an actor-scoped receipt only across a provisional Mac actor gap", () => {
  assert.equal(retainNativeTelemetryOwner(true, "usr_one", undefined), true);
  assert.equal(retainNativeTelemetryOwner(true, "usr_one", "usr_two"), false);
  assert.equal(retainNativeTelemetryOwner(false, "usr_one", undefined), false);
  assert.equal(retainNativeTelemetryOwner(true, undefined, undefined), false);
});
