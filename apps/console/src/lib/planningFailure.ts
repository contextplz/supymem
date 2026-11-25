export type PlanningUsage = { credits_micro: number; has_unsettled: boolean;
  operations?: { error_code?: string | null }[] };

const RECOVERY = " Your answers and research are kept and no executable plan was saved. Incurred model usage remains charged. No automatic retry will run; Retry plan asks again after you review and explicitly approve it.";
const REASONS: Record<string, string> = {
  plan_invalid_output: "The AI's plan failed our checks (for example, a field was longer than allowed).",
  plan_outcome_uncertain: "The AI took too long to finish the plan, so it was stopped.",
};

export function planningFailureExplanation(plan: { generation_state: string; usage?: PlanningUsage } | null): string | null {
  if (plan?.generation_state !== "failed") return null;
  const code = [...(plan.usage?.operations ?? [])].reverse().map(operation => operation.error_code).find(c => !!c && Object.hasOwn(REASONS, c));
  return code ? REASONS[code] + RECOVERY : null;
}
