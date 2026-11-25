import assert from "node:assert/strict";
import test from "node:test";
import { chatErrorCode, confirmFailureMessage, gtmConfirmFailureMessage } from "./chatConfirmError.ts";

test("reads the code from every error envelope", () => {
  assert.equal(chatErrorCode({ detail: { error: { code: "task" } } }), "task");
  assert.equal(chatErrorCode({ error: { code: "task" } }), "task");
  assert.equal(chatErrorCode({ detail: "unknown_action" }), "unknown_action");
  assert.equal(chatErrorCode(null), "");
});

test("a failed confirm names the reason, never just 'Couldn't run that action'", () => {
  const gone = confirmFailureMessage(404, { detail: { error: { code: "task" } } });
  assert.match(gone, /already started/);
  assert.match(confirmFailureMessage(409, { detail: { error: { code: "build_quote_expired" } } }), /quote expired/);
  assert.match(confirmFailureMessage(403, {}), /permission/);
  assert.match(confirmFailureMessage(500, {}), /Nothing was changed/);
  assert.doesNotMatch(confirmFailureMessage(404, { detail: { error: { code: "task" } } }), /build_quote|_/);
});

test("a failed GTM approval says credits or a changed quote, not 'outcome unconfirmed'", () => {
  assert.match(gtmConfirmFailureMessage(402, { error: { code: "gtm_insufficient_credits" } }), /usage limit.*Buy more/);
  assert.match(gtmConfirmFailureMessage(409, { detail: { error: { code: "gtm_quote_changed" } } }), /changed/);
  assert.doesNotMatch(gtmConfirmFailureMessage(422, {}), /unconfirmed/);
});

test("a credit system failure is not reported as a stale card", () => {
  for (const code of ["credits_unavailable", "duplicate_ledger_entry"]) {
    const message = confirmFailureMessage(409, { detail: { error: { code } } });
    assert.doesNotMatch(message, /Something changed|fresh card|credit/i);
  }
  assert.match(confirmFailureMessage(409, { detail: { error: { code: "chat_confirmation_changed" } } }), /fresh card/);
});
