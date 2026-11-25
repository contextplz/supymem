import { usageFeature } from "./usageFeature.ts";

const MAX_AGE_MS = 30_000;
let pending: { from: string; to: string; at: number } | null = null;

/** Correlate a saved chat navigation reply with a subsequent route change. */
export function beginChatNavigation(from: string, to: string, now = Date.now()): void {
  pending = from !== to && usageFeature(to) ? { from, to, at: now } : null;
}

/** A confirmed route change is an action even when macOS marks the WebView hidden. */
export function consumeChatNavigation(pathname: string, now = Date.now()): boolean {
  if (!pending) return false;
  if (now - pending.at > MAX_AGE_MS || now < pending.at) { pending = null; return false; }
  if (pathname === pending.from) return false;
  if (pathname !== pending.to) { pending = null; return false; }
  pending = null;
  return true;
}
