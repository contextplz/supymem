import { apiLive } from "./client.ts";
import { apiFailure } from "./apiFailure.ts";
import { announceJobDiagnostic } from "./uiDiagnostics.ts";
import { pendingJourneyQuestion } from "./journeyQuestion.ts";

export type ResearchProgress = { state: string; percent: number | null; step_label: string; started_at: string | null; run_id?: string | null };
export type DurableJob = { id: string; status: string; kind: string; venture_id?: string; workspace_id?: string | null; revision_id?: string | null; error_code?: string | null; failure_reason?: string | null; progress?: ResearchProgress | null;
  wait?: { reason: string; label: string; until: string | null } | null;
  created_at?: string | null; scheduled_for?: string | null; started_at?: string | null; locked_at?: string | null };
const knownJobStatuses = new Set(["scheduled", "pending", "leased", "running", "retry_wait", "succeeded", "done", "failed_dead", "failed", "poison", "cancelled"]);
/** A successful HTTP status does not guarantee that a job payload is usable. */
export function isDurableJob(value: unknown): value is DurableJob {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const job = value as Record<string, unknown>;
  return typeof job.id === "string" && job.id.length > 0
    && typeof job.kind === "string" && job.kind.length > 0
    && typeof job.status === "string" && knownJobStatuses.has(job.status);
}
export const jobSucceeded = (job: DurableJob) => ["succeeded", "done"].includes(job.status);
export const jobFinished = (job: DurableJob) => jobSucceeded(job) || ["failed_dead", "failed", "poison", "cancelled"].includes(job.status);

export function jobWaitingLabel(job: DurableJob): string | null {
  if (!["scheduled", "pending", "queued", "retry_wait"].includes(job.status)) return null;
  if (typeof job.wait?.label === "string" && job.wait.label) return job.wait.label;
  return job.status === "retry_wait" ? "Starting soon. Waiting before trying again after a temporary problem."
    : "Starting soon. Your work is saved but has not started yet.";
}

/** A status read must not leave the founder on an unbounded progress screen. */
export function statusObservationStalled(id: string | null, job: DurableJob | null, error: string,
  lastConfirmedAt: number, now: number): boolean {
  return Boolean(id && !error && (!job || !jobFinished(job)) && now - lastConfirmedAt >= 45_000);
}

/** Restore only jobs that still belong to the current Idea route. */
export function ideaJobKindsForSession(session: { state: string; project_intent?: string; turns?: { role: string; kind: string; options?: unknown }[] }): string[] {
  if (session.state === "reviewing" || pendingJourneyQuestion(session.state, session.turns || [])) return [];
  if (session.state === "confirmed") {
    // Older personal projects may have a research run from before the direct
    // build route. Its failure must not replace their local build action.
    return session.project_intent === "personal" ? [] : ["research.deep"];
  }
  return ["idea.clarify_turn", "idea.validate"];
}

export async function restoreIdeaSessionJob(ventureId: string,
  session: { state: string; project_intent?: string; turns?: { role: string; kind: string; options?: unknown }[]; initial_job_id?: string | null; current_revision?: { id?: string } | null },
  workspaceId?: string, onRecoveredFailure?: (job: DurableJob) => void) {
  const kinds = ideaJobKindsForSession(session);
  if (kinds.length === 0) return { id: null, error: "", lastFailure: undefined };
  if (session.initial_job_id) return { id: session.initial_job_id, error: "", lastFailure: undefined };
  // A founder can correct the idea while older work is still leased. Its
  // receipt belongs to the old revision and must not occupy this revision's
  // progress or prevent the founder from reviewing the new one.
  return restoreActiveJob(ventureId, kinds, workspaceId, true, onRecoveredFailure,
    session.current_revision?.id);
}

/** A delayed label changes only the founder's view; it never retries a paid job. */
export function delayedJobNotice(job: DurableJob | null, now = Date.now()): string | null {
  if (!job || jobFinished(job)) return null;
  if (job.wait?.reason === "cancel_requested" || job.wait?.reason === "paused") return null;
  if (job.wait?.until && Date.parse(job.wait.until) > now) return null;
  const timestamp = job.status === "leased" || job.status === "running" ? job.locked_at : job.wait?.until || job.scheduled_for || job.created_at;
  const at = timestamp ? Date.parse(timestamp) : NaN;
  if (!Number.isFinite(at) || now - at < 120_000) return null;
  if (job.status === "leased" || job.status === "running") {
    return "You have not had an update on this work recently. Its saved progress is still being checked. Check progress before you start it again.";
  }
  if (job.status === "scheduled" || job.status === "pending" || job.status === "retry_wait") {
    return "This task has been queued longer than expected. Supymem is still checking its saved status; no new request has been sent.";
  }
  return null;
}

