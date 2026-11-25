export type PlanObjective = {
  exclusions?: readonly string[] | null;
};

function excludesPaymentGoal(item: string): boolean {
  return /\bno payments?\b(?=\s*[,.;:]|\s*$)/i.test(item)
    || /\bpayments?\s+(?:are\s+|is\s+)?(?:excluded|out of scope|deferred)\b/i.test(item)
    || /\b(?:exclude|excluding|without)\s+payments?\b(?=\s*[,.;:]|\s*$)/i.test(item)
    || /\b(?:no|without)\s+(?:first\s+)?paid\s+(?:users?|customers?)\b/i.test(item);
}

export function isActivationObjective(proposal: PlanObjective | null | undefined): boolean {
  return proposal?.exclusions?.some(excludesPaymentGoal) ?? false;
}

export function objectiveLabel(proposal: PlanObjective | null | undefined): string {
  return isActivationObjective(proposal) ? "Activation objective" : "First paid user objective";
}

export function objectivePageCopy(proposal: PlanObjective | null | undefined) {
  if (!proposal) {
    return {
      subtitle: "Execution milestones and resources",
      heading: "Execution path",
    };
  }
  return isActivationObjective(proposal)
    ? {
        subtitle: "Milestones and resources for user activation",
        heading: "Execution path to user activation",
      }
    : {
        subtitle: "Milestones and resources for your first paid user",
        heading: "Execution path to your first paid user",
      };
}
