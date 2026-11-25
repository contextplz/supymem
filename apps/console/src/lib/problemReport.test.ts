import assert from "node:assert/strict";
import { test } from "node:test";
import { buildProblemBody, pageTemplate, screenshotProblem } from "./problemReport.ts";

test("page is a route template without ids or query", () => {
  assert.equal(pageTemplate("/v/ven_ab12cd/now?token=secret"), "/v/{id}/now");
  assert.equal(pageTemplate("/account"), "/account");
});

test("screenshot is dropped without explicit consent", () => {
  const base = { message: "  Chat froze ", pathname: "/v/ven_1/now", screenshotB64: "AAAA" };
  assert.equal("screenshot_b64" in buildProblemBody(base), false);
  assert.equal("screenshot_b64" in buildProblemBody({ ...base, consent: false }), false);
  const body = buildProblemBody({ ...base, consent: true, appVersion: "0.1.55" });
  assert.equal(body.screenshot_b64, "AAAA");
  assert.equal(body.screenshot_consent, true);
  assert.equal(body.message, "Chat froze");
  assert.equal(body.app_version, "0.1.55");
});

test("screenshot type and size are checked", () => {
  assert.equal(screenshotProblem({ type: "image/png", size: 10 }), null);
  assert.ok(screenshotProblem({ type: "application/pdf", size: 10 }));
  assert.ok(screenshotProblem({ type: "image/png", size: 2_000_000 }));
});
