import assert from "node:assert/strict";
import test from "node:test";
import { awaitingBrowser, CODE_STEP_AFTER_MS, LINK_EXPIRES_MS, signInPhase, signInErrorMessage, signInReturnPath, supymemSignInLink } from "./signInReturn.ts";

test("browser sign-in returns product destinations to the gate while desktop keeps them", () => {
  for (const path of ["/", "/v/x", "/v/x/research", "/account", "//evil.test", "/authentication"]) {
    assert.equal(signInReturnPath(path, false), "/");
    assert.equal(signInReturnPath(path, true), path);
  }
  for (const path of ["/auth", "/auth/device?code_challenge=fixture", "/auth/device/approve"]) {
    assert.equal(signInReturnPath(path, false), path);
  }
});

test("code step appears by itself when the deep link has not returned", () => {
  assert.equal(signInPhase(0), "waiting");
  assert.equal(signInPhase(CODE_STEP_AFTER_MS - 1), "waiting");
  assert.equal(signInPhase(CODE_STEP_AFTER_MS), "paste_code");
  assert.equal(signInPhase(LINK_EXPIRES_MS - 1), "paste_code");
  assert.equal(signInPhase(LINK_EXPIRES_MS), "expired");
});

test("only the authorizing state waits on the browser", () => {
  assert.equal(awaitingBrowser("authorizing"), true);
  for (const s of ["connected", "signed_out", "connecting", undefined]) assert.equal(awaitingBrowser(s), false);
});

test("only HTTPS Supymem links can be displayed or copied during sign-in", () => {
  const link = "https://gateway.supymem.com/auth/device/approve?challenge=one-time";
  assert.equal(supymemSignInLink(link), link);
  for (const value of [undefined, "not a url", "http://auth.supymem.com", "https://supymem.com.evil.test", "https://evil-supymem.com", "https://user@auth.supymem.com", "https://auth.supymem.com:8443", "https://example-auth.auth.us-east-1.amazoncognito.com"]) {
    assert.equal(supymemSignInLink(value), "");
  }
});

test("sign-in errors preserve recovery advice without exposing provider infrastructure", () => {
  const fallback = "Supymem could not finish sign-in. Try again.";
  for (const message of [null, "", "Cognito rejected sign-in", "Failed at https://auth.supymem.com", "us-east-1_EXAMPLE unavailable", "prod sign-in failed", "Pool ID: internal"]) {
    assert.equal(signInErrorMessage(message, fallback), fallback);
  }
  assert.equal(signInErrorMessage("Unlock your Keychain to reconnect.", fallback), "Unlock your Keychain to reconnect.");
});