/** A delayed saved job needs a recovery action even while status reads succeed. */
export function jobNeedsRecovery(error: string, stalled: boolean, delayed: boolean): boolean {
  return Boolean(error || stalled || delayed);
}

/** Setup, changed inputs and deliberate cancellation need founder action, not an operator defect. */
export function unexpectedJobFailure(job: DurableJob): boolean {
  if (!["failed_dead", "failed", "poison"].includes(job.status)) return false;
  const code = job.error_code || "";
  // Mirror the API attention-code boundary for job-specific prefixes. These
  // states need the founder's setup, permission or input, not a defect report.
  return !/(?:^|_)(?:missing_key|key_required|credential_required|credentials_missing|api_key_missing|not_connected|reconnect_required|setup_required|permission_required|permission_denied|approval_required|unauthenticated|invalid_credentials|invalid_password|csrf_failed|validation_error|composio_missing|gmail_not_ready|gmail_env|google_ads_env|meta_ads_env|consent_required|email_verification_required|insufficient_credits|out_of_credits|reauth_required|secure_store_pending|keychain_unavailable|bad_api_key|invalid_api_key|credits_unavailable|credit_reservation_unavailable|byok_unavailable|inputs_changed|revision_superseded|confirmation_required|revision_required|research_required|resource_inventory_required|idea_missing|authority_revoked|account_suspended|capability_unavailable|saved_sources_unavailable|call_budget_exhausted|context_limit)$/.test(code);
}

export function reportJobFailure(job: DurableJob): void {
  if (unexpectedJobFailure(job)) announceJobDiagnostic(job.id);
}

export function jobFailure(job: DurableJob): string {
  switch (job.error_code) {
    case "job_outcome_uncertain": return job.kind === "research.deep"
      ? "Research was interrupted. Your saved sources are kept. Run research again, or skip for now."
      : "This task stopped without a confirmed result. Your earlier work is saved. Check its saved progress before you start it again.";
    case "research_confirmation_required": return "Confirm the current idea before deep research. Earlier reports and your answers are saved.";
    case "validation_revision_superseded": return "A newer idea revision replaced the one being reviewed. Validate the current revision to continue.";
    case "research_insufficient_credits": return "You cannot continue research right now. Your findings are saved. Check your account in Settings.";
    case "research_outcome_uncertain":
    case "research_interrupted": return "Research was interrupted. Your saved sources are kept. Run research again, or skip for now.";
    case "research_invalid_output": return "You could not get a usable research review. Your sources are saved. Try again with those sources.";
    case "research_output_truncated": return "The model reached its output limit before finishing the assessment. Saved sources remain available for a new review without repeating searches.";
    case "plan_output_truncated": return "Your plan could not be finished. Review what is saved before you try again.";
    case "research_inputs_changed": return "The saved sources changed. Refresh the report and review its current estimate before continuing.";
    case "research_saved_sources_unavailable": return "There are no usable saved sources for this review. Run research after confirming the current idea.";
    case "research_call_budget_exhausted": return "Research reached its operation budget. Review the evidence and remaining gaps before authorizing another run.";
    case "research_provider_offline": return "Research providers are paused in this environment. Existing reports and founder answers are saved.";
    case "research_byok_unavailable": return "Your selected API connection is unavailable. Check it in Settings before researching further.";
    case "research_credits_unavailable": case "research_credit_reservation_unavailable": return "You cannot start research right now. Your work is saved. Check your account in Settings.";
    case "plan_insufficient_credits": case "plan_credits_unavailable": case "plan_credit_reservation_unavailable": return "You cannot start planning right now. Your work is saved. Check your account in Settings.";
    case "plan_inputs_changed": return "Your idea, research or resources changed during planning. Review the current evidence and request a fresh plan.";
    case "plan_outcome_uncertain": return "Your plan's progress could not be confirmed. Check your saved plan before you start another.";
    case "plan_invalid_output": return "You could not get a usable plan. Your earlier work has not changed. Review it before you try again.";
    case "plan_provider_offline": case "plan_byok_unavailable": case "plan_provider_rejected": return "The planning provider needs attention. Review the AI connection in Settings.";
    case "clarification_insufficient_credits": return "You cannot prepare the next question right now. Your answers are saved. Check your account in Settings.";
    case "clarification_byok_unavailable": return "Your selected API connection is unavailable. Check it in Settings before continuing.";
    case "clarification_account_suspended": case "job_authority_revoked": return "Your account no longer has permission to continue this work. Check company access.";
    case "clarification_outcome_uncertain": return "You could not get a confirmed result. Your answers are saved. Check the saved progress before you start this step again.";
    case "clarification_invalid_output": return "You could not get a usable answer. Your idea has not changed. Try again with your saved answers.";
    case "clarification_provider_bad_api_key": case "clarification_provider_out_of_credits": return "The AI connection needs attention before clarification can continue. Your answers are saved.";
    case "clarification_credits_unavailable": case "clarification_credit_reservation_unavailable": return "You cannot prepare the next question right now. Your idea is saved. Check your account in Settings.";
    case "clarification_provider_provider_error": return "The AI provider rejected this clarification request. Your idea is saved; Supymem did not keep retrying it.";
    case "clarification_provider_offline": return "AI requests are paused in this environment. Your answers are saved; work can continue when the service is enabled.";
    case "clarification_context_limit": return "This conversation is too large for the next clarification step. Shorten the current idea into a new revision.";
    case "capability_unavailable": return "This workflow is not available yet. Your confirmed idea and answers are saved.";
    default: return "This step needs attention. Your saved work is retained. Refresh to review its current state.";
  }
}

