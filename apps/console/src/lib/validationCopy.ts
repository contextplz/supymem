export const validationHeadings: Record<string, string> = {
  gstack_office_hours: "Founder questions",
  gstack_ceo_review: "Product risks",
  gbrain_think: "Second opinion",
};

export const GSTACK_HEADING = "Let's pressure-test your idea";
export const GSTACK_ABOUT =
  "I'll use your saved answers to find promising directions and suggest what you can try next. I haven't checked these ideas with customers yet.";
export const GSTACK_SKIP = "Skip, run the review with what you have";

export function founderReviewText(text: string): string {
  return text.replace(/GStack looks for:\s*/gi, "Strong evidence looks like: ")
    .replace(/GStack looks for\s+/gi, "Strong evidence looks like: ")
    .replace(/ with GStack\b/gi, "")
    .replace(/\bGStack review\b/gi, "idea review")
    .replace(/GStack is Garry Tan's open-source founder toolkit[^\n]*/gi, GSTACK_ABOUT)
    .replace(/\bQ\d+\s+/g, "")
    .replace(/\bDemand Reality\b/gi, "Customer demand")
    .replace(/\bStatus Quo\b/gi, "Current workarounds")
    .replace(/\bDesperate Specificity\b/gi, "Who needs this most")
    .replace(/\bNarrowest Wedge\b/gi, "Smallest useful version")
    .replace(/\bFuture-Fit\b/gi, "Future needs");
}

export const validationMethods = ["gstack_office_hours", "gstack_ceo_review", "gbrain_think"];

export function latestValidationRuns<Run extends { method: string }>(runs: Run[]): Run[] {
  const byMethod = new Map<string, Run>();
  for (const run of runs) byMethod.set(run.method, run);
  return validationMethods.map((method) => byMethod.get(method)).filter((run): run is Run => Boolean(run));
}

export type ValidatedIdea = {
  id: string; state: string; project_intent?: string;
  current_revision?: { id?: string } | null;
  validation_runs?: { method: string; revision_id?: string; state: string }[];
};

export function canConfirmValidatedIdea(idea: ValidatedIdea): boolean {
  return idea.project_intent !== "personal" && ["reviewing", "validating"].includes(idea.state)
    && Boolean(idea.current_revision?.id) && latestValidationRuns((idea.validation_runs || [])
      .filter(run => run.revision_id === idea.current_revision?.id))
      .filter(run => run.state === "complete").length === validationMethods.length;
}

/** One GStack forcing question, structured by the review (absent on older validation rows). */
export type ForcingQuestion = {
  question_id: string; question: string; founder_answer?: string;
  assessment: string; risk: string; suggested_test: string;
};

const NO_MEMORY = /no scoped .*memory/i;
const JARGON_LEAD = /^\s*prime directives applied to [^:]*:\s*/i;

/** Founder-facing text for one method's findings; empty when there is nothing to show. */
export function cleanAssessment(text: string): string {
  const t = text.trim();
  return NO_MEMORY.test(t) ? "" : founderReviewText(t.replace(JARGON_LEAD, ""));
}

const GAP_CODES: Record<string, string> = {
  no_customer_conversation: "No customer conversation recorded yet.",
  no_memory_gap: "No saved research or notes were connected, so this review rests on your own answers only.",
  no_memory: "No saved research or notes were connected, so this review rests on your own answers only.",
  missing_memory_gap: "No saved research or notes were connected, so this review rests on your own answers only.",
};

/** Gaps safe to show a founder: drops the internal "No scoped GBrain memory was provided." note and never shows a raw snake_case code. */
export const founderGaps = (gaps: string[] | undefined): string[] => (gaps || []).map((gap) => {
  const code = gap.trim().toLowerCase();
  if (!/^[a-z0-9]+(_[a-z0-9]+)+$/.test(code)) return founderReviewText(gap);
  return GAP_CODES[code] ?? founderReviewText(code.replace(/_gap$/, "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) + ".");
}).filter((gap) => !NO_MEMORY.test(gap) && !/gbrain/i.test(gap));

/** Structured forcing-question items, or [] for rows saved before they existed. */
export function forcingQuestions(report: { forcing_questions?: ForcingQuestion[] } | undefined): ForcingQuestion[] {
  return (report?.forcing_questions || []).filter((item) => item && item.question && item.assessment);
}

/** Legacy fallback: splits "Q1 Label: text Q2 Label: text" into one item per question; a single item when not Q-shaped. */
export function splitQuestions(text: string): { label: string; body: string }[] {
  const parts = text.split(/(?=\bQ\d+ )/).map((p) => p.trim()).filter(Boolean);
  return parts.map((part) => {
    const m = part.match(/^(Q\d+ [^:]{1,60}:)\s*([\s\S]*)$/);
    return m ? { label: founderReviewText(m[1]), body: founderReviewText(m[2]) } : { label: "", body: founderReviewText(part) };
  });
}
