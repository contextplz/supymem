/** Unsent corrections are local to the authenticated workspace and idea revision. */
export type IdeaCorrectionDraft = { answer: string; selectedOptions: string[] };
export type IdeaCorrectionScope = {
  workspaceId: string; ventureId: string; revisionId: string;
  target: "question" | "field"; targetId: string;
};
type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function ideaCorrectionDraftKey(scope: IdeaCorrectionScope): string {
  return `supymem:idea-correction-draft:${[scope.workspaceId, scope.ventureId, scope.revisionId, scope.target, scope.targetId].map(encodeURIComponent).join(":")}`;
}

export function readIdeaCorrectionDraft(storage: DraftStorage, scope: IdeaCorrectionScope): IdeaCorrectionDraft | null {
  try {
    const raw = storage.getItem(ideaCorrectionDraftKey(scope));
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (typeof value?.answer !== "string" || !Array.isArray(value?.selectedOptions)) return null;
    return {
      answer: value.answer.slice(0, 4000),
      selectedOptions: value.selectedOptions.filter((item: unknown): item is string => typeof item === "string").slice(0, 8),
    };
  } catch { return null; }
}

export function writeIdeaCorrectionDraft(storage: DraftStorage, scope: IdeaCorrectionScope, draft: IdeaCorrectionDraft): void {
  try { storage.setItem(ideaCorrectionDraftKey(scope), JSON.stringify(draft)); }
  catch { /* Keep the editor usable when storage is unavailable. */ }
}

export function clearIdeaCorrectionDraft(storage: DraftStorage, scope: IdeaCorrectionScope): void {
  try { storage.removeItem(ideaCorrectionDraftKey(scope)); }
  catch { /* A saved correction remains authoritative. */ }
}