export async function restoreActiveJob(ventureId: string, kinds: string[], workspaceId?: string, includeRecentFailure = false,
  onRecoveredFailure?: (job: DurableJob) => void, revisionId?: string): Promise<{ id: string | null; error: string; lastFailure?: DurableJob }> {
  let workspace = workspaceId;
  if (!workspace) {
    const venture = await apiLive(`/api/ventures/${encodeURIComponent(ventureId)}`);
    if (!venture.ok) return { id: null, error: apiFailure(venture, "restoring company work") };
    workspace = (venture.data as { workspace_id?: string }).workspace_id;
  }
  if (!workspace) return { id: null, error: "The company workspace is unavailable. Refresh before starting more work." };
  const query = new URLSearchParams({ venture_id: ventureId, active_only: "true", limit: revisionId ? "50" : "1" });
  kinds.forEach((kind) => query.append("kinds", kind));
  const result = await apiLive(`/api/workspaces/${encodeURIComponent(workspace)}/jobs?${query}`);
  if (!result.ok) return { id: null, error: apiFailure(result, "restoring background work") };
  const items = (result.data as { items?: unknown[] } | null)?.items;
  if (!Array.isArray(items) || items.some((job) => !isDurableJob(job) || job.venture_id !== ventureId || !kinds.includes(job.kind) || jobFinished(job))) {
    return { id: null, error: "Background work could not be synchronized. Refresh before starting more work." };
  }
  const validItems = items as DurableJob[];
  const active = revisionId ? validItems.find((job) => job.revision_id === revisionId) : validItems[0];
  if (active || !includeRecentFailure) return { id: active?.id || null, error: "" };
  query.set("active_only", "false");
  if (revisionId) query.set("limit", "50");
  const recent = await apiLive(`/api/workspaces/${encodeURIComponent(workspace)}/jobs?${query}`);
  const history = (recent.data as { items?: unknown[] } | null)?.items;
  if (!recent.ok) return { id: null, error: apiFailure(recent, "restoring the last workflow result") };
  if (!Array.isArray(history) || history.some((job) => !isDurableJob(job))) {
    return { id: null, error: "The last workflow result could not be read. Check saved status before starting more work." };
  }
  const validHistory = history as DurableJob[];
  const last = revisionId ? validHistory.find((job) => job.revision_id === revisionId) : validHistory[0];
  if (last && (last.venture_id !== ventureId || !kinds.includes(last.kind))) return { id: null, error: "The last workflow result did not match this company. Refresh before continuing." };
  if (last && !jobFinished(last)) return { id: last.id, error: "" };
  if (last && ["failed_dead", "failed", "poison"].includes(last.status)) {
    // A job may finish while the app is closed. The polling hook never sees
    // that transition. The caller checks that this load still owns the screen
    // before attributing the failure to the current venture.
    onRecoveredFailure?.(last);
    return { id: null, error: "", lastFailure: last };
  }
  return { id: null, error: "" };
}
