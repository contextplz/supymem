const assert = require("node:assert/strict");
const test = require("node:test");

for (const workspace of ["supplier-catalog", "piano-practice"]) {
  test(`your static page uses the shared bounded build controls for ${workspace}`, async () => {
    const { parseProductBuildLoop } = await import("./productBuildCaps.ts");
    const loop = { operation: "workspace_loop_v1", provider: "codex", workspace_slug: workspace,
      profile: "static_landing_v1", check_recipe: "static_landing_checks_v1", phase: "implement",
      network: "none", read_scope: "venture_workspace", write_scope: "venture_workspace",
      max_iterations: 2, max_wall_seconds: 900, per_call_seconds: 420, max_turns: null,
      plan_order_id: null, plan_digest: null };
    assert.deepEqual(parseProductBuildLoop(loop), loop);
    for (const change of [{ max_iterations: 3 }, { phase: "plan" }, { check_recipe: "web_app_vercel_checks_v1" },
      { network: "internet" }, { plan_order_id: "dwo_unrelated" }]) {
      assert.equal(parseProductBuildLoop({ ...loop, ...change }), null);
    }
  });
}
