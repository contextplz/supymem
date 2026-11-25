/** Ask the venture chat to show some text in its composer (nothing is sent), opening the panel if it is closed. */
export const PREFILL_ACTION = "chat.prefill";
const CLIENT_ACTIONS = new Set([
  "chat.reply", PREFILL_ACTION, "chat.stop", "chat.startFresh", "chat.new_conversation",
  "task.run", "mac.task.prepare", "gtm.route.confirm", "gtm.diagnosis.start",
  "entry.direction.confirm", "entry.direction.back",
]);

export function isClientChatAction(action: string): boolean {
  return CLIENT_ACTIONS.has(action);
}

export function applyChatPrefill(text: string): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent("supymem-chat-prefill", { detail: { text } }));
}

// Same event the next-step buttons use (AppShell opens the panel and hands it to the launcher).
const ASK_EVENT = "supymem:ask";

export function prefillChat(text: string): void {
  if (typeof window === "undefined") return;
  // An open composer listens for this directly; a closed panel opens from the ask below and then replays it.
  applyChatPrefill(text);
  window.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: { action: PREFILL_ACTION, payload: { text } } }));
}
