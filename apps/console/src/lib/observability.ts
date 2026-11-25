import { usageFeature } from "./usageFeature.ts";
import { isErrorElement } from "./userError.ts";

export const OBSERVABILITY_EVENT = "supymem-observability";
export const JOURNEY_STEPS = ["idea", "clarify", "validate", "review", "find_customers", "review_leads", "sent", "call_booked"] as const;
export type JourneyStep = typeof JOURNEY_STEPS[number];
export type DeadEndCode = "ui_error_shown" | "reconnect_shown" | "empty_no_action" | "action_failed";
export type Observation = { event: "ui.dead_end"; code: DeadEndCode } |
  { event: "journey.step_entered" | "journey.step_completed"; step: JourneyStep; ventureId: string; workspaceId?: string };

export function consoleRoute(path: string): string {
  const pathname = path.split(/[?#]/, 1)[0];
  if (pathname.startsWith("/auth/")) return "/auth";
  if (pathname.startsWith("/invite/")) return "/other";
  const route = pathname.replace(/^\/v\/[^/]+(?=\/|$)/, "/v/{id}");
  if (route === "/other") return route;
  if (!usageFeature(path.split(/[?#]/, 1)[0])) return "/other";
  if (route.startsWith("/v/{id}")) return /^\/v\/\{id\}(?:\/[a-z][a-z-]*)?$/.test(route) ? route : "/other";
  return /^\/(?:[a-z][a-z-]*(?:\/[a-z][a-z-]*)*)?$/.test(route) ? route : "/other";
}

export function observeFounder(detail: Observation): void {
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(OBSERVABILITY_EVENT, { detail }));
}

export function deadEndCode(element: Element): DeadEndCode | null {
  if (element.closest("[hidden],[aria-hidden='true'],[data-not-error]")) return null;
  if (element.getClientRects && !element.getClientRects().length) return null;
  const explicit = element.getAttribute("data-dead-end");
  if (explicit === "reconnect_shown") return explicit;
  if (explicit === "empty_no_action") {
    const host = element.closest("section,article") || element;
    const actions = host.querySelectorAll("a[href]:not([aria-disabled='true']),button:not(:disabled):not([aria-disabled='true']),input:not(:disabled):not([type='hidden']),select:not(:disabled),textarea:not(:disabled)");
    return Array.from(actions).some(action => !action.closest("[hidden],[aria-hidden='true']") &&
      (!action.getClientRects || action.getClientRects().length)) ? null : explicit;
  }
  if (isErrorElement(element)) return "ui_error_shown";
  return null;
}

export function startDeadEndObserver(): () => void {
  if (typeof MutationObserver === "undefined" || typeof document === "undefined") return () => {};
  const selector = "[role='alert'],[data-error-notice],[class*='error'],[class*='fail'],[class*='err'],[data-dead-end],[data-journey-step]";
  const seen = new WeakMap<Element, string>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  // ponytail: full marked-DOM scan per mutation batch; inspect changed subtrees if large screens make this costly.
  const scan = () => {
    timer = undefined;
    document.querySelectorAll(selector).forEach(element => {
      const step = element.getAttribute("data-journey-step") as JourneyStep;
      const ventureId = element.getAttribute("data-journey-venture");
      if (JOURNEY_STEPS.includes(step) && ventureId && !element.closest("[hidden],[aria-hidden='true']") &&
          (!element.getClientRects || element.getClientRects().length)) {
        const signature = `${step}:${ventureId}`;
        if (seen.get(element) !== signature) observeFounder({ event: "journey.step_entered", step, ventureId,
          ...(element.getAttribute("data-journey-workspace") ? { workspaceId: element.getAttribute("data-journey-workspace")! } : {}) });
        seen.set(element, signature);
        return;
      }
      const code = deadEndCode(element);
      const signature = code ? `${code}:${element.textContent}` : "";
      if (signature && seen.get(element) !== signature) observeFounder({ event: "ui.dead_end", code: code! });
      seen.set(element, signature);
    });
  };
  const observer = new MutationObserver(() => { if (timer === undefined) timer = setTimeout(scan, 0); });
  observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ["role", "class", "style", "hidden", "aria-hidden", "aria-disabled", "data-dead-end", "disabled", "data-journey-step", "data-journey-venture"] });
  timer = setTimeout(scan, 0);
  return () => { observer.disconnect(); if (timer !== undefined) clearTimeout(timer); };
}

export function ideaObservationStep(state: string): JourneyStep | null {
  return ({ open: "idea", recon: "clarify", clarifying: "clarify", validating: "validate",
    proposing: "review", reviewing: "review", confirmed: "review" } as Record<string, JourneyStep>)[state] || null;
}
