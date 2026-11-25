import type { InboxBriefingItem, InboxBriefingPayload } from "./api-types";

export function validInboxBriefing(value: unknown): value is InboxBriefingPayload {
  if (!value || typeof value !== "object") return false;
  const payload = value as InboxBriefingPayload;
  const item = (entry: InboxBriefingItem) => entry && [entry.id, entry.from, entry.subject, entry.reason].every(text => typeof text === "string")
    && !!entry.id && ["important", "worth_a_look", "unclassified"].includes(entry.category) && Array.isArray(entry.actions)
    && entry.actions.every(action => action && typeof action.label === "string");
  return Array.isArray(payload.items) && payload.items.every(item) && !!payload.filtered
    && [payload.filtered.newsletters, payload.filtered.notifications].every(count => Number.isSafeInteger(count) && count >= 0);
}
