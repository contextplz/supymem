export type DesktopExecutionAvailability = "available" | "paused" | "unavailable" | "unknown";

export function isNativeDesktopManifest(manifest: { desktop_verification?: unknown; actions?: unknown }): boolean {
  return manifest.desktop_verification != null || (Array.isArray(manifest.actions)
    && manifest.actions.some(action => typeof action === "string" && action.startsWith("desktop.")));
}

/** Native input requires an explicit current server capability, not merely an unpaused device. */
export function desktopExecutionAvailability(result: { ok: boolean; data?: unknown }): DesktopExecutionAvailability {
  if (!result.ok || !result.data || typeof result.data !== "object") return "unknown";
  const data = result.data as { paused?: unknown; execution_available?: unknown; unavailability_code?: unknown };
  if (typeof data.paused !== "boolean") return "unknown";
  if (data.execution_available === false && data.unavailability_code === "device_desktop_execution_unavailable") return "unavailable";
  if (data.unavailability_code != null) return "unknown";
  if (data.paused === true && data.execution_available === false) return "paused";
  if (data.paused === false && data.execution_available === true) return "available";
  return "unknown";
}

export function desktopExecutionMessage(status: DesktopExecutionAvailability): string {
  if (status === "unavailable") return "Native Mac app control is unavailable in this beta while background input is being verified. Ask Supymem for manual app steps. Reviewed static coding, isolated browser and supported terminal tasks use separate review paths. Native app tasks cannot be newly prepared, approved or restarted.";
  if (status === "paused") return "Mac app control is paused on this device. Ask Supymem for manual app steps. Reviewed coding, browser and terminal tasks use separate review paths.";
  if (status === "available") return "Mac app control requires a separate reviewed task before execution.";
  return "Could not confirm whether native Mac app control is available. Refresh the saved status before preparing, approving or restarting native app control.";
}
