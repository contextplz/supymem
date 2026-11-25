const assert = require("node:assert/strict");
const test = require("node:test");

test("only tools with a confined executor and verified login can be Ready", async () => {
  const { CODING_AGENTS, codingAgentState, macReportedTools } = await import("./codingAgents.ts");
  assert.deepEqual(CODING_AGENTS.map(tool => tool.name), ["claude", "codex", "cursor", "opencode"]);
  for (const domain of ["warehouse scheduling", "personal recipe journal"]) {
    const tools = CODING_AGENTS.map(agent => ({ ...agent, kind: "ide", installed: true,
      connected: true, signed_in: true, coding_available: true, cli_path: `/${domain}/${agent.name}` }));
    assert.ok(macReportedTools(tools));
    assert.notEqual(codingAgentState(macReportedTools(tools.slice(0, 3)).opencode), "Ready");
    for (const tool of tools) {
      assert.equal(codingAgentState(tool), "Ready");
      assert.notEqual(codingAgentState({ ...tool, signed_in: null }), "Ready");
      assert.notEqual(codingAgentState({ ...tool, coding_available: false }), "Ready");
      assert.notEqual(codingAgentState({ ...tool, cli_path: null }), "Ready");
      assert.notEqual(codingAgentState({ ...tool, installed: false }), "Ready");
    }
  }
});

test("Cursor and OpenCode give short next steps without exposing option flags", async () => {
  const { localToolNextStep, localToolStatus, localToolCanStartBuilder } = await import("./localToolGuidance.ts");
  for (const domain of ["warehouse scheduling", "personal recipe journal"]) {
    for (const [name, label] of [["cursor", "Cursor"], ["opencode", "OpenCode"]]) {
      const tool = { name, label, kind: "ide", installed: true, connected: false,
        signed_in: false, coding_available: false, cli_path: `/${domain}/${name}` };
      const nextStep = localToolNextStep(tool);
      assert.match(nextStep, /Settings > Keys and connections > Coding tools/);
      assert.doesNotMatch(nextStep, /--\w+/);
      assert.match(localToolStatus({ ...tool, signed_in: true, coding_available: false }), /build connection not verified/);
      assert.equal(localToolCanStartBuilder({ ...tool, cli_path: null, app_path: "/Applications/Cursor.app" }), false);
      assert.equal(localToolCanStartBuilder(tool), false);
      const ready = { ...tool, signed_in: true, coding_available: true };
      assert.equal(localToolCanStartBuilder(ready), true);
      if (name === "cursor") {
        assert.match(nextStep, /Update Cursor.*cursor-agent login.*Terminal.*Check connection/);
        assert.match(localToolNextStep(ready), /allow Charlie to use Cursor/);
        assert.doesNotMatch(localToolNextStep(ready), /cannot yet build/);
      } else {
        assert.match(nextStep, /opencode auth login.*Check connection/);
        assert.match(localToolNextStep(ready), /allow Charlie to use OpenCode/);
      }
    }
  }
});
