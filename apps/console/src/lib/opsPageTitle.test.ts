import assert from "node:assert/strict";
import test from "node:test";
import { opsPageTitle } from "./opsPageTitle.ts";

test("outreach uses its own page title instead of Operations", () => {
  assert.deepEqual(opsPageTitle("/ops/outreach"), {
    title: "Outreach",
    subtitle: "Review prospects and prepare founder outreach",
  });
  assert.equal(opsPageTitle("/ops").title, "Operations");
  assert.equal(opsPageTitle("/ops/tasks").title, "Operations");
});
