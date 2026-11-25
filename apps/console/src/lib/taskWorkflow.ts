export type WorkflowTask = {
  tags?: string[];
  source?: string;
};

function taggedValue(tags: string[] | undefined, prefix: string): string {
  const value = (tags || []).find((tag) => tag.startsWith(prefix));
  return value ? value.slice(prefix.length) : "";
}

export function planTaskId(task: WorkflowTask): string {
  if (!task.source?.startsWith("approved_plan:")) return "";
  return taggedValue(task.tags, "plan_task:");
}

export function buildPlanTaskId(task: WorkflowTask): string {
  if (!task.source?.startsWith("approved_plan:")) return "";
  if (!(task.tags || []).includes("capability:build.run")) return "";
  return planTaskId(task);
}

export function buildWorkflowHref(ventureId: string, task: WorkflowTask): string {
  const planTaskId = buildPlanTaskId(task);
  const query = planTaskId ? `?task=${encodeURIComponent(planTaskId)}` : "";
  return `/v/${encodeURIComponent(ventureId)}/builds${query}`;
}
