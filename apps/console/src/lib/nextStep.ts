import type { CompanyJourneyStep } from "./companyJourney.ts";
import type { FounderStage } from "./founderAssetsApi.ts";
import { inChatActions } from "./chatRouteAction.ts";

/** Server-computed venture next step (services/company_os/app/ventures/next_step.py). */
export type ServerNextStep = {
  key: string; stage: string; title: string; detail: string; cta_label: string;
  chat_action: string | null; payload: Record<string, unknown> | null; href: string;
  has_customers?: boolean; has_website?: boolean;
  skip_step?: string | null;
  journey_overrides?: { skipped: Record<string, { who: string; when: string; why: string }>; target?: string | null };
  founder_stage?: FounderStage | null; site_url?: string;
  alternatives?: { choice: "build" | "skip"; label: string; href: string; recommended?: boolean }[];
};
export type NextStepView = { eyebrow: string; title: string; detail: string; cta: string; href: string };

const isStep = (value: unknown): value is ServerNextStep => {
  const step = value as Partial<ServerNextStep> | null;
  return !!step && typeof step === "object" && [step.title, step.cta_label, step.href, step.stage].every(v => typeof v === "string" && v.length > 0)
    && step.href!.startsWith("/") && !step.href!.startsWith("//");
};

export function hasPageChoices(value: unknown): value is ServerNextStep {
  return isStep(value) && value.key === "site.build" && Array.isArray(value.alternatives)
    && value.alternatives.length === 2 && value.alternatives.some(a => a?.choice === "build")
    && value.alternatives.some(a => a?.choice === "skip");
}

/** Prefer the server's next step; fall back to the client journey so a missing or malformed field is harmless. */
export function selectNextStep(server: unknown, fallback: CompanyJourneyStep | null, fallbackHref: string): NextStepView | null {
  // Exact job receipts take precedence over an older dashboard's generic
  // working next-step text. This does not mark queued/unknown work complete.
  if (fallback?.workState && fallback.workState !== "none" && (!isStep(server) || server.key === "work.running")) return { eyebrow: fallback.eyebrow, title: fallback.title,
    detail: fallback.detail, cta: fallback.cta, href: fallbackHref };
  if (isStep(server)) return { eyebrow: server.stage, title: server.title, detail: server.detail || "", cta: server.cta_label, href: server.href };
  return fallback ? { eyebrow: fallback.eyebrow, title: fallback.title, detail: fallback.detail, cta: fallback.cta, href: fallbackHref } : null;
}

export type StepButton =
  | { kind: "link"; href: string; label: string }
  | { kind: "action"; action: string; payload: Record<string, unknown>; label: string }
  | { kind: "scroll"; id: string; label: string };

/** Chat actions /chat/confirm runs safely with the step's payload: each returns its own card or is read-only.
 * The rest (research.start, work.start, site.publish, waitlist.enable) would run unconfirmed or are not executable there, so they navigate. */
export const CONFIRM_SAFE_ACTIONS = new Set(["chat.prefill", "plan.generate", "plan.approve", "gtm.diagnosis", "resume", "job.retry", "build.prepare", "outreach.draft",
  "journey.skip_step", "journey.jump_to", "journey.restore_step", "venture.set_website"]);

/** The one button for the server step on the current page, or null when the founder is already there. */
export function nextStepButton(server: unknown, pathname: string, chatOpen = false, chatFirst = false): StepButton | null {
  if (!isStep(server)) return null;
  const label = server.cta_label;
  const opensChat = server.href.endsWith("?chat=1");
  if (opensChat && (chatOpen || pathname === "/new")) return null;
  if (server.chat_action && CONFIRM_SAFE_ACTIONS.has(server.chat_action))
    return { kind: "action", action: server.chat_action, payload: server.payload || {}, label };
  const ventureId = /^\/v\/([^/]+)/.exec(pathname)?.[1];
  const inChat = chatFirst ? inChatActions([{ label, href: server.href }], true, ventureId)[0] : null;
  if (inChat?.action) return { kind: "action", action: inChat.action, payload: inChat.payload || {}, label };
  const [path, hash = ""] = server.href.split("#");
  if (opensChat || (path.split("?")[0] || "/") !== pathname) return { kind: "link", href: server.href, label };
  return hash ? { kind: "scroll", id: hash, label } : null;
}

/** One CTA per step: with a visible strip CTA the status bar shows none, except a failed/blocked run
 * pointing somewhere the strip is not (and whose key does not already say retry/fail/error). */
export function dedupeStatusCta<T extends { href: string }>(cta: T | null, server: unknown, runState?: string, pathname = ""): T | null {
  if (!cta || (/\/(now|site)$/.test(pathname) && cta.href.split(/[?#]/)[0] === pathname)) return null;
  if (runState === "ready") return cta;
  // Active work is described by one status bar; its companion strip is hidden.
  if ((isStep(server) && server.key === "work.running") || runState === "working" || runState === "queued") return cta;
  if (!isStep(server)) return cta;
  if (!nextStepButton(server, pathname) && !hasPageChoices(server)) return cta;
  return failureOwnsCta(server, runState) ? cta : null;
}

/** On a failed run the status bar's recovery action replaces the strip button (unless the step is already a retry). */
export function failureOwnsCta(server: unknown, runState?: string): boolean {
  return (runState === "failed" || runState === "blocked") && !/retry|fail|error/i.test(String((server as { key?: unknown })?.key ?? ""));
}

export const asServerStep = (value: unknown): ServerNextStep | null => (isStep(value) ? value : null);

/** The growth-experiment prompt contradicts the Find customers page the founder is already working in. */
export const ownedByGtmFlow = (server: unknown, pathname: string): boolean =>
  (pathname === "/gtm" || /\/market$/.test(pathname)) && /^experiment\./.test(String((server as { key?: unknown })?.key ?? ""));
