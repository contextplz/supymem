export type ChatConversation = {
  id: string; venture_id: string; workspace_id: string; visibility: "private" | "legacy_shared";
  title: string; created_at: string | null; updated_at: string | null;
  pinned: boolean; archived: boolean; folder: string | null;
};
export type ChatConversationCatalog = {
  venture_id: string; workspace_id: string; actor_id: string; legacy_conversation_id: string; items: ChatConversation[];
};

const id = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const actorId = (subject: string) => ["founder", "local", "per_local000000"].includes(subject) ? "per_local000000" : subject;

export function chatConversationItem(value: unknown, venture: string, workspace: string): ChatConversation | null {
  if (!record(value) || !id(value.id) || value.venture_id !== venture || value.workspace_id !== workspace ||
      !["private", "legacy_shared"].includes(String(value.visibility)) || typeof value.title !== "string" || value.title.length > 120 ||
      !(value.created_at === null || typeof value.created_at === "string") || !(value.updated_at === null || typeof value.updated_at === "string") ||
      !(value.pinned === undefined || typeof value.pinned === "boolean") || !(value.archived === undefined || typeof value.archived === "boolean") ||
      !(value.folder === undefined || value.folder === null || typeof value.folder === "string")) return null;
  return {...value, pinned: value.pinned ?? false, archived: value.archived ?? false, folder: value.folder || null} as ChatConversation;
}

export function chatConversationCatalog(value: unknown, venture: string, subject: string): ChatConversationCatalog | null {
  if (!record(value) || value.venture_id !== venture || !id(value.workspace_id) || value.actor_id !== actorId(subject) ||
      !id(value.legacy_conversation_id) || !Array.isArray(value.items) || value.items.length > 200) return null;
  const items = value.items.map(item => chatConversationItem(item, venture, value.workspace_id as string));
  if (items.some(item => !item) || new Set(items.map(item => item!.id)).size !== items.length ||
      items.some(item => (item!.visibility === "legacy_shared") !== (item!.id === value.legacy_conversation_id))) return null;
  return {...value, items} as ChatConversationCatalog;
}

export type ChatConversationPatch = Partial<Pick<ChatConversation, "title" | "pinned" | "archived" | "folder">>;
export type ChatHistoryItem = {id: string; text: string; at: string; pinned?: boolean; archived?: boolean; folder?: string | null};

export function conversationListPath(venture: string, current: string | null = null, archived = false): string {
  const query = new URLSearchParams();
  if (current) query.set("current", current);
  if (archived) query.set("archived", "1");
  return `/api/ventures/${encodeURIComponent(venture)}/chat/conversations${query.size ? `?${query}` : ""}`;
}

export function historyGroups(items: ChatHistoryItem[], now = new Date()): {key: string; label: string; items: ChatHistoryItem[]; folder?: boolean}[] {
  const dayKey = (date: Date) => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
  const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
  const dayLabel = new Intl.DateTimeFormat("en", {month: "short", day: "numeric"});
  const pinned: ChatHistoryItem[] = [], folders = new Map<string, ChatHistoryItem[]>();
  const days = new Map<string, {key: string; label: string; items: ChatHistoryItem[]; folder?: boolean}>();
  for (const item of [...items].sort((first, second) => (Date.parse(second.at) || 0) - (Date.parse(first.at) || 0))) {
    if (item.pinned) {pinned.push(item); continue;}
    if (item.folder) {
      if (!folders.has(item.folder)) folders.set(item.folder, []);
      folders.get(item.folder)!.push(item); continue;
    }
    const at = new Date(item.at), valid = !Number.isNaN(at.getTime());
    const key = valid ? dayKey(at) : "earlier";
    const label = !valid ? "Earlier" : key === dayKey(now) ? "Today" : key === dayKey(yesterday) ? "Yesterday" : dayLabel.format(at);
    if (!days.has(key)) days.set(key, {key: `day:${key}`, label, items: []});
    days.get(key)!.items.push(item);
  }
  return [
    ...(pinned.length ? [{key: "pinned", label: "Pinned", items: pinned}] : []),
    ...[...folders].sort(([first], [second]) => first.localeCompare(second)).map(([label, grouped]) => ({key: `folder:${label}`, label, folder: true, items: grouped})),
    ...days.values(),
  ];
}

export function conversationChatPath(venture: string, conversation: string | null, before?: string | null): string {
  const query = new URLSearchParams();
  if (conversation) query.set("conversation_id", conversation);
  if (before) query.set("before", before);
  return `/api/ventures/${encodeURIComponent(venture)}/chat${query.size ? `?${query}` : ""}`;
}

/** Exact live venture check also catches route retirement before a Next render. */
export function chatRouteCurrent(venture: string, initialPath: string, pathname: string, initialSearch = "", search = ""): boolean {
  const match = /^\/v\/([^/]+)(?:\/|$)/.exec(pathname);
  try {
    if (match) return decodeURIComponent(match[1]) === venture;
    if (pathname !== initialPath || /^\/v\//.test(initialPath)) return false;
    const original = new URLSearchParams(initialSearch), live = new URLSearchParams(search);
    return ["workspace", "venture"].every(key => original.get(key) === live.get(key)) &&
      (!live.has("venture") || live.get("venture") === venture);
  }
  catch { return false; }
}

export function conversationSelectionKey(subject: string, venture: string): string {
  return `supymem.chatConversation.v1:${encodeURIComponent(subject)}:${encodeURIComponent(venture)}`;
}

export function conversationResponseMatches(value: unknown, conversation: string | null): boolean {
  return record(value) && (conversation === null ? value.conversation_id == null : value.conversation_id === conversation);
}
