export const NEAR_BOTTOM_PX = 32;

export const isNearBottom = (el: { scrollHeight: number; scrollTop: number; clientHeight: number }) =>
  el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;

/** What to do when the message list changed: follow the newest, show a "New messages" pill, or nothing. */
export function scrollDecision({ newMessage, following, selecting, own = false, streaming = false }: { newMessage: boolean; following: boolean; selecting: boolean; own?: boolean; streaming?: boolean }): "follow" | "pill" | "none" {
  if (selecting) return newMessage ? "pill" : "none";
  // Only a streaming reply grows in place; polls and re-renders never move the view. The founder's own send always jumps down.
  if (!newMessage) return following && streaming ? "follow" : "none";
  return following || own ? "follow" : "pill";
}

export function readScrollAnchor(el: HTMLElement): {id: string; offset: number} | null {
  const top = el.getBoundingClientRect().top;
  const row = Array.from(el.querySelectorAll<HTMLElement>("[data-message-id]"))
    .find(message => message.getBoundingClientRect().bottom > top);
  return row?.dataset.messageId ? {id: row.dataset.messageId, offset: row.getBoundingClientRect().top - top} : null;
}

export function restoreScrollAnchor(el: HTMLElement, anchor: {id: string; offset: number} | null): void {
  if (!anchor) return;
  const row = Array.from(el.querySelectorAll<HTMLElement>("[data-message-id]"))
    .find(message => message.dataset.messageId === anchor.id);
  if (!row) return;
  const delta = row.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset;
  if (delta) el.scrollTop += delta;
}
