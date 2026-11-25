import { apiDiagnosticServerCode } from "./uiDiagnostics.ts";

/** Result of the API call a tagged control started: success or failure with the HTTP status and the service's
 *  machine error code. The tracker arms the control on click; the API client claims it when the call starts
 *  and settles it when the call resolves. Closed values only: no body, path or message. */
export const CONTROL_OUTCOME_EVENT = "supymem-control-outcome";
const CLAIM_MS = 5000;
// A GET is only the control's own call for reads the founder asked for; otherwise a background poll would be blamed.
const READ_CONTROL = /(refresh|redetect|status_check|select|details|entry|budget|estimate)$/;

let armed: { id: string; at: number } | null = null;

export function armControlOutcome(controlId: string, now = Date.now()): void {
  armed = { id: controlId, at: now };
}

/** Called when an API call starts; returns the control this call answers, at most once per click. */
export function claimControlOutcome(method: string, path: string, now = Date.now()): string | null {
  if (!armed || path.startsWith("/api/telemetry")) return null;
  if (now - armed.at > CLAIM_MS) { armed = null; return null; }
  if (method.toUpperCase() === "GET" && !READ_CONTROL.test(armed.id)) return null;
  const id = armed.id;
  armed = null;
  return id;
}

export type ControlOutcome = { controlId: string; ok: boolean; status: number; serverCode: string | null };

export function settleControlOutcome(controlId: string | null, status: number, data: unknown): void {
  if (!controlId || typeof window === "undefined") return;
  const detail: ControlOutcome = { controlId, ok: status >= 200 && status < 300,
    status: Number.isInteger(status) && status >= 0 && status <= 599 ? status : 0, serverCode: apiDiagnosticServerCode(data) };
  window.dispatchEvent(new CustomEvent(CONTROL_OUTCOME_EVENT, { detail }));
}
