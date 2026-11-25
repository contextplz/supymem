/** Suggestions: cards Supymem offers to take over. Shared by the overlay, the list page and Settings. */
import { desktopInvoke, desktopInvokeOrThrow } from "./desktop.ts";

/** Fired after any decision so the sidebar badge refetches. */
export const SUGGESTIONS_CHANGED = "supymem-suggestions-changed";

export type Suggestion = {
  id: string;
  venture_id: string;
  venture_name: string;
  title: string;
  why: string;
  plan_text: string;
  est_credits: number;
  action_kind: string;
  requires_mac: boolean;
  status: "new" | "saved" | "dismissed" | "accepted" | "executing" | "done" | "failed" | "running" | "needs_you" | "noop" | "expired";
  created_at: string;
  decided_at: string | null;
  result_ref: string | null;
  result_state?: "running" | "done" | "needs_you" | "noop" | null;
  result_href?: string;
  result_message?: string;
};

export type SuggestionList = { items: Suggestion[]; new: number; saved: number; next_offset?: number | null };
export type Decision = "accept" | "save" | "dismiss";
export type SuggestionsView = "venture" | "all";

/** A venture route defaults to its exact saved cards, including after navigation. */
export function suggestionsPath(ventureId: string, view: SuggestionsView, history = false): string {
  const query = new URLSearchParams();
  if (view === "venture") query.set("venture_id", ventureId);
  if (history) query.set("status", "history");
  return "/api/suggestions" + (query.size ? `?${query}` : "");
}

/** Defend against an old/ignored filter response; backend still owns membership. */
export function scopedSuggestions(items: Suggestion[], ventureId: string, view: SuggestionsView): Suggestion[] {
  return items.filter(item => item && typeof item.id === "string" && typeof item.venture_id === "string"
    && (view === "all" || item.venture_id === ventureId));
}

export function suggestionPlan(item: Suggestion): string {
  const plan = item.plan_text?.trim();
  return plan === item.title || plan === `Runs the step '${item.title}' for you.` ? item.why : plan || "";
}

export function suggestionsSnoozed(subject: string, nowMs = Date.now()): boolean {
  try {
    const until = Number(window.localStorage.getItem(`supymem-suggestions-later:${subject}`));
    return Number.isFinite(until) && until > nowMs;
  } catch { return false; }
}

// ponytail: device-local snooze, use an account snooze endpoint when one exists.
export function snoozeSuggestions(subject: string, nowMs = Date.now()): void {
  const until = new Date(nowMs);
  until.setHours(24, 0, 0, 0);
  try { window.localStorage.setItem(`supymem-suggestions-later:${subject}`, String(until.getTime())); } catch {}
}

/** Mirrors the Mac app's `Prefs` (src-tauri/src/suggestions.rs). */
export type SuggestionPrefs = {
  enabled: boolean;
  min_gap_minutes: number;
  quiet_enabled: boolean;
  quiet_start_hour: number;
  quiet_end_hour: number;
  paused_until: number;
};

export const DEFAULT_PREFS: SuggestionPrefs = {
  enabled: true,
  min_gap_minutes: 60,
  quiet_enabled: true,
  quiet_start_hour: 21,
  quiet_end_hour: 8,
  paused_until: 0,
};

export const GAP_CHOICES = [
  { minutes: 30, label: "At most every 30 minutes" },
  { minutes: 60, label: "At most every hour" },
  { minutes: 120, label: "At most every 2 hours" },
  { minutes: 240, label: "At most every 4 hours" },
] as const;

export const DECISION_LABEL: Record<Decision, string> = {
  accept: "Sure, go ahead",
  save: "Save for later",
  dismiss: "Dismiss",
};

/** Sidebar badge text: nothing for zero, "9+" past nine. */
export function badgeLabel(count: number): string {
  if (!Number.isFinite(count) || count <= 0) return "";
  return count > 9 ? "9+" : String(Math.floor(count));
}

