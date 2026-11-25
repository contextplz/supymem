import assert from "node:assert/strict";
import test from "node:test";
import { externalUrl } from "./externalLink.ts";

const app = "https://app.supymem.com";
test("external links are routed out, in-app links are not", () => {
  assert.equal(externalUrl("https://www.linkedin.com/pulse/x", app), "https://www.linkedin.com/pulse/x");
  assert.equal(externalUrl("https://acme.supymem.com", app), "https://acme.supymem.com/");
  assert.equal(externalUrl("mailto:a@b.co", app), "mailto:a@b.co");
  assert.equal(externalUrl("/v/ven_1/now", app), null);
  assert.equal(externalUrl("https://app.supymem.com/v/ven_1", app), null);
  assert.equal(externalUrl("#x", app), null);
  assert.equal(externalUrl("javascript:alert(1)", app), null);
  assert.equal(externalUrl(null, app), null);
});
