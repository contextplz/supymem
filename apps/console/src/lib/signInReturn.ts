/**
 * Browser sign-in for the Mac app returns through a deep link that macOS does
 * not always deliver (inconclusive on 0.1.82). Rather than leave the founder
 * staring at "authorizing", the app switches by itself to the one-time code step.
 */
export const CODE_STEP_AFTER_MS = 20_000;
/** The native sign-in challenge expires after five minutes. */
export const LINK_EXPIRES_MS = 5 * 60_000;

export const SIGNED_IN_RETURN_MESSAGE = "You are signed in. Return to the Supymem app.";

export function signInReturnPath(path: string, desktop: boolean): string {
  return desktop || path === "/auth" || path.startsWith("/auth/") ? path : "/";
}

export type SignInPhase = "waiting" | "paste_code" | "expired";

export function signInPhase(waitedMs: number): SignInPhase {
  if (waitedMs >= LINK_EXPIRES_MS) return "expired";
  return waitedMs >= CODE_STEP_AFTER_MS ? "paste_code" : "waiting";
}

export const PASTE_CODE_STEPS = [
  "Finish signing in and approve this Mac in your browser.",
  "The browser page then shows a one-time code (or tries to open Supymem for you).",
  "Copy the code, come back to Supymem and paste it below.",
] as const;

/** Native states in which the browser round trip is still outstanding. */
export function awaitingBrowser(state: string | undefined): boolean {
  return state === "authorizing";
}

export function supymemSignInLink(value: string | undefined): string {
  if (!value) return "";
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && !url.port &&
      (url.hostname === "supymem.com" || url.hostname.endsWith(".supymem.com")) ? value : "";
  } catch { return ""; }
}

export function signInErrorMessage(message: string | null | undefined, fallback: string): string {
  return message && !/https?:\/\/|cognito|amazonaws|cloudfront|\b[a-z]{2}-[a-z]+-\d(?:\b|_)|\bprod(?:uction)?\b|pool[_ -]?id/i.test(message)
    ? message : fallback;
}
