export type GodModeRequestState = { status: "none" | "requested" | "approved" | "declined"; requested_at: string | null };

/** The status line shown once someone has asked for God mode; empty when they can still request it. */
export function godModeRequestLine(s: GodModeRequestState): string {
  const date = s.requested_at ? new Date(s.requested_at).toLocaleDateString("en", { year: "numeric", month: "short", day: "numeric" }) : "";
  if (s.status === "requested") return `Requested on ${date}, we'll enable it for you`;
  if (s.status === "approved") return `Approved. Ask in chat to "turn on God mode" whenever you want it on.`;
  return "";
}
