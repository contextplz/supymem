/** Pure chat-composer helpers — shared by dock, bar, and tests. */

export const COMPOSER_MIN_LINES = 1;
export const COMPOSER_MAX_LINES = 8;
export const COMPOSER_LINE_PX = 22;
export const COMPOSER_PAD_PX = 8;

export const DOCK_WIDTH_MIN = 320;
export const DOCK_WIDTH_MAX = 720;
export const DOCK_WIDTH_DEFAULT = 400;
export const DOCK_HEIGHT_DEFAULT = 620;
export const DOCK_HEIGHT_RANGE = 0.25; // ±25% of default
export const DOCK_STORAGE_KEY = "supymem.chatDock.size.v1";
const CHAT_DRAFT_PREFIX = "supymem.chatDraft.v1";
export const CHAT_DRAFT_MAX_CHARS = 200000;

export type DockSize = { width: number; height: number };

/** Durable so a relaunch cannot discard typing, and account-scoped
 * so a different founder on the same Mac cannot inherit another account's draft. */
export function chatDraftKey(subject: string | undefined, ventureId: string | undefined, conversationId?: string | null): string | null {
  if (!subject || !/^(?:usr_[a-zA-Z0-9_-]+|founder)$/.test(subject) || !ventureId || !/^ven_[a-zA-Z0-9_-]+$/.test(ventureId)) return null;
  if (conversationId && !/^[A-Za-z0-9_-]{1,128}$/.test(conversationId)) return null;
  return `${CHAT_DRAFT_PREFIX}:${encodeURIComponent(subject)}:${encodeURIComponent(ventureId)}${conversationId ? `:${encodeURIComponent(conversationId)}` : ""}`;
}

type ChatDraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function draftStorage(): Storage | null {
  try { return typeof window === "undefined" ? null : window.localStorage; }
  catch { return null; }
}

export function readChatDraft(storage: ChatDraftStorage | null, key: string | null): string {
  try {
    const draft = key ? (storage?.getItem(key) || "") : "";
    return draft.length <= CHAT_DRAFT_MAX_CHARS ? draft : "";
  }
  catch { return ""; }
}

export function writeChatDraft(storage: ChatDraftStorage | null, key: string | null, draft: string): void {
  try {
    // ponytail: drafts over the entry input limit stay memory-only; use IndexedDB if larger inputs are supported.
    if (!key || !storage || draft.length > CHAT_DRAFT_MAX_CHARS) return;
    if (draft) storage.setItem(key, draft);
    else storage.removeItem(key);
  } catch { /* Storage denial must not block typing. */ }
}

/** An older in-flight send must never erase text entered after reconnect. */
export function clearChatDraftIfMatches(storage: ChatDraftStorage | null, key: string | null, sent: string): void {
  try {
    if (key && storage?.getItem(key) === sent) storage.removeItem(key);
  } catch { /* Best effort; the visible send result remains authoritative. */ }
}

/** Guard against empty drafts and double-submit while a send is in flight. */
export function canStartSend(body: string, pending: boolean): boolean {
  return body.trim().length > 0 && !pending;
}

export function shouldSendOnKey(event: {
  key: string;
  shiftKey: boolean;
  isComposing?: boolean;
  keyCode?: number;
  nativeEvent?: { isComposing?: boolean; keyCode?: number };
}): boolean {
  if (event.key !== "Enter" || event.shiftKey) return false;
  const composing =
    event.isComposing === true
    || event.nativeEvent?.isComposing === true
    || event.keyCode === 229
    || event.nativeEvent?.keyCode === 229;
  return !composing;
}

/** After a failed send, restore only if the box is still empty or unchanged from the clear. */
export function draftAfterFailedSend(args: {
  failedText: string;
  currentDraft: string;
}): string {
  const current = args.currentDraft;
  if (current.trim() === "" || current === args.failedText) return args.failedText;
  return current;
}

export function composerHeightPx(args: {
  scrollHeight: number;
  lineHeight?: number;
  padding?: number;
  maxLines?: number;
  dockHeight?: number;
}): { height: number; capped: boolean } {
  const line = args.lineHeight ?? COMPOSER_LINE_PX;
  const pad = args.padding ?? COMPOSER_PAD_PX;
  const maxLines = args.maxLines ?? COMPOSER_MAX_LINES;
  const min = line * COMPOSER_MIN_LINES + pad;
  const byLines = line * maxLines + pad;
  const byDock = args.dockHeight != null ? Math.floor(args.dockHeight * 0.4) : byLines;
  const max = Math.max(min, Math.min(byLines, byDock));
  const next = Math.min(Math.max(args.scrollHeight, min), max);
  return { height: next, capped: args.scrollHeight > max };
}

export function clampDockSize(size: Partial<DockSize>, defaults?: DockSize): DockSize {
  const base = defaults || { width: DOCK_WIDTH_DEFAULT, height: DOCK_HEIGHT_DEFAULT };
  const heightMin = Math.round(base.height * (1 - DOCK_HEIGHT_RANGE));
  const heightMax = Math.round(base.height * (1 + DOCK_HEIGHT_RANGE));
  return {
    width: Math.min(DOCK_WIDTH_MAX, Math.max(DOCK_WIDTH_MIN, Math.round(size.width ?? base.width))),
    height: Math.min(heightMax, Math.max(heightMin, Math.round(size.height ?? base.height))),
  };
}

export function readStoredDockSize(storage?: Pick<Storage, "getItem"> | null): DockSize | null {
  try {
    const raw = storage?.getItem(DOCK_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<DockSize>;
    if (typeof parsed?.width !== "number" || typeof parsed?.height !== "number") return null;
    return clampDockSize(parsed);
  } catch {
    return null;
  }
}

export function writeStoredDockSize(
  size: DockSize,
  storage?: Pick<Storage, "setItem"> | null,
): void {
  try {
    storage?.setItem(DOCK_STORAGE_KEY, JSON.stringify(clampDockSize(size)));
  } catch {
    /* private mode / quota — ignore */
  }
}

export function nudgeDockSize(
  size: DockSize,
  edge: "width" | "height",
  delta: number,
  defaults?: DockSize,
): DockSize {
  if (edge === "width") return clampDockSize({ ...size, width: size.width + delta }, defaults);
  return clampDockSize({ ...size, height: size.height + delta }, defaults);
}

/** Composer placeholder: names the manager; @mentions are hinted by the mention menu, not the placeholder. */
export const composerPlaceholder = (manager?: string): string => `Ask ${manager?.trim() || "Charlie"} anything…`;
