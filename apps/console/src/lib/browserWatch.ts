/** Pure view-model for the browser watch panel (kept apart from React so node:test can run it). */

export type BrowserWatchFrame = {
  seq: number; step: number; label?: string; jpeg_b64: string;
  cursor?: { x: number; y: number } | null; viewport?: { w: number; h: number } | null;
};

export type BrowserWatchView = {
  src: string; step: number; label: string;
  /** Agent cursor as percentages of the frame, or null when the target is unknown. */
  cursor: { left: number; top: number } | null;
};

// JPEG data URLs only: this string goes into <img src>, so it must be strict base64.
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;
const MAX_B64 = 200_000;
export const WATCH_POLL_MS = 1500;
export const IDLE_POLL_MS = 5000;

const pct = (value: number, of: number) => Math.min(100, Math.max(0, (value / of) * 100));

/** Null when there is nothing safe to show. A Needs-you pause never shows a frame (the feed is blank then). */
export function browserWatchView(watch: BrowserWatchFrame | null | undefined, state?: string): BrowserWatchView | null {
  if (!watch || state === "needs_you" || typeof watch.jpeg_b64 !== "string") return null;
  if (!watch.jpeg_b64 || watch.jpeg_b64.length > MAX_B64 || !BASE64.test(watch.jpeg_b64)) return null;
  const { cursor, viewport } = watch;
  const sized = viewport && viewport.w > 0 && viewport.h > 0 ? viewport : null;
  const at = cursor && sized && Number.isFinite(cursor.x) && Number.isFinite(cursor.y)
    ? { left: pct(cursor.x, sized.w), top: pct(cursor.y, sized.h) } : null;
  return { src: `data:image/jpeg;base64,${watch.jpeg_b64}`, step: watch.step, label: (watch.label ?? "").slice(0, 40), cursor: at };
}

/** Poll quickly while frames are streaming, slowly otherwise. */
export const nativePollMs = (watching: boolean) => (watching ? WATCH_POLL_MS : IDLE_POLL_MS);

export const SIGN_IN_PROMPT = "Sign in in the Supymem browser, then Continue.";

/** Instruction copy for a Needs-you card; sign-in gets the takeover wording, nothing else changes. */
export function needsYouInstruction(pauseKind: string | undefined, handoff: string | undefined): string {
  if (pauseKind === "sign_in") {
    return `${SIGN_IN_PROMPT} Supymem does not see or type what you enter, and takes no screenshots until you continue.`;
  }
  return handoff === "visible"
    ? "A Supymem browser window is open for this owned profile. Finish the step there, then press Continue."
    : "Press Open Supymem browser when you are ready to finish this step yourself, then press Continue. For CAPTCHAs and similar challenges, follow the prompt without granting the agent control.";
}
