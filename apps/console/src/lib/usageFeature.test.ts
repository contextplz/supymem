import assert from "node:assert/strict";
import test from "node:test";
import { diagnosticUsageFeature, usageFeature } from "./usageFeature.ts";

test("only canonical feature names escape route mapping", () => {
  assert.equal(usageFeature("/v/private-company-id/plan"), "planning");
  assert.equal(usageFeature("/v/private-company-id/builds/private-build"), "builds");
  assert.equal(usageFeature("/admin/users/private-user"), null);
  assert.equal(usageFeature("/v/id/experiments"), "experiments");
  assert.equal(usageFeature("/v/id/unknown-secret-path"), null);
});

test("unmapped routes keep a closed diagnostic label without gaining usage views", () => {
  assert.equal(usageFeature("/v/id/unknown-secret-path"), null);
  assert.equal(diagnosticUsageFeature("/v/id/unknown-secret-path"), "shell");
  assert.equal(diagnosticUsageFeature("/v/id/builds/private-build"), "builds");
});


test("actual founder navigation routes map to canonical usage features", () => {
  for (const [route, feature] of Object.entries({
    "/v/id/site": "website", "/v/id/org": "team", "/v/id/tasks": "execution",
    "/v/id/charter": "planning", "/v/id/people": "team", "/v/id/connections": "settings",
    "/v/id/device-work-orders/private-order-id": "computer_use",
    "/new": "onboarding", "/lab": "idea", "/gtm": "gtm", "/content": "content",
    "/community": "community", "/ops/outreach": "outreach", "/ops/finance": "payments",
  })) assert.equal(usageFeature(route), feature, route);
  assert.equal(usageFeature("/ops/private-unknown-route"), null);
  assert.equal(usageFeature("/invite/private-token"), "team");
});
