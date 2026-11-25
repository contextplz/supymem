import assert from "node:assert/strict";
import { test } from "node:test";
import { inviteAfterAuthentication } from "./onboardingInvite.ts";

test("confirmed admission retires displayed invitation before a redirect or later account expiry", () => {
  const afterAdmission = inviteAfterAuthentication("INVITE22", { ok: true, data: { authenticated: true, subject: "usr_joined" } });
  assert.equal(afterAdmission, "");
  assert.equal(inviteAfterAuthentication(afterAdmission, { ok: false, data: null }), "");
});

test("failed admission and pending verification retain displayed recovery input", () => {
  for (const result of [
    { ok: false, data: { authenticated: true } },
    { ok: false, data: { detail: { code: "invite_code_exhausted" } } },
    { ok: true, data: { authenticated: false, mode: "email_verification_required" } },
    { ok: true, data: { authenticated: "true" } },
    { ok: true, data: null },
  ]) assert.equal(inviteAfterAuthentication("INVITE22", result), "INVITE22");
});
