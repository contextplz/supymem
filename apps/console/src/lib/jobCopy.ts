import { jobFailure } from "./durableJobs.ts";
import { errorCodeCopy } from "./errorCopy.ts";

type JobCopyInput = { kind?: string | null; status?: string | null; state?: string | null; error_code?: string | null; last_error?: string | null };
const KINDS: Record<string, string> = {
  "research.deep": "Research", "idea.validate": "Validation", "plan.compile": "Planning", "idea.clarify_turn": "Clarification",
};
const STATUSES: Record<string, string> = {
  scheduled: "Queued", pending: "Queued", queued: "Queued", leased: "Working", running: "Working", in_progress: "Working",
  retry_wait: "Retrying in a moment", succeeded: "Complete", done: "Complete", completed: "Complete", created: "Created",
  failed_dead: "Needs attention", failed: "Needs attention", poison: "Needs attention", blocked: "Needs attention",
  cancelled: "Cancelled", paused: "Paused", active: "Active", draft: "Draft", pending_approval: "Awaiting approval",
  approved: "Approved", rejected: "Declined", revoked: "Disconnected",
};

/** Saved work gets a plain status and one useful recovery sentence, never an internal identifier. */
export function plainJob(job: JobCopyInput): { title: string; body: string } {
  const code = (job.error_code || job.last_error || "").split(":")[0].trim();
  const savedStatus = STATUSES[(job.status || job.state || "").toLowerCase()];
  const status = savedStatus || (code ? "Needs attention" : "Saved");
  const kind = KINDS[job.kind || ""];
  if (status !== "Needs attention") return { title: kind ? `${kind} · ${status}` : status, body: "" };
  const fallback = jobFailure({ id: "", kind: job.kind || "", status: job.status || "failed" });
  const failure = jobFailure({ id: "", kind: job.kind || "", status: job.status || "failed", error_code: code });
  return { title: kind ? `${kind} · ${status}` : status,
    body: failure !== fallback ? failure : errorCodeCopy(code) || fallback };
}
