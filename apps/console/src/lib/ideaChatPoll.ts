import type { ChatMessage } from "./api-types.ts";

const isProposal = (m: ChatMessage) =>
  m.actions?.some((a) => a.style === "primary" && /^\/v\/[^/]+\/idea$/.test(a.href ?? "")) ?? false;

export function ideaConversationActivity(messages: ChatMessage[], saving = false, now = Date.now()): string | null {
  if (saving) return "Saving your answer";
  const latest = messages.findLast(message => /^(?:entry_pending|idea_failed|idea_clarification_(?:pending|ready)|idea_validation_(?:ready|failed))$/.test(message.kind || ""));
  if (!latest) return null;
  if (latest.kind === "idea_failed") return "Couldn't prepare the next question";
  if (latest.kind === "entry_pending" || latest.kind === "idea_clarification_pending") {
    return now - Date.parse(latest.created_at || latest.ts || "") >= 45_000
      ? "Still working on a good question. This can take up to 3 minutes."
      : "Preparing your next question";
  }
  if (latest.kind === "idea_validation_failed") return "Review your saved status";
  const question = latest.actions?.some(action => (action.action || action.name) === "chat.reply"
    && typeof (action.payload as { question_id?: unknown } | null)?.question_id === "string");
  return question || /Reply here with your answer/.test(latest.body_md || "") ? "Answer the next question about your idea" : "Review your saved idea";
}

/** `after` = saved messages following the pending one. "done" stops polling; "proposal" keeps waiting for the validation outcome. */
export function ideaPollState(after: ChatMessage[]): "waiting" | "proposal" | "done" {
  if (after.some(message => message.kind === "idea_failed")) return "done";
  const i = after.findIndex((m) => m.kind === "idea_clarification_ready");
  if (i < 0) return "waiting";
  if (!isProposal(after[i])) return "done";
  return after.slice(i + 1).some((m) => m.kind === "idea_validation_ready" || m.kind === "idea_validation_failed")
    ? "done"
    : "proposal";
}

/** Should history polling run for this latest message? "pending" waits for a new question; "proposal" waits for validation. */
export function ideaPollStart(last: ChatMessage | undefined): "pending" | "proposal" | null {
  if (!last || last.role === "user") return null;
  if (last.kind === "entry_pending" || last.kind === "idea_clarification_pending") return "pending";
  return last.kind === "idea_clarification_ready" && isProposal(last) ? "proposal" : null;
}
