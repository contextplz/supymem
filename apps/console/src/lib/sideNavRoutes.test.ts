import assert from "node:assert/strict";
import test from "node:test";
import { ventureGrowthHrefs } from "./operationsScope.ts";

test("venture growth links keep the venture selector on company surfaces", () => {
  const hrefs = ventureGrowthHrefs("ven_dt5tmy6p68t6");
  assert.equal(hrefs.gtm, "/gtm?venture=ven_dt5tmy6p68t6");
  assert.equal(hrefs.content, "/content?venture=ven_dt5tmy6p68t6");
  assert.equal(hrefs.community, "/community?venture=ven_dt5tmy6p68t6");
  assert.equal(hrefs.connections, "/connections?venture=ven_dt5tmy6p68t6");
  assert.equal(hrefs.execution, "/v/ven_dt5tmy6p68t6/plan");
  assert.equal(hrefs.plan, "/v/ven_dt5tmy6p68t6/charter");
});

test("execution nav targets the plan surface; charter remains Plan", () => {
  const hrefs = ventureGrowthHrefs("ven_1");
  assert.equal(hrefs.execution, "/v/ven_1/plan");
  assert.equal(hrefs.plan, "/v/ven_1/charter");
});
