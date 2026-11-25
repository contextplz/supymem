/** Only closed native auth causes enter operator diagnostics. Never send status.error verbatim. */
const REASONS = [
  "invalid_gateway_origin", "invalid_saved_credentials", "authorization_expired",
  "authorization_rejected", "refresh_outcome_unknown", "refresh_rejected_or_uncertain",
  "invalid_refresh_response", "device_revoked_or_expired",
] as const;
export type NativeAuthReason = typeof REASONS[number] | "unclassified";
export type AuthHttpClass = "4xx" | "5xx" | "other" | "unknown";

/** An absent actor is provisional until native auth reports explicit sign-out. */
export function retainNativeTelemetryOwner(native: boolean, owner?: string, next?: string): boolean {
  return native && Boolean(owner) && !next;
}

export function closedNativeAuthTransition(value: unknown): { reason: NativeAuthReason; httpClass: AuthHttpClass } | null {
  if (!value || typeof value !== "object") return null;
  const status = value as { state?: unknown; error?: unknown; http_status_class?: unknown };
  if (status.state !== "reauth_required") return null;
  const reason = typeof status.error === "string" && REASONS.some((candidate) => candidate === status.error)
    ? status.error as NativeAuthReason : "unclassified";
  const httpClass = ["4xx", "5xx", "other"].includes(String(status.http_status_class))
    ? status.http_status_class as AuthHttpClass : "unknown";
  return { reason, httpClass };
}
