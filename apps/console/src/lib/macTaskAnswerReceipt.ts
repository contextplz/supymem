import type { ChatMessage } from "./api-types";

/** Only the server-selected order may supply a local supplement to a chat answer. */
export function macTaskAnswerOrderId(message: ChatMessage, ventureId: string): string | null {
  const href = message.actions?.find((action) => action.label === "Review Mac task")?.href;
  const match = /^\/v\/([A-Za-z0-9_-]+)\/device-work-orders\/([A-Za-z0-9_-]+)$/.exec(href || "");
  return match && match[1] === ventureId ? match[2] : null;
}

type LocalReceipt = {
  order_id?: unknown;
  state?: unknown;
  failure_code?: unknown;
  driver_failure_code?: unknown;
  task_verified?: unknown;
  evidence?: {
    steps?: Array<{
      kind?: unknown;
      operation?: unknown;
      target_label?: unknown;
      effect?: unknown;
      after_effect?: unknown;
      visible_facts?: unknown;
      same_process?: unknown;
      reviewed_window_present?: unknown;
      other_window_count?: unknown;
    }> | null;
    visual_review?: {
      verdict?: unknown;
      visible_facts?: unknown;
    } | null;
  } | null;
};

function safeFact(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const cleaned = value.replace(/[\r\n\t]+/g, " ").replace(/[\\`*_\[\](){}<>#|]/g, " ")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[redacted-email]")
    .replace(/\b(?:api[_-]?key|secret|token|password)\b\s*[:=]?\s*\S*/gi, "[redacted]")
    .replace(/\s+/g, " ").trim();
  return cleaned ? cleaned.slice(0, 180) : null;
}

/**
 * Plain chat summary of a stopped Mac task: what Supymem saw on screen, never
 * driver receipts (those live on the order page under "How Supymem checked").
 * Screenshot facts are observations, never proof of the whole approved task.
 */
export function localMacTaskObservation(receipt: unknown, orderId: string): string | null {
  if (!receipt || typeof receipt !== "object") return null;
  const saved = receipt as LocalReceipt;
  if (saved.order_id !== orderId || saved.state !== "stopped") return null;
  const steps = Array.isArray(saved.evidence?.steps) ? saved.evidence.steps : [];
  const review = saved.evidence?.visual_review;
  const clean = (list: unknown): string[] => Array.isArray(list)
    ? list.slice(0, 3).map(safeFact).filter((fact): fact is string => Boolean(fact)) : [];
  const last = steps.findLast((step) => step.kind === "visual_review" && clean(step.visible_facts).length);
  const facts = last ? clean(last.visible_facts)
    : review && ["done", "continue", "blocked"].includes(String(review.verdict)) ? clean(review.visible_facts) : [];
  const seen = facts.length
    ? `The Mac task stopped; Supymem saw on screen: ${facts.join(", ")}.`
    : "The Mac task stopped before Supymem could confirm what happened on screen.";
  return `${seen} It is not confirmed done; open Review Mac task for the full check.`;
}
