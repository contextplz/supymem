import assert from "node:assert/strict";
import { test } from "node:test";
import { localToolCanStartBuilder, localToolNextStep, localToolStatus, type DetectedLocalTool } from "./localToolGuidance.ts";

const tool = (overrides: Partial<DetectedLocalTool>): DetectedLocalTool => ({
  name: "codex", label: "Codex", kind: "ide", installed: true, connected: false,
  app_path: null, cli_path: null, ...overrides,
});

test("an app without a coding CLI is not presented as task ready", () => {
  const codex = tool({ app_path: "/Applications/Codex.app" });
  const claude = tool({ name: "claude", label: "Claude Code", app_path: "/Applications/Claude.app" });
  assert.match(localToolStatus(codex), /coding CLI not found/);
  assert.match(localToolNextStep(codex), /Signing in to the app alone does not enable/);
  assert.match(localToolStatus(claude), /coding CLI not found/);
  assert.match(localToolNextStep(claude), /Claude Code CLI/);
});

test("nested ChatGPT Codex CLI is identified as a CLI with unverified access", () => {
  const codex = tool({ cli_path: "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex" });
  assert.match(localToolStatus(codex), /Codex CLI found inside ChatGPT/);
  assert.match(localToolStatus(codex), /sign-in unverified/);
  assert.match(localToolNextStep(codex), /codex login/);
});

test("Claude CLI guidance names the supported reviewed coding path", () => {
  const claude = tool({ name: "claude", label: "Claude Code", cli_path: "/usr/local/bin/claude" });
  assert.match(localToolNextStep(claude), /Claude Code 2.1.284 or newer/);
  assert.match(localToolNextStep(claude), /Keys and connections > Coding tools/);
});

test("builder choices require an actual launch path", () => {
  assert.equal(localToolCanStartBuilder(tool({ app_path: "/Applications/Codex.app" })), false);
  assert.equal(localToolCanStartBuilder(tool({ name: "claude", label: "Claude Code", app_path: "/Applications/Claude.app" })), false);
  assert.equal(localToolCanStartBuilder(tool({ cli_path: "/usr/local/bin/codex" })), true);
  assert.equal(localToolCanStartBuilder(tool({ name: "cursor", label: "Cursor", app_path: "/Applications/Cursor.app" })), false);
  assert.equal(localToolCanStartBuilder(undefined), false);
});


test("Cursor detection does not imply an authenticated editing adapter", () => {
  const cursor = tool({ name: "cursor", label: "Cursor", cli_path: "/usr/local/bin/cursor-agent" });
  assert.match(localToolStatus(cursor), /sign-in unverified/);
  assert.match(localToolNextStep(cursor), /cursor-agent login.*Check connection/);
  assert.match(localToolNextStep(cursor), /Keys and connections > Coding tools/);
});

test("real sign-in status remains separate from installation and task approval", () => {
  const codex = tool({ cli_path: "/bin/codex", signed_in: false });
  assert.match(localToolStatus(codex), /sign-in not confirmed/);
  assert.match(localToolNextStep(codex), /codex login/);
  const ready = { ...codex, signed_in: true, coding_available: true };
  assert.match(localToolStatus(ready), /signed in; subscription login verified/);
  assert.match(localToolNextStep(ready), /nothing is deployed/);
  const claude = tool({ name: "claude", label: "Claude Code", cli_path: "/bin/claude", signed_in: true });
  assert.match(localToolNextStep(claude), /subscription login/);
  assert.doesNotMatch(localToolStatus(claude), /ready to review/);
});
