export type HistoryScrollAnchor = {
  firstId?: string;
  scrollHeight: number;
  scrollTop: number;
};

/** Preserve the reader's position when older messages appear above the current page. */
export function historyScrollPosition(anchor: HistoryScrollAnchor, firstId: string | undefined, scrollHeight: number): number | null {
  if (anchor.firstId === firstId) return null;
  return anchor.scrollTop + Math.max(0, scrollHeight - anchor.scrollHeight);
}
