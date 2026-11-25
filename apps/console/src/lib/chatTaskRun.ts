export function chatTaskRunId(payload: unknown): string | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const value = (payload as Record<string, unknown>).task_id;
  return typeof value === "string" && /^tsk_[a-z0-9]+$/.test(value) ? value : null;
}

export function chatTaskRunPath(ventureId: string, payload: unknown): string | null {
  const taskId = chatTaskRunId(payload);
  if (!taskId) return null;
  return `/api/ventures/${encodeURIComponent(ventureId)}/tasks/${encodeURIComponent(taskId)}/run`;
}
