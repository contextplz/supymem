import assert from "node:assert/strict";
import { test } from "node:test";
import { nativeAuthActionPending, nativeAuthAvailabilityMessage } from "./nativeAuthAvailability.ts";

test("Keychain approval is distinguished from ordinary account work", () => {
  assert.match(nativeAuthAvailabilityMessage("secure_store_pending")!, /macOS.*credential/);
  assert.match(nativeAuthAvailabilityMessage("secure_store_pending")!, /continue automatically/);
  assert.doesNotMatch(nativeAuthAvailabilityMessage("native_auth_busy")!, /Keychain|macOS|retry/i);
  assert.equal(nativeAuthAvailabilityMessage("untrusted_error_code"), undefined);
});

test("Pending operations disable duplicate account actions and completion restores them", () => {
  for (const state of ["restoring", "secure_store_pending", "native_auth_busy"]) assert.equal(nativeAuthActionPending(state), true);
  for (const state of ["connected", "signed_out", "keychain_unavailable", "account_mismatch", undefined]) {
    assert.equal(nativeAuthActionPending(state), false);
  }
});
