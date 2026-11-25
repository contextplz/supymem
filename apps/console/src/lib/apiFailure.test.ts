import assert from "node:assert/strict";
import test from "node:test";
import { apiErrorCode, apiFailure, isRetryableNativeOpenFailure, isTransientNativeIdentityFence, resourceReadFailure } from "./apiFailure.ts";

test("resource read failures give recovery steps without exposing codes or consuming the response", async () => {
  const response = new Response(JSON.stringify({error: {code: "keychain_unavailable", message: "private detail"}}),
    {status: 503, headers: {"content-type": "application/json"}});
  assert.equal(await resourceReadFailure(response, "QA report"), "QA report could not be read. Open your venture's Website screen and refresh the saved preview, then try again.");
  assert.equal(response.bodyUsed, false);
  assert.equal(await resourceReadFailure(new Response("not json", {status: 503}), "Screenshot"), "Screenshot could not be read. Open your venture's Website screen and refresh the saved preview, then try again.");
  assert.equal(await resourceReadFailure(new Response(JSON.stringify({error: {code: "private token=secret"}}),
    {status: 503, headers: {"content-type": "application/json"}}), "Screenshot"), "Screenshot could not be read. Open your venture's Website screen and refresh the saved preview, then try again.");
});

test("resource reads use the same limit notice without exposing usage across domains", async () => {
  const { serverReason } = await import("./serverReason.ts");
  for (const resource of ["Your warehouse report", "Your family activity preview"]) {
    for (const status of [402, 409]) {
      const response = new Response(JSON.stringify({error: {code: "research_insufficient_credits", required_micro: 123456}}),
        {status, headers: {"content-type": "application/json"}});
      assert.equal(await resourceReadFailure(response, resource), "You have reached your usage limit. Buy more in Settings, then retry.");
      assert.equal(response.bodyUsed, false);
      assert.equal(serverReason({ok: false, status, data: {error: {code: "research_insufficient_credits", message: "Your quote uses 25 credits."}}}, "Try again."),
        "You have reached your usage limit. Buy more in Settings, then retry.");
    }
  }
});

test("extracts native identity-fence errors without treating them as revoked sessions", () => {
  const result = {
    ok: false,
    status: 401,
    data: { error: { code: "account_changed_during_request" } },
  };

  assert.equal(apiErrorCode(result), "account_changed_during_request");
  assert.equal(isTransientNativeIdentityFence(result), true);
  assert.match(apiFailure(result, "opening your guided journey"), /connection changed/i);
  assert.doesNotMatch(apiFailure(result, "opening your guided journey"), /sign in again/i);
});

test("keeps a genuine unauthenticated response explicit", () => {
  const result = { ok: false, status: 401, data: { error: { code: "signed_out" } } };
  assert.equal(apiErrorCode(result), "signed_out");
  assert.equal(isTransientNativeIdentityFence(result), false);
  assert.equal(apiFailure(result, "opening your guided journey"), "Sign in again to continue.");
});

test("retries idempotent opens only after native transport or busy failures", () => {
  assert.equal(isRetryableNativeOpenFailure({ ok: false, status: 0, data: null }), true);
  assert.equal(isRetryableNativeOpenFailure({ ok: false, status: 503, data: { error: { code: "secure_store_pending" } } }), true);
  assert.equal(isRetryableNativeOpenFailure({ ok: false, status: 409, data: null }), false);
  assert.equal(isRetryableNativeOpenFailure({ ok: false, status: 401, data: null }), false);
});

test("research, import and family codes get plain copy instead of the generic 409 or a raw code", async () => {
  const { apiFailure } = await import("./apiFailure.ts");
  const r = (status: number, code?: string) => ({ ok: false, status, data: code ? { error: { code } } : null }) as never;
  assert.match(apiFailure(r(409, "research_insufficient_credits"), "x"), /usage limit.*Buy more/);
  assert.match(apiFailure(r(409, "research_inputs_changed"), "x"), /Review them/);
  assert.match(apiFailure(r(409, "state_version_conflict"), "x"), /Refresh/);
  assert.match(apiFailure(r(429, "import_upload_capacity"), "x"), /Too many uploads/);
  assert.doesNotMatch(apiFailure(r(400, "import_brand_new_thing"), "x"), /import_|\(4/);
  assert.match(apiFailure(r(429), "x"), /Too many requests/);
});
