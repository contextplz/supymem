import assert from "node:assert/strict";
import test from "node:test";
import { oneLine, headline, sourceDomain, evidenceSegments, evidenceCoverage, productDescription, plainText } from "./researchBrief.ts";

test("research brief helpers", () => {
  assert.equal(oneLine("Short one. Second sentence."), "Short one.");
  const long = "word ".repeat(40).trim() + ".";
  assert.equal(oneLine(long, 30), long);
  assert.equal(oneLine("A narrow\u2014inter-agent gap. More."), "A narrow, inter-agent gap.");
  assert.equal(headline("Test the idea before you build anything real"), "Test the idea before you build anything real");
  assert.equal(headline("Worth building"), "Worth building");
  assert.equal(sourceDomain("https://www.example.com/a?b"), "example.com");
  assert.equal(sourceDomain("javascript:alert(1)"), "");
  assert.equal(sourceDomain(null), "");
  assert.equal(evidenceSegments("unknown", 5), 0);
  assert.equal(evidenceSegments("mixed_leads", 9), 3);
  assert.equal(evidenceSegments("supporting_leads", 0), 1);
  assert.deepEqual(evidenceCoverage({ a: { status: "unknown" }, b: { status: "supporting_leads" } }), { known: 1, total: 2 });
});

test("scraped product descriptions drop navigation and images and stay bounded", () => {
  const raw = "[Skip to main content](https://beanbox.com/#main-content) [**Free bag when you join today! ►**](https://beanbox.com/deals) 4.5 ★from 2,000+ reviews\n![Beanbox Curator](https://assets.example/image.jpg)\n[Coffee subscriptions](https://beanbox.com/coffee) from independent roasters. Fresh beans delivered monthly. Third sentence.";
  assert.equal(productDescription(raw), "Coffee subscriptions from independent roasters. Fresh beans delivered monthly.");
  assert.ok(productDescription("longword ".repeat(100)).length <= 280);
  assert.equal(productDescription("[Home](https://example.com)\n![logo](https://example.com/a.png)"), "");
  assert.equal(headline("No_customer_conversation"), "No customer conversation recorded yet");
});

test("plain quote sanitizer preserves every sentence without markdown or link targets", () => {
  const raw = "# Pricing\n- [Go pro](https://meetings.hubspot.com/alpha): **$29/month**.\n2. Keep _all_ details. Third sentence stays.\n> ![Logo](https://assets.example/logo.png) Save &amp; compare &#36;29.";
  assert.equal(plainText(raw), "Pricing Go pro: $29/month. Keep all details. Third sentence stays. Save & compare $29.");
  assert.equal(plainText("[Plan][price]\n[price]: https://example.com/pricing"), "Plan");
});
