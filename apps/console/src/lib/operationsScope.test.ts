import assert from "node:assert/strict";
import test from "node:test";
import {
  companyOverviewHref,
  isCompanyOperationsPath,
  operationsHref,
  operationsReadPath,
  shellVentureId,
  ventureGrowthHrefs,
  ventureQueryId,
} from "./operationsScope.ts";
import { workspaceSwitcherCopy } from "./workspaceSwitcherCopy.ts";

test("company navigation retains selectors while unrelated and external destinations stay untouched", () => {
  const scope = { workspaceId: "ws_1", ventureId: "ven_1" };
  assert.equal(operationsHref("/content?draft=1", scope), "/content?draft=1&workspace=ws_1&venture=ven_1");
  assert.equal(operationsHref("/media?venture=old", { workspaceId: "ws_2" }), "/media?workspace=ws_2");
  assert.equal(operationsHref("/ads", scope), "/ads?workspace=ws_1&venture=ven_1");
  assert.equal(operationsHref("/", scope), "/");
  assert.equal(operationsHref("https://example.test/content", scope), "https://example.test/content");
});

test("shell keeps ?venture= when OpsWorkspaceGate clears async scope", () => {
  // Real click path: leave /v/ven_x for /gtm?venture=ven_x. Gate mounts and used to
  // call onScopeChange(null) before resolving ownership (OpsWorkspaceGate.tsx load
  // effect) — that flipped WorkspaceSwitcher to "All ventures". Query venture must win.
  assert.equal(
    shellVentureId({ pathVentureId: undefined, queryVentureId: "ven_dt5tmy6p68t6", scopeVentureId: undefined }),
    "ven_dt5tmy6p68t6",
  );
  assert.equal(
    shellVentureId({ pathVentureId: undefined, queryVentureId: "ven_x", scopeVentureId: null }),
    "ven_x",
  );
  assert.equal(
    shellVentureId({ pathVentureId: "ven_path", queryVentureId: "ven_query", scopeVentureId: "ven_scope" }),
    "ven_path",
  );
  assert.equal(ventureQueryId("?venture=ven_x&workspace=ws"), "ven_x");
  assert.equal(ventureQueryId(new URLSearchParams("venture=ven_x")), "ven_x");
  assert.equal(companyOverviewHref(), "/");
  assert.equal(isCompanyOperationsPath("/gtm"), true);
  assert.equal(isCompanyOperationsPath("/v/ven_1"), false);
});

test("SideNav Growth destinations always carry the venture query", () => {
  const hrefs = ventureGrowthHrefs("ven_dt5tmy6p68t6");
  for (const key of ["gtm", "content", "community", "connections"] as const) {
    assert.match(hrefs[key], /\?venture=ven_dt5tmy6p68t6$/);
  }
});

test("navigating to each Growth href with a cleared ops scope still shows Venture", () => {
  for (const path of [
    ventureGrowthHrefs("ven_x").gtm,
    ventureGrowthHrefs("ven_x").content,
    ventureGrowthHrefs("ven_x").community,
    ventureGrowthHrefs("ven_x").connections,
  ]) {
    const query = path.split("?")[1];
    const id = shellVentureId({
      pathVentureId: undefined,
      queryVentureId: new URLSearchParams(query).get("venture"),
      scopeVentureId: undefined,
    });
    const copy = workspaceSwitcherCopy(id, undefined);
    assert.equal(copy.eyebrow, "Venture", path);
    assert.notEqual(copy.label, "All ventures", path);
  }
});

test("only supported GET reads carry the selected company and override caller query", () => {
  for (const path of ["/tasks", "/tasks/job_1", "/dashboard/summary", "/agents/status"]) {
    assert.equal(operationsReadPath(`${path}?venture_id=foreign&limit=2`, "GET", "owned"), `${path}?venture_id=owned&limit=2`);
    assert.equal(operationsReadPath(`${path}?venture_id=foreign`, "GET"), path);
  }
  assert.equal(operationsReadPath("/tasks", "POST", "owned"), "/tasks");
  assert.equal(operationsReadPath("/config", "PUT", "owned"), "/config");
  assert.equal(operationsReadPath("/dashboard/activity", "GET", "owned"), "/dashboard/activity");
  assert.throws(() => operationsReadPath("https://foreign.test/tasks", "GET", "owned"));
  assert.throws(() => operationsReadPath("/tasks#hidden", "GET", "owned"));
});
