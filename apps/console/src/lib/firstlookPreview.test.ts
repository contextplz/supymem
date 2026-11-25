import assert from "node:assert/strict";
import test from "node:test";
import { firstlookPreviewPath, firstlookPreviewSandbox, ventureSitePath, scopedSiteMatches } from "./firstlookPreview.ts";
import { createGodModeRequestFence } from "./godModeRequestScope.ts";

test("first-look URLs use a workspace-authorized native preview with exact version", () => {
  assert.equal(firstlookPreviewPath("https://example.test/w/wsp_one/site/"), "/api/workspaces/wsp_one/site/preview");
  assert.equal(firstlookPreviewPath("http://127.0.0.1:3300/w/wsp_one/site/index.html?v=2"), "/api/workspaces/wsp_one/site/preview?v=2");
  assert.equal(firstlookPreviewPath("/w/wsp_one/site/?v=999"), "/api/workspaces/wsp_one/site/preview?v=999");
});

test("other product previews keep their existing URL contract", () => {
  for (const url of ["https://example.test/app/", "data:image/png;base64,a", "/w/wsp_one/site/styles.css", "/w/wsp_one/site/?v=bad"]) {
    assert.equal(firstlookPreviewPath(url), null);
  }
});

test("private static preview cannot inherit script or origin sandbox exceptions", () => {
  const permissive = "allow-scripts allow-forms allow-same-origin allow-top-navigation";
  const path = firstlookPreviewPath("/w/wsp_one/site/");
  assert.equal(firstlookPreviewSandbox(path, permissive), "");
  assert.equal(firstlookPreviewSandbox(null, permissive), permissive);
});

test("actual venture metadata and preview requests carry exact scope and preserve version", () => {
  assert.equal(ventureSitePath("wsp_shared", "ven_a"), "/api/workspaces/wsp_shared/site?venture_id=ven_a");
  assert.equal(firstlookPreviewPath("/w/wsp_shared/site/?v=7", "ven_a"),
    "/api/workspaces/wsp_shared/site/preview?v=7&venture_id=ven_a");
  assert.equal(scopedSiteMatches({ workspace_id: "wsp_shared", venture_id: "ven_a" }, "wsp_shared", "ven_a"), true);
  for (const payload of [null, {}, { workspace_id: "wsp_shared", venture_id: "ven_b" },
    { workspace_id: "wsp_shared", venture_id: null }, { workspace_id: "other", venture_id: "ven_a" }]) {
    assert.equal(scopedSiteMatches(payload, "wsp_shared", "ven_a"), false);
  }
});

test("same workspace does not let an older website read cross venture, account, epoch or abort", async () => {
  const fence = createGodModeRequestFence();
  const scope = { venture: "ven_a", owner: "account_a", epoch: 1 };
  const request = fence.begin(scope);
  const abort = new AbortController();
  let resolved = false;
  await Promise.resolve();
  for (const changed of [{ ...scope, venture: "ven_b" }, { ...scope, owner: "account_b" }, { ...scope, epoch: 2 }]) {
    if (!abort.signal.aborted && fence.current(request, changed)) resolved = true;
  }
  assert.equal(resolved, false);
  abort.abort();
  assert.equal(!abort.signal.aborted && fence.current(request, scope), false);
  fence.begin(scope);
  assert.equal(fence.current(request, scope), false);
});
