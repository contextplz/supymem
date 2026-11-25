import test from "node:test";
import assert from "node:assert/strict";
import { isWebPortalPath } from "./webPortal.ts";

test("browser allows only auth, downloads, invites, admin and legal pages", () => {
  for (const path of ["/auth", "/auth/device/approve", "/auth/consume", "/download", "/admin", "/admin/users", "/invite/token", "/privacy", "/terms"])
    assert.equal(isWebPortalPath(path), true, path);
});
test("browser excludes company workflows and lookalike paths", () => {
  for (const path of ["/", "/new", "/gtm", "/content", "/v/x", "/v/x/research", "/account", "/operator", "/accounting", "/authentic", "/operators", "/operator/anything", "/download/extra", "/invite", "/administrator"])
    assert.equal(isWebPortalPath(path), false, path);
});
test("browser never exposes internal product labs", () => {
  for (const path of ["/pagebuilder-lab", "/pagebuilder-lab/jobs", "/pagebuilder-lab-legacy", "/pagebuilder", "/v/ven_a/pagebuilder"])
    assert.equal(isWebPortalPath(path), false, path);
});
