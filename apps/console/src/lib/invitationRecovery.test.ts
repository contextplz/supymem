import assert from "node:assert/strict";
import test from "node:test";
import { invitationRecoveryMessage } from "./invitationRecovery.ts";

test("every known verified admission failure gives recoverable invitation guidance", () => {
  for (const code of ["invite_code_required", "invalid_invite_code", "invite_code_exhausted", "self_invitation", "already_onboarded"]) {
    for (const shape of [{ error: { code } }, { detail: { code } }]) {
      assert.match(invitationRecoveryMessage(shape) || "", /code|invitation|sign in/i);
    }
  }
});
test("a full beta is reported honestly with the waitlist", () => {
  for (const shape of [{ error: { code: "beta_full" } }, { detail: { code: "beta_full" } }]) {
    assert.match(invitationRecoveryMessage(shape) || "", /beta is full.*waitlist/);
  }
});
test("identity failures and arbitrary invite-like codes never claim verified identity", () => {
  for (const value of [null, {}, { error: { code: "invalid_grant" } }, { error: { code: "email_not_verified" } }, { detail: { code: "private_invite_provider_body" } }, { error: { code: 42 } }]) {
    assert.equal(invitationRecoveryMessage(value), null);
  }
});
