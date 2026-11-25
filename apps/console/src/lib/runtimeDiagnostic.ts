/** Closed metadata only. Never display arbitrary queue fields or exception prose. */
export const RUNTIME_PHASES = [
  "starting", "claiming", "launching", "observing", "deciding", "acting", "waiting",
  "verifying", "cleanup", "settling", "sidecar_start", "sidecar_health", "sidecar_run",
] as const;
export const RUNTIME_REASONS = [
  "runtime_failure", "uncaught_exception", "cu_permission_missing", "cu_bridge_not_ready",
  "cu_session_unavailable", "cu_field_not_cleared", "cu_field_value_mismatch", "cu_effect_unverified",
  "cu_repeated_action", "cu_window_changed", "cu_reconcile_unavailable", "cu_stopped",
  "cu_claim_rejected", "cu_claim_expired", "cu_claim_busy", "cu_claim_changed",
  "cu_browser_decision_unavailable", "sidecar_spawn_failed", "sidecar_health_timeout", "sidecar_exited",
  "sidecar_port_conflict", "sidecar_health_unavailable",
  "sidecar_binary_missing", "sidecar_permission_denied", "sidecar_startup_exited", "sidecar_protocol_mismatch",
] as const;
export type RuntimeDiagnostic = { phase: typeof RUNTIME_PHASES[number]; reason: typeof RUNTIME_REASONS[number]; phase_ms: number; elapsed_ms: number };

export function closedRuntimeDiagnostic(raw: unknown): RuntimeDiagnostic | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const row = raw as Record<string, unknown>;
  if (Object.keys(row).sort().join() !== "elapsed_ms,phase,phase_ms,reason" ||
      !RUNTIME_PHASES.some(p => p === row.phase) || !RUNTIME_REASONS.some(r => r === row.reason) ||
      [row.phase_ms, row.elapsed_ms].some(n => typeof n !== "number" || !Number.isInteger(n) || n < 0 || n > 86_400_000)) return null;
  if ((row.phase_ms as number) > (row.elapsed_ms as number)) return null;
  return { phase: row.phase, reason: row.reason, phase_ms: row.phase_ms, elapsed_ms: row.elapsed_ms } as RuntimeDiagnostic;
}

export function runtimeDiagnosticText(raw: unknown): string | null {
  const row = closedRuntimeDiagnostic(raw);
  return row ? `${row.phase.replaceAll("_", " ")} · ${row.reason} · ${row.phase_ms} ms in phase · ${row.elapsed_ms} ms total` : null;
}
