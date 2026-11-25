import test from "node:test";
import assert from "node:assert/strict";
import { buildInstruction, pageBuildView, pickTool } from "./pageBuild.ts";

const claude = { name: "claude", label: "Claude Code", installed: true, cli_path: "/bin/claude" };
const codex = { name: "codex", label: "Codex", installed: true, cli_path: "/bin/codex" };

test("picks Claude Code, then Codex; never Cursor or an app without its CLI", () => {
  assert.equal(pickTool([codex, claude]), "claude");
  assert.equal(pickTool([codex]), "codex");
  assert.equal(pickTool([{ ...claude, cli_path: null }, { name: "cursor", label: "Cursor", installed: true, cli_path: "/c" }]), null);
});

test("no tool means connect; a tool means build", () => {
  assert.deepEqual(pageBuildView(null, []), { kind: "connect" });
  assert.deepEqual(pageBuildView("codex", []), { kind: "ready", tool: "codex" });
  assert.equal(buildInstruction("claude"), "Use Claude to build the approved landing page");
});

test("the newest real landing order drives the card from its saved profile and task", () => {
  const o = (state: string, artifact_profile = "static_landing_v1") => ({ id: "o1", artifact_profile, task_id: "landing-a", state, review_path: "/v/x/device-work-orders/o1" });
  assert.equal(pageBuildView("claude", [o("pending")]).kind, "approve");
  assert.equal(pageBuildView("claude", [o("claimed"), o("stopped")]).kind, "working");
  assert.equal(pageBuildView("claude", [o("stopped")]).kind, "stopped");
  assert.equal(pageBuildView("claude", [o("expired")]).kind, "failed");
  assert.equal(pageBuildView("claude", [o("claimed", "product_v1")]).kind, "ready");
  assert.equal(pageBuildView("claude", [o("claimed")], "landing-b").kind, "ready");
  assert.equal(pageBuildView("claude", [{ id: "other", state: "claimed", title: "Build approved task with Claude" }]).kind, "ready");
});
