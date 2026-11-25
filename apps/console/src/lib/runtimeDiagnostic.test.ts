import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { closedRuntimeDiagnostic, runtimeDiagnosticText, RUNTIME_PHASES, RUNTIME_REASONS } from "./runtimeDiagnostic.ts";
import { nativeShellTelemetry } from "./nativeShellEvents.ts";

const diagnostic = {phase: "sidecar_health", reason: "sidecar_health_timeout", phase_ms: 30000, elapsed_ms: 30000};

test("native failure keeps bounded metadata while malformed and legacy rows retain their old shape", () => {
  const row = {kind: "error", id: "shell_sidecar_health", runtime_diagnostic: diagnostic};
  assert.deepEqual(nativeShellTelemetry([row])[0].props.runtime_diagnostic, diagnostic);
  assert.match(runtimeDiagnosticText(diagnostic)!, /sidecar health.*30000 ms in phase/);
  for (const bad of [{...diagnostic, prompt: "private prompt"}, {...diagnostic, reason: "private_slug"},
    {...diagnostic, phase_ms: true}, {...diagnostic, elapsed_ms: Infinity}, {...diagnostic, phase_ms: 86400001},
    {...diagnostic, phase_ms: -1}, {...diagnostic, phase_ms: 30001}, {...diagnostic, phase: ["sidecar_health"]}]) {
    assert.equal(closedRuntimeDiagnostic(bad), null);
    assert.equal(runtimeDiagnosticText(bad), null);
    assert.ok(!("runtime_diagnostic" in nativeShellTelemetry([{...row, runtime_diagnostic: bad}])[0].props));
  }
  assert.ok(!("runtime_diagnostic" in nativeShellTelemetry([{kind: "error", id: "shell_sidecar_spawn"}])[0].props));
  assert.ok(!("runtime_diagnostic" in nativeShellTelemetry([{...row, id: "shell_panic"}])[0].props));
  assert.ok(!("runtime_diagnostic" in nativeShellTelemetry([{...row, id: "shell_sidecar_spawn"}])[0].props));
});

test("runtime vocabularies match the server/runtime contract", () => {
  const source = readFileSync(new URL("../../../../packages/core/runtime_diagnostics.py", import.meta.url), "utf8");
  for (const [name, values] of [["RUNTIME_PHASES", RUNTIME_PHASES], ["RUNTIME_REASONS", RUNTIME_REASONS]] as const) {
    const block = source.match(new RegExp(`${name} = \\(([\\s\\S]*?)\\)`))![1];
    assert.deepEqual([...block.matchAll(/"([a-z_]+)"/g)].map(m => m[1]), [...values]);
  }
});

test("new sidecar classifications survive telemetry without weakening phase validation", () => {
  for (const [id, phase, reason] of [
    ["shell_sidecar_spawn", "sidecar_start", "sidecar_port_conflict"],
    ["shell_sidecar_health", "sidecar_health", "sidecar_health_unavailable"],
    ["shell_sidecar_spawn", "sidecar_start", "sidecar_binary_missing"],
    ["shell_sidecar_spawn", "sidecar_start", "sidecar_permission_denied"],
    ["shell_sidecar_spawn", "sidecar_start", "sidecar_startup_exited"],
    ["shell_sidecar_health", "sidecar_health", "sidecar_protocol_mismatch"],
  ]) {
    const runtime_diagnostic = {phase, reason, phase_ms: 0, elapsed_ms: 46000};
    assert.deepEqual(nativeShellTelemetry([{kind: "error", id, runtime_diagnostic}])[0].props.runtime_diagnostic, runtime_diagnostic);
    assert.ok(!("runtime_diagnostic" in nativeShellTelemetry([{kind: "error", id, runtime_diagnostic: {...runtime_diagnostic, phase: "sidecar_run"}}])[0].props));
  }
});
