/** Whether a chat message's one-shot answer chips were answered, derived from the stored thread (survives reload). */
export type AnsweredState = { chosen: string | null; typed: string | null } | null;

export function confirmationState(messages: import("./api-types").ChatMessage[], messageId: string, action: string, card?: import("./api-types").ChatConfirmation): import("./api-types").ChatConfirmation["state"] {
  const sameActionCards = messages.find(message => message.id === messageId)?.actions?.filter(item =>
    (item.confirmation?.action || item.action || item.name) === action && item.confirmation);
  if (sameActionCards && sameActionCards.length > 1) {
    // Receipts identify only the message and action; each saved card has its own exact-payload state.
    return card?.state;
  }
  const reply = messages.findLast(message => message.confirmed_action?.message_id === messageId && message.confirmed_action.action === action);
  const receipt = reply?.confirmed_action;
  return receipt ? receipt.state || (reply?.kind === "needs_you" || reply?.kind === "confirmation" ? "not_started" : "started") : messages.find(message => message.id === messageId)?.actions?.find(item =>
    (item.confirmation?.action || item.action || item.name) === action)?.confirmation?.state;
}

export function confirmationStarted(messages: import("./api-types").ChatMessage[], messageId: string, action: string, card?: import("./api-types").ChatConfirmation): boolean {
  return confirmationState(messages, messageId, action, card) === "started";
}

type Msg = { role?: string; body_md?: string; text?: string };
const isFounder = (m: Msg) => m.role === "founder" || m.role === "user";

/** null while open; otherwise the chip the founder picked (chosen) or their own words (typed). */
export function answeredState(messages: Msg[], index: number, optionTexts: string[]): AnsweredState {
  for (let i = index + 1; i < messages.length; i++) {
    const m = messages[i];
    if (!isFounder(m)) continue;
    const said = (m.body_md ?? m.text ?? "").trim();
    const chosen = optionTexts.find((o) => o.trim() === said) ?? null;
    return { chosen, typed: chosen ? null : said };
  }
  return null;
}

/** True when an error message (`local-err-<ms>`) was added at or after `startedMs`. */
export function failedSince(messages: { id: string }[], startedMs: number): boolean {
  return messages.some((m) => m.id.startsWith("local-err-") && Number(m.id.slice(10)) >= startedMs);
}

/** Clicked-button set after an action settles: a failed click is released so the founder can retry. */
export function settleClick(clicked: Set<string>, key: string, ok: boolean): Set<string> {
  if (ok || !clicked.has(key)) return clicked;
  const next = new Set(clicked);
  next.delete(key);
  return next;
}
