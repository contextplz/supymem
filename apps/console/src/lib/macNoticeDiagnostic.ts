/** Founder-facing Mac notification diagnostic copy for Settings. */

export type MacNoticeDiagnostic = {
  status?: string;
  reason?: string | null;
};

export function macNoticeDiagnosticLine(
  diag: MacNoticeDiagnostic | null | undefined,
): string {
  if (!diag || typeof diag.status !== "string" || !diag.status) {
    return "Mac notifications: checking…";
  }
  if (diag.status === "working") return "Mac notifications: working";
  if (diag.status === "blocked_in_system_settings") {
    return "Mac notifications: blocked in System Settings";
  }
  if (diag.status === "failed") {
    const reason =
      typeof diag.reason === "string" && diag.reason.trim()
        ? diag.reason.trim()
        : "unknown";
    return `Mac notifications: failed (${reason})`;
  }
  return "Mac notifications: checking…";
}
