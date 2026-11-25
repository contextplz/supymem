import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("analytics uses plain language without claiming verified customers or payments", () => {
  const source = readFileSync(new URL("../app/v/[ventureId]/analytics/page.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(source, /instrumented browser events|Synthetic public-submission|waitlist ledger|ordered user funnel|Email ownership/);
  for (const copy of ["Test sign-ups are not counted", "We haven’t checked their email addresses", "doesn’t prove money was received",
    "Repeat visits and test activity may be included", "An empty dashboard doesn’t necessarily mean nobody has visited"]) assert.ok(source.includes(copy), copy);
});
