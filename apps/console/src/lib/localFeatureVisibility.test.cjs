const assert = require("node:assert/strict");
const test = require("node:test");
const { surfaceCapabilities, surfaceVisible } = require("./localFeatureVisibility.ts");

const capabilities = [{ state: "not_in_this_version", surfaces: {
  paths: ["/computer", "/voice", "/social", "/ads", "/lead-library", "/admin", "/billing", "/community"],
  actions: ["mac.task.prepare", "social_post", "gtm.library_claim", "referrals.show"],
  settings: ["mac", "voice", "social", "ads", "billing"], connections: ["unipile", "linkedin", "google_ads"],
} }];

test("capability state hides every unshipped surface locally, not hosted, across unrelated ventures", () => {
  for (const venture of ["ven_factory", "ven_family_recipes"]) for (const local of [true, false]) {
    for (const [surface, values] of Object.entries(capabilities[0].surfaces)) for (const value of values) {
      assert.equal(surfaceVisible(capabilities, surface, value, local), !local);
      if (surface === "paths") assert.equal(surfaceVisible(capabilities, surface, `/v/${venture}${value}/details?tab=setup`, local), !local);
    }
    for (const state of ["ready", "needs_setup", "off"]) {
      assert.equal(surfaceVisible([{ ...capabilities[0], state }], "paths", "/ads", local), true);
    }
    for (const [surface, value] of [["actions", "build.tool.start"], ["connections", "googlesheets"], ["settings", "composio"], ["paths", "/research"]]) {
      assert.equal(surfaceVisible(capabilities, surface, value, local), true);
    }
    assert.equal(surfaceVisible(null, "paths", "/ads", local), !local);
    assert.equal(surfaceVisible(null, "actions", "status", local), true);
    assert.equal(surfaceVisible(null, "actions", "social_post", local), !local);
  }
});

test("feature metadata rejects invalid states and non-string selectors", () => {
  assert.deepEqual(surfaceCapabilities(capabilities), capabilities);
  for (const value of [null, {}, [{ state: "unknown" }], [{ state: "ready", surfaces: [] }],
    [{ state: "not_in_this_version", surfaces: { paths: [42] } }]]) assert.equal(surfaceCapabilities(value), null);
});
