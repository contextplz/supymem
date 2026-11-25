import { api, verifiedSessionEpoch, verifiedSessionSubject, type ApiResult } from "./client.ts";

export type TaskKind = "one_time" | "scheduled" | "event";
export type TaskView = {
  id: string; title: string; goal: string; kind: TaskKind; state: string; updated_at: string;
  deadline_at: string | null; deadline_is_estimate: true; next_fire_at: string | null;
  blocker: { class: string; reason: string; unblock_action: string } | null;
  steps: Array<{ id: string; title: string; state: string; deadline_at: string | null }>;
  events: Array<{ id: string; kind: string; message: string; created_at: string }>;
  action?: { label: string; href: string };
  can_edit?: boolean;
  can_continue?: boolean;
  can_resume?: boolean;
  research_recovery?: { revision_id: string; job_id: string | null; saved_href?: string } | null;
};
export type TaskContract = {
  title: string; goal: string; kind: TaskKind; estimate_seconds: number;
  trigger: Record<string, unknown>; done_test: { type: string; spec?: unknown };
  steps: Array<{ title: string; skill: string; runs_on: string }>;
  [key: string]: unknown;
};
export type TaskAction = "cancel" | "pause" | "resume" | "continue";
export type TaskPreferences = { timezone: string; confirmed: boolean };
export type TaskResult = { title: string; body_md: string; status: "verified" | "partial" | "pending" | "unconfirmed" | "withheld";
  partial: boolean; artifacts: Array<{ label: string; href: string }> };
export const browserTimezone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
export const taskCanEdit = (task: TaskView) => task.kind !== "one_time" && task.can_edit !== false && !!task.updated_at
  && ["submitted", "paused", "canceled", "completed", "failed", "rejected"].includes(task.state)
  && task.steps.every(step => ["completed", "failed", "canceled", "rejected"].includes(step.state));
