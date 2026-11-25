/** An editorial fallback from saved founder answers, never a model or market finding. */
export type FounderExperimentContext = { problem: string; customer: string; offer: string };

function answer(value: unknown): string {
  return typeof value === "string" ? value.trim().slice(0, 240) : "";
}

export function founderExperimentContext(structured?: Record<string, unknown> | null): FounderExperimentContext | null {
  if (!structured) return null;
  const problem = answer(structured.problem);
  const customer = answer(structured.customer);
  const offer = answer(structured.offer);
  return problem && customer && offer ? { problem, customer, offer } : null;
}

/** A personal-project review uses saved founder inputs, never an old market verdict. */
export function personalProjectReview(structured?: Record<string, unknown> | null, rawIdea?: string): {
  goal: string; firstVersion: string | null;
} {
  return {
    goal: answer(structured?.desired_outcome) || answer(structured?.problem) || answer(rawIdea) || "Try your idea on one real task of your own.",
    firstVersion: answer(structured?.offer) || answer(structured?.workflow) || null,
  };
}
