/** A question-scoped draft that survives a native app restart or update. */
export type IdeaDraft = { answer: string; selectedOptions: string[] };
type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function ideaDraftKey(ventureId: string, questionId: string): string {
  return `supymem:idea-draft:${encodeURIComponent(ventureId)}:${encodeURIComponent(questionId)}`;
}

export function readIdeaDraft(storage: DraftStorage, ventureId: string, questionId: string): IdeaDraft {
  try {
    const value = JSON.parse(storage.getItem(ideaDraftKey(ventureId, questionId)) || "null");
    return {
      answer: typeof value?.answer === "string" ? value.answer.slice(0, 4000) : "",
      selectedOptions: Array.isArray(value?.selectedOptions)
        ? value.selectedOptions.filter((item: unknown): item is string => typeof item === "string").slice(0, 8)
        : [],
    };
  } catch { return { answer: "", selectedOptions: [] }; }
}

export function writeIdeaDraft(storage: DraftStorage, ventureId: string, questionId: string, draft: IdeaDraft): void {
  try { storage.setItem(ideaDraftKey(ventureId, questionId), JSON.stringify(draft)); }
  catch { /* storage can be disabled or full; the form still works */ }
}

export function clearIdeaDraft(storage: DraftStorage, ventureId: string, questionId: string): void {
  try { storage.removeItem(ideaDraftKey(ventureId, questionId)); }
  catch { /* the saved answer remains authoritative */ }
}

/** Migrate drafts saved by older app versions in sessionStorage. */
export function readPersistentIdeaDraft(
  persistent: DraftStorage, legacy: DraftStorage, ventureId: string, questionId: string,
): IdeaDraft {
  const saved = readIdeaDraft(persistent, ventureId, questionId);
  if (saved.answer || saved.selectedOptions.length) return saved;
  const previous = readIdeaDraft(legacy, ventureId, questionId);
  if (previous.answer || previous.selectedOptions.length) writeIdeaDraft(persistent, ventureId, questionId, previous);
  return previous;
}
