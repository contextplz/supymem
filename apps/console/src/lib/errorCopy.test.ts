import test from "node:test";
import assert from "node:assert/strict";
import { humanizeErrorCodes } from "./errorCopy.ts";

test("a known desktop code becomes plain copy and no raw code is left", () => {
  const out = humanizeErrorCodes("The Mac task stopped.\ndesktop_app_launch_unconfirmed");
  assert.match(out, /could not confirm it came up/);
  assert.doesNotMatch(out, /desktop_/);
});

test("an unknown code loses its underscores and ordinary text is untouched", () => {
  assert.equal(humanizeErrorCodes("native_thing_broke"), "native thing broke");
  assert.equal(humanizeErrorCodes("Nothing to fix here."), "Nothing to fix here.");
});

test("customer conversation codes are readable in chat and errors regardless of casing", () => {
  for (const code of ["No_customer_conversation", "no_customer_conversation"]) {
    assert.equal(humanizeErrorCodes(`Gap: ${code}`), "Gap: No customer conversation recorded yet");
    assert.equal(errorCodeCopy(code), "No customer conversation recorded yet");
  }
});

import { errorCodeCopy, opsErrorMessage, plainErrorText } from "./errorCopy.ts";
import { apiFailure } from "./apiFailure.ts";
test("browser configuration refusal stays specific through API and native error presentation", () => {
  const code = "device_browser_ai_configuration_unavailable";
  const copy = errorCodeCopy(code)!;
  assert.match(copy, /exact models, pricing and execution configuration/);
  assert.match(copy, /No other model is substituted/);
  assert.match(copy, /Cancel an unstarted task.*rerun/);
  assert.equal(apiFailure({ok: false, status: 409, data: {error: {code}}}, "preparing this Mac task"), copy);
  assert.equal(plainErrorText(code, "Generic failure."), copy);
  assert.doesNotMatch(copy, /workflow state changed|ready|nothing went out|device_browser_ai_/);
});
test("errorCodeCopy maps exact codes, falls back by family, and ignores non-codes", () => {
  assert.match(errorCodeCopy("research_insufficient_credits")!, /usage limit.*Buy more/);
  assert.match(errorCodeCopy("import_something_new")!, /import step/);
  assert.equal(errorCodeCopy("totally_unknown"), undefined);
  assert.equal(errorCodeCopy(null), undefined);
});

test("opsErrorMessage hides the API prefix and raw codes but keeps real server text", () => {
  assert.match(opsErrorMessage(new Error("API 409: content_scope_mismatch")), /different company/);
  assert.equal(opsErrorMessage(new Error("API 500: some_new_code"), "Could not save."), "Could not save.");
  assert.equal(opsErrorMessage(new Error("API 422: Brief is missing a title."), "x"), "Brief is missing a title.");
  assert.equal(opsErrorMessage("boom", "Fallback"), "Fallback");
});

test("plainErrorText never leaks an unknown bare code", () => {
  assert.match(plainErrorText("device_order_weird", "Generic."), /Mac task step/);
  assert.equal(plainErrorText("zzz_weird", "Generic."), "Generic.");
  assert.equal(plainErrorText("Permission denied by macOS", "Generic."), "Permission denied by macOS");
});

test("exception names and job identifiers cannot leak through an error fallback", () => {
  assert.match(plainErrorText("handler_failed:ValueError", "Generic."), /saved work is safe/);
  for (const raw of ["Failed job job_abc123", "RuntimeError: unexpected outcome", "provider_failed:TypeError"])
    assert.equal(plainErrorText(raw, "Generic."), "Generic.");
});
