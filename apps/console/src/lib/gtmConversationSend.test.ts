import assert from "node:assert/strict";
import test from "node:test";
import { gtmSendMode } from "./gtmConversationSend.ts";

test("GTM diagnosis status stays on the quote route while other requests use general chat", () => {
  for (const kind of ["gtm_diagnosis_quote", "gtm_diagnosis_pending"]) {
    assert.equal(gtmSendMode("What is the diagnosis status?", [{ kind }]), "followup");
    assert.equal(gtmSendMode("What is the status?", [{ kind }]), "followup");
    assert.equal(gtmSendMode("Send a campaign now", [{ kind }]), "general");
    assert.equal(gtmSendMode("Start another diagnosis", [{ kind }]), "followup");
  }
});

test("first product description quotes once; later questions and actions use general chat", () => {
  assert.equal(gtmSendMode("How do I get customers?", []), "general");
  assert.equal(gtmSendMode("Bakery owners need help finding new customers", []), "quote");
  assert.equal(gtmSendMode("Run another GTM diagnosis", [{ kind: "gtm_diagnosis_completed" }]), "quote");
  assert.equal(gtmSendMode("Draft an outreach email", [{ kind: "gtm_diagnosis_completed" }]), "general");
  assert.equal(gtmSendMode("How many credits remain?", [{ kind: "gtm_diagnosis_completed" }]), "general");
});
