"""Closed, content-free runtime failure metadata shared by capture and ingest."""
from __future__ import annotations

RUNTIME_PHASES = (
    "starting", "claiming", "launching", "observing", "deciding", "acting",
    "waiting", "verifying", "cleanup", "settling", "sidecar_start", "sidecar_health", "sidecar_run",
)
RUNTIME_REASONS = (
    "runtime_failure", "uncaught_exception", "cu_permission_missing", "cu_bridge_not_ready",
    "cu_session_unavailable", "cu_field_not_cleared", "cu_field_value_mismatch", "cu_effect_unverified",
    "cu_repeated_action", "cu_window_changed", "cu_reconcile_unavailable", "cu_stopped",
    "cu_claim_rejected", "cu_claim_expired", "cu_claim_busy", "cu_claim_changed",
    "cu_browser_decision_unavailable", "sidecar_spawn_failed", "sidecar_health_timeout", "sidecar_exited",
    "sidecar_port_conflict", "sidecar_health_unavailable",
    "sidecar_binary_missing", "sidecar_permission_denied", "sidecar_startup_exited", "sidecar_protocol_mismatch",
)
MAX_RUNTIME_MS = 86_400_000


def closed_runtime_diagnostic(value: object) -> dict | None:
    """Reject extra keys and arbitrary slugs; a naming convention is not a vocabulary."""
    if not isinstance(value, dict) or set(value) != {"phase", "reason", "phase_ms", "elapsed_ms"}:
        return None
    if value["phase"] not in RUNTIME_PHASES or value["reason"] not in RUNTIME_REASONS:
        return None
    if any(type(value[key]) is not int or not 0 <= value[key] <= MAX_RUNTIME_MS
           for key in ("phase_ms", "elapsed_ms")):
        return None
    if value["phase_ms"] > value["elapsed_ms"]:
        return None
    return {key: value[key] for key in ("phase", "reason", "phase_ms", "elapsed_ms")}


def compatible_runtime_diagnostic(value: object, source: str | None, intent: str | None, label: str) -> dict | None:
    diagnostic = closed_runtime_diagnostic(value)
    if diagnostic is None:
        return None
    phase, reason = diagnostic["phase"], diagnostic["reason"]
    if (source, intent) == ("device_worker", "run_mac_task") and label in {"Mac native_failure", "Mac uncaught_exception"}:
        return diagnostic if not phase.startswith("sidecar_") and not reason.startswith("sidecar_") else None
    pairs = {("sidecar_start", "sidecar_spawn_failed"): ("start_bridge", "Mac native_failure"),
        ("sidecar_start", "sidecar_port_conflict"): ("start_bridge", "Mac native_failure"),
        ("sidecar_start", "sidecar_binary_missing"): ("start_bridge", "Mac native_failure"),
        ("sidecar_start", "sidecar_permission_denied"): ("start_bridge", "Mac native_failure"),
        ("sidecar_start", "sidecar_startup_exited"): ("start_bridge", "Mac native_failure"),
        ("sidecar_health", "sidecar_protocol_mismatch"): ("start_bridge", "Mac native_failure"),
        ("sidecar_health", "sidecar_health_unavailable"): ("start_bridge", "Mac native_failure"),
        ("sidecar_health", "sidecar_health_timeout"): ("start_bridge", "Mac native_failure"),
        ("sidecar_run", "sidecar_exited"): ("run_bridge", "Mac process_crashed")}
    return diagnostic if source == "desktop_sidecar" and pairs.get((phase, reason)) == (intent, label) else None
