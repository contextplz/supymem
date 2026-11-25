import assert from "node:assert/strict";
import test from "node:test";
import { founderConnectionMessage, founderSafeConnectionText } from "./connectionFounderCopy.ts";

test("connection blockers never surface server env key names to founders", () => {
  assert.equal(
    founderConnectionMessage({ code: "unipile_missing", message: "Missing: UNIPILE_API_KEY, UNIPILE_ACCOUNT_ID" }),
    "LinkedIn outreach is not set up on Supymem yet.",
  );
  assert.equal(
    founderConnectionMessage({ code: "hunter_missing", message: "HUNTER_API_KEY is not set." }),
    "Email enrichment is not set up on Supymem yet.",
  );
  assert.equal(
    founderConnectionMessage({
      provider: "google_ads",
      note: "Missing: GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CUSTOMER_ID",
    }),
    "Google Ads is not set up on Supymem yet.",
  );
  assert.equal(founderSafeConnectionText("Missing: UNIPILE_API_KEY"), null);
  assert.equal(
    founderSafeConnectionText("Connect your mailbox when you are ready to send."),
    "Connect your mailbox when you are ready to send.",
  );
});
