import { USAGE_LIMIT_MESSAGE, usageLimitMessage } from "./credits.ts";
/** Plain-language reason for a failed chat confirm (and GTM quote approval), from the server's stable error code. */

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

/** The stable code from any of the error envelopes the API uses, or "". */
export function chatErrorCode(data: unknown): string {
  const root = object(data), detail = object(root.detail), nested = object(detail.error), error = object(root.error);
  const code = [error.code, nested.code, detail.code, typeof root.detail === "string" ? root.detail : null]
    .find((value) => typeof value === "string");
  return (code as string | undefined) || "";
}

export function chatActionPending(status: number, data: unknown): boolean {
  return status === 0 || status === 408 || status === 504 || chatErrorCode(data) === "upstream_timeout";
}

export class ChatActionPendingError extends Error {
  constructor() {
    super("This is taking longer than usual. I'll post the result here.");
  }
}

const CONFIRM_REASONS: Record<string, string> = {
  chat_request_in_progress: "This action is already starting. Check its saved status before continuing.",
  chat_request_outcome_unknown: "This action may already have started. Check its saved status before continuing.",
  account_changed_during_request: "Your connection changed while this action finished. Check its saved status before continuing.",
  task: "That item was already started or is no longer open, so I did not run it. Open Work to see where it stands.",
  build: "Your build could not be found. Ask me to show your saved status here before trying again.",
  app: "Your app could not be found. Ask me to show your saved status here before trying again.",
  venture: "I could not find this venture. Refresh the page and try again.",
  build_quote_confirmation_required: "That build quote is no longer valid, so nothing started. Ask me to build again for a fresh quote.",
  build_quote_expired: "That quote expired, so nothing started. Ask me to build again for a fresh quote.",
  build_quote_inputs_changed: "The build changed since you saw its price, so nothing started. Ask me to build again for a fresh quote.",
  build_chat_authentication_required: "Please sign in again, then confirm once more.",
  publish_chat_authentication_required: "Please sign in again, then confirm once more.",
  credits_unavailable: "That didn't start because of a problem on our side. Check the conversation, then try again in a moment.",
  duplicate_ledger_entry: "That didn't start because of a problem on our side. Check the conversation, then try again in a moment.",
  unknown_action: "That action is no longer available, so nothing ran. You can prepare a fresh request below.",
};

export function confirmFailureActions(data: unknown, messages: import("./api-types").ChatMessage[]): NonNullable<import("./api-types").ChatMessage["actions"]> {
  if (chatErrorCode(data) !== "unknown_action") return [];
  const request = messages.findLast(message => (message.role === "founder" || message.role === "user") && message.body_md?.trim());
  return [{ label: "Prepare a fresh request", action: "chat.prefill",
    payload: { text: request?.body_md || "Show me a working next step using my saved context." } }];
}

/** The message shown when POST /chat/confirm fails (402 is handled by the caller with the credits guide). */
export function confirmFailureMessage(status: number, data: unknown): string {
  const code = chatErrorCode(data);
  if (status === 402 || code.endsWith("insufficient_credits")) return usageLimitMessage();
  if (chatActionPending(status, data)) return new ChatActionPendingError().message;
  if (code && CONFIRM_REASONS[code]) return CONFIRM_REASONS[code];
  if (status === 401 || status === 403) return "You don't have permission to run that here. Sign in again or ask the workspace owner.";
  if (status === 404) return "That item no longer exists, so nothing ran. Ask me again and I'll prepare a fresh card.";
  if (status === 409) return "Something changed since I prepared that, so nothing ran. Ask me again for a fresh card.";
  if (status === 400 || status === 422) return "I couldn't run that as requested, so nothing ran. Ask me again and I'll prepare a fresh card.";
  return "That didn't run because of a problem on our side. Nothing was changed. Try again in a moment.";
}

const GTM_REASONS: Record<string, string> = {
  gtm_insufficient_credits: USAGE_LIMIT_MESSAGE,
  gtm_quote_changed: "The price or inputs changed since that quote, so nothing started. Ask me to diagnose again for a new quote.",
  gtm_quote_not_found: "I couldn't find that quote. Ask me to diagnose again for a new one.",
  gtm_quote_unreadable: "That quote can't be read any more. Ask me to diagnose again for a new one.",
  gtm_idempotency_conflict: "This diagnosis was already approved. Check the conversation for its result before approving again.",
};

/** The message shown when approving a chat GTM diagnosis quote fails. */
export function gtmConfirmFailureMessage(status: number, data: unknown): string {
  const code = chatErrorCode(data);
  if (status === 402 || code.endsWith("insufficient_credits")) return usageLimitMessage();
  if (code && GTM_REASONS[code]) return GTM_REASONS[code];
  if (status === 402) return GTM_REASONS.gtm_insufficient_credits;
  if (status === 0) return "I couldn't reach Supymem, so I can't tell whether the diagnosis started. Check the conversation before approving again.";
  if (status === 409) return GTM_REASONS.gtm_quote_changed;
  return "The diagnosis was not started. Nothing was charged. Ask me to diagnose again for a fresh quote.";
}
