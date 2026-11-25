export type SelectionMode = "single" | "multiple";
export const IDEA_ANSWER_MAX_CHARS = 4000;

export function editableIdeaAnswer(text: string): string {
  return text.replace(/^Assumed:\s*/gm, "").trim();
}

/** The saved skip marker drives routing; the founder sees the button they actually chose. */
export function founderChoiceWords(text: string): string {
  return text.replace(/^Skip this question$/gm, "I'm not sure, continue with what I shared");
}

export function questionSelectionMode(_mode?: SelectionMode): SelectionMode {
  return "multiple";
}

export function savedIdeaQuestionOptions(options?: { options?: string[]; selection_mode?: SelectionMode; allow_free_text?: boolean } | string[]): string[] {
  const choices = Array.isArray(options) ? options : options?.options;
  return Array.isArray(choices) ? choices.filter((choice): choice is string => typeof choice === "string") : [];
}

export function toggleIdeaChoice(selected: string[], option: string, mode: SelectionMode): string[] {
  if (mode === "single") return [option];
  return selected.includes(option) ? selected.filter((value) => value !== option) : [...selected, option];
}

export function composeIdeaAnswer(selected: string[], freeText: string, assumed = true): string {
  const choice = selected.join(", ");
  return [assumed && choice ? `Assumed: ${choice}` : choice, freeText.trim()].filter(Boolean).join(assumed ? "\n" : ". ");
}

/** Restore the choices from a saved answer without guessing at free-text content. */
export function savedIdeaChoiceAnswer(reply: string, options: string[]): { selected: string[]; freeText: string } {
  const assumed = reply.startsWith("Assumed: ");
  if (assumed) reply = reply.slice("Assumed: ".length);
  // At most four options are offered. Match the exact composed prefix, including
  // choice order; an option may itself contain commas or punctuation.
  const visit = (selected: string[], remaining: string[]): { selected: string[]; freeText: string } | null => {
    if (selected.length) {
      const prefix = selected.join(", ");
      if (reply === prefix) return { selected, freeText: "" };
      if (assumed && reply.startsWith(`${prefix}\n`)) return { selected, freeText: reply.slice(prefix.length + 1) };
      if (reply.startsWith(`${prefix}. `)) return { selected, freeText: reply.slice(prefix.length + 2) };
    }
    for (const option of remaining) {
      const found = visit([...selected, option], remaining.filter((value) => value !== option));
      if (found) return found;
    }
    return null;
  };
  return visit([], options.slice(0, 4)) || { selected: [], freeText: reply };
}

export function currentQuestionAnswer(reply: string, questionId: string, structured?: Record<string, unknown>): string {
  const corrections = structured?._answer_corrections;
  if (!corrections || typeof corrections !== "object" || Array.isArray(corrections)) return reply;
  const corrected = (corrections as Record<string, unknown>)[questionId];
  return typeof corrected === "string" && corrected.trim() ? corrected : reply;
}

/** Typed text that is a new instruction ("skip these", "find me people"), not an answer to the open question. */
export function leavesIdeaQuestion(text: string): boolean {
  return /^\s*(?:(?:ok(?:ay)?|please|just|now|so|can you|could you)[,\s]+)*(?:skip\b|(?:find|get|show)\s+(?:me\s+|us\s+)?(?:some\s+|more\s+|my\s+|our\s+)?(?:first\s+)?(?:people|customers|users|leads|prospects)\b)/i.test(text);
}

export function canSendIdeaAnswer(text: string, blocked: boolean): boolean {
  const length = text.trim().length;
  return !blocked && length > 0 && length <= IDEA_ANSWER_MAX_CHARS;
}

export function openPreviousAnswerReview(details: HTMLDetailsElement | null): void {
  if (!details) return;
  details.open = true;
  const summary = details.querySelector("summary");
  summary?.scrollIntoView({ behavior: "smooth", block: "start" });
  summary?.focus({ preventScroll: true });
}

type AnswerTurn = { role: string; kind: string; text: string; options?: { field?: string } | string[] };

export function previousIdeaAnswers<T extends AnswerTurn>(turns: T[]): { question: T; reply: T; field?: string }[] {
  return turns.flatMap((turn, index) => {
    if (turn.role !== "agent" || turn.kind !== "question") return [];
    const reply = turns[index + 1];
    if (reply?.role !== "founder") return [];
    const field = turn.options && !Array.isArray(turn.options) ? turn.options.field : undefined;
    return [{ question: turn, reply, field }];
  });
}

/** Legacy callers keep the full answer visible, even with saved short labels. */
export function shortOptionLabel(option: string, _labels?: string[], _index?: number): string {
  return option;
}

/** Per-option labels saved with a question (same order as its options), or [] for older questions. */
export function savedIdeaOptionLabels(options?: { option_labels?: string[] } | string[]): string[] {
  const labels = Array.isArray(options) ? undefined : options?.option_labels;
  return Array.isArray(labels) ? labels.filter((l): l is string => typeof l === "string") : [];
}