/** Keep a stored or typed value inside what the Mac app accepts. */
export function sanitizePrefs(value: Partial<SuggestionPrefs> | null | undefined): SuggestionPrefs {
  const v = { ...DEFAULT_PREFS, ...(value || {}) };
  const hour = (n: unknown, fallback: number) => (Number.isInteger(n) && (n as number) >= 0 && (n as number) <= 23 ? (n as number) : fallback);
  return {
    enabled: v.enabled !== false,
    min_gap_minutes: Math.min(720, Math.max(15, Math.round(Number(v.min_gap_minutes) || DEFAULT_PREFS.min_gap_minutes))),
    quiet_enabled: v.quiet_enabled !== false,
    quiet_start_hour: hour(v.quiet_start_hour, DEFAULT_PREFS.quiet_start_hour),
    quiet_end_hour: hour(v.quiet_end_hour, DEFAULT_PREFS.quiet_end_hour),
    paused_until: Math.max(0, Math.round(Number(v.paused_until) || 0)),
  };
}

export function hourLabel(hour: number): string {
  const h = ((hour % 24) + 24) % 24;
  return `${h % 12 === 0 ? 12 : h % 12}:00 ${h < 12 ? "AM" : "PM"}`;
}

/** "Paused until 12:00 AM" style note, or "" when not paused. `nowMs` is injectable for tests. */
export function pausedNote(prefs: SuggestionPrefs, nowMs: number = Date.now()): string {
  if (prefs.paused_until * 1000 <= nowMs) return "";
  return "Paused for today. Suggestions stay in your list.";
}

/** Status text for the history list. */
export function statusLabel(s: Suggestion["status"], resultRef?: string | null): string {
  if (s === "running") return "Running";
  if (s === "needs_you") return "Needs you";
  if (s === "noop") return "Already done, nothing to do";
  if (s === "expired") return "State changed, review a fresh suggestion";
  if (resultRef === "queued" || resultRef === "in_progress") return "Needs you · verify the saved outcome";
  if (resultRef === "outcome_unconfirmed") return "Outcome unconfirmed";
  if (resultRef === "needs_you" || resultRef === "refusal") return "Needs you";
  if (resultRef === "mac_review_required") return "Ready for Mac review";
  if (resultRef === "mac_prepare_required") return "Mac review must be prepared";
  if (s === "done") return resultRef === "completion_verified" || resultRef?.startsWith("{") ? "Done" : "Completion unconfirmed";
  return {
    new: "New", saved: "Saved for later", dismissed: "Dismissed", accepted: "Needs you · verify the saved outcome",
    executing: "Working on it", done: "Done", failed: "Did not finish",
  }[s] || s;
}

type Reply = { status: number; data: unknown };

/** The overlay window's only way to reach the API: a narrow native command, never the general one. */
export async function overlayApi(method: "GET" | "POST", path: string, body?: unknown): Promise<{ ok: boolean; status: number; data: unknown }> {
  try {
    const reply = await desktopInvokeOrThrow<Reply>("suggestions_api_request", { method, path, ...(body === undefined ? {} : { body }) });
    return { ok: reply.status >= 200 && reply.status < 300, status: reply.status, data: reply.data };
  } catch {
    return { ok: false, status: 0, data: null };
  }
}

export async function loadPrefs(): Promise<SuggestionPrefs> {
  const saved = await desktopInvokeOrThrow<SuggestionPrefs>("suggestions_get_prefs");
  if (!saved || typeof saved !== "object") throw new Error("Suggestion settings could not be read.");
  return sanitizePrefs(saved);
}

export async function savePrefs(prefs: SuggestionPrefs): Promise<SuggestionPrefs | null> {
  const saved = await desktopInvoke<SuggestionPrefs>("suggestions_set_prefs", { prefs: sanitizePrefs(prefs) });
  return saved ? sanitizePrefs(saved) : null;
}
