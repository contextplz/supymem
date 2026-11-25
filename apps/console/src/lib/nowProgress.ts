export type BackgroundWork = {
  job_id: string; stage: string; activity: string; state: "queued" | "working";
  owner_name?: string | null; owner_role?: string | null; changed_at?: string | null;
};

/** A queued plan task cannot replace a current background-job receipt. */
export function currentBackgroundWork(value: unknown, venture: string): BackgroundWork[] | null {
  if (!value || typeof value !== "object") return null;
  const data = value as { venture?: { id?: string }; run_status?: { background_work?: unknown } };
  if (data.venture?.id !== venture || !Array.isArray(data.run_status?.background_work)) return null;
  const jobs = data.run_status.background_work;
  if (jobs.some(job => !job || typeof job !== "object" || typeof job.job_id !== "string"
    || typeof job.stage !== "string" || typeof job.activity !== "string"
    || !["queued", "working"].includes(job.state))) return null;
  return jobs;
}

/** A failed, cancelled or paused run is shown as saved, never as "nothing is happening". */
export function stoppedRun(value: unknown): { state: string; stage: string; activity: string; action?: { label: string; href: string } } | null {
  const run = (value as { run_status?: { state?: unknown; stage?: unknown; activity?: unknown; action?: { label?: unknown; href?: unknown } | null } } | null)?.run_status;
  if (!run || !["failed", "paused", "cancelled"].includes(String(run.state))) return null;
  return { state: String(run.state), stage: String(run.stage || "Saved venture"), activity: String(run.activity || ""),
    ...(typeof run.action?.label === "string" && typeof run.action.href === "string" && run.action.href.startsWith("/") && !run.action.href.startsWith("//")
      ? { action: { label: run.action.label, href: run.action.href } } : {}) };
}
