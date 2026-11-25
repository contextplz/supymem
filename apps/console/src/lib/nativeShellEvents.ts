import { closedNativeDiagnosticCommand } from "./uiDiagnostics.ts";
import { usageControl } from "./usageControl.ts";
import { closedRuntimeDiagnostic } from "./runtimeDiagnostic.ts";

/** One entry of the native shell's on-disk ring (src-tauri/src/shell_events.rs), as drained after sign-in.
 *  Anything that is not a closed control or command id is ignored. */
export type NativeTelemetry = {
  event: "feature.used" | "ui.control_activate" | "ui.control_outcome" | "ui.error";
  feature: string;
  props: Record<string, unknown>;
};

const ERROR_FEATURE: Record<string, string> = {
  check_for_update: "updater", install_update: "updater", defer_update: "updater", request_update_check: "updater",
  device_work_order_action: "computer_use", inspect_device_work_order: "computer_use",
  desktop_permission_request: "permissions", desktop_permission_status: "permissions",
  desktop_auth_start: "account", desktop_auth_complete_code: "account", desktop_auth_reconnect: "account",
  desktop_auth_sign_out: "account",
};
// Native-shell failures (src-tauri/src/error_capture.rs EVENTS): id -> [source, intent, feature, closed label].
const SHELL_ERRORS: Record<string, [string, string, string, string]> = {
  shell_panic: ["desktop_shell", "run_app", "shell", "Mac panic_observed"],
  shell_prev_crash: ["desktop_shell", "previous_session", "shell", "Mac process_crashed"],
  shell_console_unreachable: ["desktop_webview", "load_console", "shell", "Mac native_failure"],
  shell_start_failed: ["desktop_shell", "start_app", "shell", "Mac native_failure"],
  shell_sidecar_spawn: ["desktop_sidecar", "start_bridge", "shell", "Mac native_failure"],
  shell_sidecar_exit: ["desktop_sidecar", "run_bridge", "shell", "Mac process_crashed"],
  shell_sidecar_health: ["desktop_sidecar", "start_bridge", "shell", "Mac native_failure"],
  shell_updater: ["desktop_updater", "update_app", "updater", "Mac native_failure"],
  shell_other: ["desktop_shell", "unknown", "shell", "Mac native_failure"],
};
export const MAX_NATIVE_EVENTS = 200;

export function nativeShellTelemetry(raw: unknown): NativeTelemetry[] {
  if (!Array.isArray(raw)) return [];
  const out: NativeTelemetry[] = [];
  for (const entry of raw.slice(0, MAX_NATIVE_EVENTS)) {
    if (!entry || typeof entry !== "object") continue;
    const { kind, id, outcome } = entry as { kind?: unknown; id?: unknown; outcome?: unknown };
    if (kind === "error" && typeof id === "string" && SHELL_ERRORS[id]) {
      const [error_source, error_intent, feature, error_text] = SHELL_ERRORS[id];
      const expected: Record<string, [string, string[]]> = { shell_sidecar_spawn: ["sidecar_start", ["sidecar_spawn_failed", "sidecar_port_conflict", "sidecar_binary_missing", "sidecar_permission_denied", "sidecar_startup_exited"]],
        shell_sidecar_exit: ["sidecar_run", ["sidecar_exited"]], shell_sidecar_health: ["sidecar_health", ["sidecar_health_timeout", "sidecar_health_unavailable", "sidecar_protocol_mismatch"]] };
      const parsed = closedRuntimeDiagnostic((entry as Record<string, unknown>).runtime_diagnostic);
      const diagnostic = parsed && expected[id]?.[0] === parsed.phase && expected[id]?.[1].includes(parsed.reason) ? parsed : null;
      const reasons: Record<string, string[]> = { shell_prev_crash: ["panic", "unclean_exit"], shell_panic: ["panic"],
        shell_start_failed: ["supervisor_start_failed", "console_navigation_failed"], shell_console_unreachable: ["console_unreachable"],
        shell_updater: ["update_failed"], shell_other: ["unknown"] };
      const rawReason = (entry as Record<string, unknown>).reason;
      const reason = diagnostic?.reason ?? (typeof rawReason === "string" && (expected[id]?.[1] ?? reasons[id])?.includes(rawReason) ? rawReason : null);
      out.push({ event: "ui.error", feature, props: { error_code: "native_error", error_context: "native", error_text, error_source, error_intent,
        ...(reason ? { reason } : {}),
        ...(diagnostic ? { runtime_diagnostic: diagnostic } : {}) } });
      continue;
    }
    if (kind === "error") {
      const command = closedNativeDiagnosticCommand(id);
      if (command) out.push({ event: "ui.error", feature: ERROR_FEATURE[command] || "shell",
        props: { error_code: "native_error", error_context: "native", error_text: "Mac command_failed", error_native_command: command } });
      continue;
    }
    const control = typeof id === "string" ? usageControl(id) : null;
    if (!control) continue;
    const props = { control_id: control.controlId };
    if (kind === "activate") {
      out.push({ event: "feature.used", feature: control.feature, props: {} });
      out.push({ event: "ui.control_activate", feature: control.feature, props });
    } else if (kind === "outcome" && (outcome === "ok" || outcome === "failed")) {
      out.push({ event: "ui.control_outcome", feature: control.feature, props: { ...props, outcome } });
    }
  }
  return out;
}
