import assert from "node:assert/strict";
import test from "node:test";
import { PASSWORD_REQUIREMENTS, accountSignupError, passwordSetError } from "./passwordPolicy.ts";

test("signup policy failures are human messages, not credential-mismatch copy", () => {
  assert.match(PASSWORD_REQUIREMENTS, /12 characters/);
  assert.match(PASSWORD_REQUIREMENTS, /breach/);
  assert.match(accountSignupError("password_too_short")!, /12 characters/);
  assert.match(accountSignupError("password_breached")!, /breach/);
  assert.match(accountSignupError("email_taken")!, /already exists/);
  assert.match(accountSignupError("auth_mail_unavailable")!, /cannot safely create/i);
  assert.doesNotMatch(accountSignupError("password_too_short")!, /wrong email|didn’t match|didn't match|invalid_email_or_password/i);
  assert.equal(accountSignupError("password_too_short"), passwordSetError("password_too_short"));
  assert.equal(accountSignupError("unknown_code"), null);
  assert.equal(passwordSetError("email_taken"), null);
});
