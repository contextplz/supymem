import assert from "node:assert/strict";
import { test } from "node:test";
import { nativeSessionNeedsSignIn } from "./nativeSessionGate.ts";

test("a failed route session check preserves the Mac workspace while native auth has credentials", () => {
  for (const state of ["connected", "restoring", "offline", "keychain_unavailable", "secure_store_pending", "native_auth_busy", undefined]) {
    assert.equal(nativeSessionNeedsSignIn(state), false, String(state));
  }
});

test("native sign-out and reauthorization states close the authenticated workspace", () => {
  for (const state of ["signed_out", "authorizing", "reauth_required", "account_mismatch", "revocation_pending"]) {
    assert.equal(nativeSessionNeedsSignIn(state), true, state);
  }
});