export const taskIsActive = (task: TaskView) => !["completed", "failed", "canceled", "rejected"].includes(task.state);
const states: Record<string, string> = {
  submitted: "Waiting to start", working: "Working", input_required: "Needs your input",
  auth_required: "Needs a connection", approval_required: "Needs your approval", paused: "Paused",
  verifying: "Checking the result", completed: "Completed", failed: "Needs attention", canceled: "Stopped", rejected: "Cannot continue",
};
export const taskStateLabel = (state: string) => states[state] || "Checking saved progress";
export function estimatedDeadline(value: string | null | undefined, timezone?: string): string {
  const date = value ? new Date(value) : null;
  if (!date || !Number.isFinite(date.getTime())) return "being checked";
  const options: Intl.DateTimeFormatOptions = { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit", timeZoneName: "short", ...(timezone ? { timeZone: timezone } : {}) };
  try { return `by ${date.toLocaleString(undefined, options)}`; }
  catch { return `by ${date.toLocaleString(undefined, { ...options, timeZone: "UTC" })} (UTC; saved timezone unavailable)`; }
}
export function taskResultHref(value: string, ventureId: string): string | null {
  const same = taskActionHref(value, ventureId);
  if (same) return same;
  try {
    const url = new URL(value, "https://app.supymem.com");
    return value.startsWith("/gtm?") && url.origin === "https://app.supymem.com" && url.pathname === "/gtm"
      && !url.hash && url.searchParams.getAll("venture").length === 1 && url.searchParams.get("venture") === ventureId
      && [...url.searchParams.keys()].every(key => key === "venture") ? value : null;
  } catch { return null; }
}
export function taskActionHref(value: string | undefined, ventureId: string): string | null {
  if (!value?.startsWith("/") || value.startsWith("//")) return null;
  try {
    const url = new URL(value, "https://app.supymem.com");
    const root = `/v/${encodeURIComponent(ventureId)}`;
    return url.origin === "https://app.supymem.com" && (url.pathname === root || url.pathname.startsWith(root + "/")) ? value : null;
  } catch { return null; }
}
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" ? value as Record<string, unknown> : {};
function checkedTask(value: unknown): TaskView {
  const row = value as TaskView;
  if (!row || typeof row.id !== "string" || !row.id || typeof row.title !== "string" || typeof row.goal !== "string"
    || !["one_time", "scheduled", "event"].includes(row.kind) || typeof row.state !== "string" || typeof row.updated_at !== "string"
    || row.deadline_is_estimate !== true || !Array.isArray(row.steps) || !Array.isArray(row.events)
    || (row.blocker != null && (typeof row.blocker.reason !== "string" || typeof row.blocker.unblock_action !== "string"))
    || (row.action != null && (typeof row.action.label !== "string" || typeof row.action.href !== "string"))
    || !row.steps.every(step => step && typeof step.id === "string" && typeof step.title === "string" && typeof step.state === "string")
    || !row.events.every(event => event && typeof event.id === "string" && typeof event.message === "string" && typeof event.created_at === "string")) {
    throw new Error("Saved task details could not be confirmed. Refresh to check your work.");
  }
  if (row.research_recovery != null && (typeof row.research_recovery !== "object"
      || typeof row.research_recovery.revision_id !== "string" || !row.research_recovery.revision_id
      || !(row.research_recovery.job_id === null || typeof row.research_recovery.job_id === "string")
      || (row.research_recovery.saved_href !== undefined && typeof row.research_recovery.saved_href !== "string")))
    throw new Error("The saved research request could not be confirmed. Refresh saved tasks.");
  return row;
}
function checkedContract(value: unknown): TaskContract {
  const row = value as TaskContract;
  if (!row || typeof row.title !== "string" || !row.title || typeof row.goal !== "string" || !row.goal
    || !["one_time", "scheduled", "event"].includes(row.kind) || !Number.isFinite(row.estimate_seconds)
    || row.estimate_seconds < 60 || row.estimate_seconds > 86400 || !Array.isArray(row.steps) || row.steps.length === 0
    || !row.steps.every(step => step && typeof step.title === "string" && typeof step.skill === "string")) {
    throw new Error("The task plan could not be confirmed. Edit your request and preview it again.");
  }
  return row;
}
export function createTaskClient(transport: (path: string, init?: RequestInit) => Promise<ApiResult> = api) {
  async function request(ventureId: string, suffix = "", body?: unknown, key?: string, method = "POST", resource = "handoffs"): Promise<unknown> {
    if (!ventureId) throw new Error("Choose a venture before handing off a task.");
    const result = await transport(`/api/ventures/${encodeURIComponent(ventureId)}/${resource}${suffix}`, body === undefined ? undefined : {
      method, body: JSON.stringify(body), ...(key ? { headers: { "Idempotency-Key": key } } : {}),
    });
    if (!result.ok) {
      const detail = object(result.data).detail;
      const clarifications: Record<string, string> = {
        "What exact frequency or event should start this task?": "What exact frequency or event should start this task?",
        "Choose a valid timezone": "The task's timezone could not be confirmed. Reopen the app and preview it again.",
        "Choose a valid timezone.": "Choose a valid timezone, such as Asia/Kolkata or America/New_York.",
        "Choose a valid hour": "What time should this task run? Use an hour from 1 to 12 with am or pm.",
        "Choose a valid time": "What time should this task run? Include a valid hour and minute.",
        "This plan needs a skill that is not available yet": "This task needs a capability that is not available yet. Edit your request to use a supported workflow.",
      };
      throw new Error(result.status === 401 ? "Sign in again to review your saved tasks."
        : result.status === 403 ? "The founder manages these task preferences and handoffs."
        : result.status === 409 ? "The saved task changed. Refresh before trying again."
          : result.status === 422 ? (typeof detail === "string" && clarifications[detail]) || "This request needs a clearer task or schedule. Edit it and preview again."
          : "This action could not be confirmed. Refresh saved tasks before trying again.");
    }
    return result.data;
  }
  function preferences(value: unknown): TaskPreferences {
    const row = value as TaskPreferences;
    if (!row || typeof row.timezone !== "string" || !row.timezone || typeof row.confirmed !== "boolean") throw new Error("Your task timezone could not be confirmed. Refresh saved preferences.");
    return row;
  }
  return {
    rerunResearch: async (ventureId: string, task: TaskView, key: string): Promise<string> => {
      const recovery = task.research_recovery;
      if (!recovery?.revision_id || !key) throw new Error("Refresh saved research before starting a new run.");
      const data = object(await request(ventureId, "", { expected_revision_id: recovery.revision_id, request_key: key },
        undefined, "POST", "research/dossier/regenerate"));
      if (typeof data.job_id !== "string" || !data.job_id || data.job_id === recovery.job_id)
        throw new Error("Research is still finishing. Refresh saved status before starting a new run.");
      return data.job_id;
    },
    list: async (ventureId: string): Promise<TaskView[]> => {
      const data = object(await request(ventureId));
      if (!Array.isArray(data.items)) throw new Error("Saved tasks could not be confirmed. Refresh to check your work.");
      return data.items.map(checkedTask);
    },
    preview: async (ventureId: string, goal: string, timezone: string) => checkedContract(await request(ventureId, "/preview", { goal, timezone })),
    create: async (ventureId: string, contract: TaskContract, timezone: string, key: string) => checkedTask(await request(ventureId, "", { goal: contract.goal, timezone, contract, start: true }, key)),
    act: async (ventureId: string, taskId: string, action: TaskAction, expectedUpdatedAt?: string, key?: string) => {
      if (action === "continue" && (!expectedUpdatedAt || !key)) throw new Error("Refresh saved progress before choosing Continue.");
      return checkedTask(await request(ventureId, `/${encodeURIComponent(taskId)}/${action === "continue" ? "resume" : action}`,
        action === "continue" ? { continue_budget: true, expected_updated_at: expectedUpdatedAt } : {}, key));
    },
    edit: async (ventureId: string, task: TaskView, contract: TaskContract, timezone: string) => {
      if (!taskCanEdit(task)) throw new Error("Finish or stop the current run before editing future runs.");
      return checkedTask(await request(ventureId, `/${encodeURIComponent(task.id)}`, { goal: contract.goal, timezone, contract, expected_updated_at: task.updated_at }, undefined, "PATCH"));
    },
    result: async (ventureId: string, taskId: string): Promise<TaskResult> => {
      const row = await request(ventureId, `/${encodeURIComponent(taskId)}/result`) as TaskResult;
      if (!row || typeof row.title !== "string" || typeof row.body_md !== "string" || new TextEncoder().encode(row.body_md).length > 16_384
        || !["verified", "partial", "pending", "unconfirmed", "withheld"].includes(row.status) || typeof row.partial !== "boolean"
        || (row.status === "verified") === row.partial
        || !Array.isArray(row.artifacts) || !row.artifacts.every(link => link && typeof link.label === "string" && typeof link.href === "string")) {
        throw new Error("The saved result could not be confirmed. Refresh the task and review its progress.");
      }
      return { ...row, artifacts: row.artifacts.filter(link => taskResultHref(link.href, ventureId)) };
    },
    preferences: async (ventureId: string) => preferences(await request(ventureId, "", undefined, undefined, "GET", "task-preferences")),
    savePreferences: async (ventureId: string, timezone: string, only_if_unconfirmed = false) => preferences(await request(ventureId, "", { timezone, only_if_unconfirmed }, undefined, "PATCH", "task-preferences")),
  };
}
export const taskClient = createTaskClient();

/** AppShell/sign-in seam: call after verifying the founder and selected venture. */
export async function captureFounderTimezone(ventureId: string, client = taskClient): Promise<TaskPreferences> {
  const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
  const current = () => !!subject && subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
  if (!current()) throw new Error("Sign in to confirm your task timezone.");
  const saved = await client.preferences(ventureId);
  if (!current()) throw new Error("The account changed. Refresh saved preferences.");
  const result = saved.confirmed ? saved : await client.savePreferences(ventureId, browserTimezone(), true);
  if (!current()) throw new Error("The account changed. Refresh saved preferences.");
  return result;
}
