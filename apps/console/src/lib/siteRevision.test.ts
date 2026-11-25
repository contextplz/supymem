import assert from "node:assert/strict";
import test from "node:test";
import { revisionBuildHref, revisionLoadFailure, revisionRequestBody, revisionSaveFailure } from "./siteRevision.ts";

test("revision review link binds both app and run in a multi-app venture", () => {
  const href = revisionBuildHref("ven_founder", "app_landing", "bld_revision");
  assert.equal(href, "/v/ven_founder/site?app=app_landing&run=bld_revision#build-review");
  const parsed = new URL(href, "https://console.supymem.com");
  assert.equal(parsed.searchParams.get("app"), "app_landing");
  assert.equal(parsed.searchParams.get("run"), "bld_revision");
});

test("revision review link escapes all route components", () => {
  assert.equal(revisionBuildHref("ven/one", "app&other", "bld?run"),
    "/v/ven%2Fone/site?app=app%26other&run=bld%3Frun#build-review");
});

test("revision load failure never shows raw codes; unpublished page shows nothing", () => {
  assert.equal(revisionLoadFailure("hosting_live_static_site_required", 409), "");
  assert.doesNotMatch(revisionLoadFailure("hosting_live_source_unverified", 409), /hosting_|409/);
  assert.doesNotMatch(revisionLoadFailure(null, 503), /503/);
});

test("revision POST uses only its strict write contract, never the full GET baseline", () => {
  const baseline = { deployment_id: "dep_live", source_sha256: "a".repeat(64),
    build_id: "bld_accepted", source_artifact_id: "bar_accepted" };
  assert.deepEqual(revisionRequestBody(baseline, "  Remove the old footer.  "), {
    deployment_id: "dep_live", source_sha256: "a".repeat(64), instruction: "Remove the old footer.",
  });
});

test("strict validation failure is visible to the founder", () => {
  assert.doesNotMatch(revisionSaveFailure(422, null), /422/);
  assert.match(revisionSaveFailure(409, "hosting_revision_baseline_changed"), /page changed while you were editing/);
  assert.doesNotMatch(revisionSaveFailure(500, "weird_code"), /weird_code|500/);
});
