export type ExecutionNextActionInput = {
  ventureId: string;
  ready: boolean;
  busy: boolean;
  jobId: string | null;
  error: string;
  syncError: string;
  resourceDirty: boolean;
  resourcesComplete: boolean;
  quoteReady: boolean;
  hasProposal: boolean;
  staleDependencies: boolean;
  findingCount: number;
  planState: string | null;
  planReviewable: boolean;
  generationState: string | null;
  newerPlanId?: string | null;
  editing: boolean;
  nextTask?: {
    id?: string;
    title: string;
    capabilityId: string;
    implementation: string;
    scope?: string;
    reason?: string;
  } | null;
};

export function executionNextAction(input: ExecutionNextActionInput) {
  if (!input.ready) return { label: "Loading saved execution work", href: "#execution-progress", detail: "Checking your saved plan and resources." };
  if (input.syncError || input.error) return { label: "Refresh saved status", href: "#execution-progress", detail: "The current state could not be confirmed. Refresh before making another request." };
  if (input.jobId || input.busy) return { label: "Plan is updating", href: "#execution-progress", detail: "This work continues in the background. Its saved status appears below." };
  if (input.editing) return { label: "Continue editing", href: "#plan-edit", detail: "Save your corrections, then review the updated plan." };
  if (input.resourceDirty) return { label: "Save resource changes", href: "#plan-resources", detail: "Save your resource changes before continuing with the plan." };
  if (input.planState === "approved" || input.planState === "executing") {
    const task = input.nextTask;
    if (task?.scope === "out") return {
      label: "Review blocked step", href: task.id ? `#plan-task-${encodeURIComponent(task.id)}` : "#plan-milestones",
      detail: `${task.title} cannot run as planned. ${task.reason || "Review its unavailable capability and revise the plan before continuing."}`,
    };
    if (task?.capabilityId === "build.run" && task.id) return {
      label: "Review next build", href: `/v/${encodeURIComponent(input.ventureId)}/builds?task=${encodeURIComponent(task.id)}`,
      detail: `${task.title}. Review its saved state and separate build approval.`,
    };
    if (task?.implementation === "founder_action" && task.id) return {
      label: "Review your next step", href: `#plan-task-${encodeURIComponent(task.id)}`,
      detail: `${task.title}. Open the saved instructions and completion evidence below.`,
    };
    if (task) return {
      label: "Open next work item", href: `/v/${encodeURIComponent(input.ventureId)}/tasks`,
      detail: `${task.title}. Review the saved work item and its available action.`,
    };
    return { label: "Review plan progress", href: "#plan-milestones", detail: "No unfinished plan task is available. Review the saved milestones and their evidence." };
  }
  if (input.newerPlanId) return { label: "Review the latest plan", href: "#plan-review", detail: "A newer saved plan is available. Open it before deciding on this version.", intent: "latest" as const };
  if (!input.resourcesComplete) return { label: "Complete resources", href: "#plan-resources", detail: "Save the resources this plan can use before estimating or approving it." };
  if (input.quoteReady) return { label: "Review the estimate", href: "#plan-estimate", detail: "Check cost and scope before choosing whether to generate." };
  if (!input.hasProposal || input.staleDependencies || !input.planReviewable || input.generationState !== "completed") return { label: "Estimate your plan", href: "#plan-create", detail: "Review the estimate before starting plan generation." };
  if (input.findingCount) return { label: "Edit plan manually", href: "#plan-findings", detail: "Correct the saved findings in this plan, then review it before approval.", intent: "edit" as const };
  return { label: "Review and approve", href: "#plan-review", detail: "Review the milestones, then approve only when the plan is ready." };
}
