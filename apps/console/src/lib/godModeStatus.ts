/** The server state, rather than the enabled setting, determines activity copy. */
export function godModeStatusText(status: { state: string; working_on?: string }): string {
  const detail = status.working_on?.trim();
  switch (status.state) {
    case "working": return `Supymem is working on: ${detail || "looking for the next step"}`;
    case "paused": return detail || "This venture is paused. Nothing new starts until you resume.";
    case "waiting_on_you": return detail || "Supymem needs your help to continue.";
    case "queued": return detail || "Work is queued; execution has not started.";
    case "idle": return detail || "No work is in progress.";
    case "failed": return detail || "This step failed. Your saved work is retained.";
    case "needs_you": return detail || "A required action needs your attention.";
    case "cancelled": return detail || "This work was cancelled.";
    case "completed": return detail || "The recorded work has finished.";
    case "off": return "God mode is off.";
    default: return "Supymem's current activity could not be confirmed.";
  }
}
