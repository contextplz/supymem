/** Local recovery for an unsent manual idea review, scoped to its revision. */
export type ManualIdeaDraft = { problem: string; customer: string; offer: string };
type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function manualIdeaDraftKey(ventureId: string, revisionId: string): string {
  return `supymem:manual-idea-draft:${encodeURIComponent(ventureId)}:${encodeURIComponent(revisionId)}`;
}

export function readManualIdeaDraft(storage: DraftStorage, ventureId: string, revisionId: string): ManualIdeaDraft | null {
  try {
    const raw = storage.getItem(manualIdeaDraftKey(ventureId, revisionId));
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (!["problem", "customer", "offer"].every((field) => typeof value?.[field] === "string")) return null;
    return { problem: value.problem.slice(0, 2000), customer: value.customer.slice(0, 2000), offer: value.offer.slice(0, 2000) };
  } catch { return null; }
}

export function writeManualIdeaDraft(storage: DraftStorage, ventureId: string, revisionId: string, draft: ManualIdeaDraft): void {
  try { storage.setItem(manualIdeaDraftKey(ventureId, revisionId), JSON.stringify(draft)); }
  catch { /* Keep the form editable if browser storage is unavailable. */ }
}

export function clearManualIdeaDraft(storage: DraftStorage, ventureId: string, revisionId: string): void {
  try { storage.removeItem(manualIdeaDraftKey(ventureId, revisionId)); }
  catch { /* The saved review remains authoritative. */ }
}
