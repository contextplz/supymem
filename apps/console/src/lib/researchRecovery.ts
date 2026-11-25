import { api } from "./client.ts";
import { restoreActiveJob, type DurableJob } from "./durableJobs.ts";

type Result = { ok: boolean; data: unknown };
export type ResearchRecovery = { state: string; revisionId: string | null; personal: boolean;
  jobId: string | null; failure?: DurableJob; error: string };
type Options = { read?: (path: string) => Promise<Result>; restore?: typeof restoreActiveJob; current?: () => boolean };
export const unknownResearchRecovery = (): ResearchRecovery => ({ state: "unknown", revisionId: null,
  personal: false, jobId: null, error: "" });

/** Read saved stage and current-revision receipts only. Never creates a session or starts research. */
export async function loadResearchRecovery(ventureId: string, workspaceId: string | undefined, options: Options = {}): Promise<ResearchRecovery> {
  const read = options.read || api, restore = options.restore || restoreActiveJob;
  const current = options.current || (() => true);
  const unknown = unknownResearchRecovery();
  const failed = () => ({ ...unknown, error: "The saved idea and research status could not be checked. Refresh before starting another run." });
  if (!workspaceId) return failed();
  const listed = await read(`/api/ventures/${encodeURIComponent(ventureId)}/idea/sessions`);
  if (!current()) return unknown;
  const items = (listed.data as { items?: { id?: unknown; state?: unknown; project_intent?: unknown }[] } | null)?.items;
  if (!listed.ok || !Array.isArray(items)) return failed();
  const latest = items.find(item => item && item.state !== "abandoned");
  if (!latest) return { ...unknown, state: "missing" };
  if (typeof latest.id !== "string" || !latest.id) return failed();
  const detail = await read(`/api/ventures/${encodeURIComponent(ventureId)}/idea/sessions/${encodeURIComponent(latest.id)}`);
  if (!current()) return unknown;
  const idea = detail.data as { id?: unknown; venture_id?: unknown; workspace_id?: unknown; state?: unknown;
    current_revision?: { id?: unknown; structured?: { project_intent?: unknown } } | null } | null;
  if (!detail.ok || idea?.id !== latest.id || idea.venture_id !== ventureId || idea.workspace_id !== workspaceId
      || typeof idea.state !== "string") return failed();
  const revisionId = typeof idea.current_revision?.id === "string" ? idea.current_revision.id : null;
  const personal = latest.project_intent === "personal" || idea.current_revision?.structured?.project_intent === "personal";
  if (idea.state === "confirmed" && !revisionId) return failed();
  const result = { ...unknown, state: idea.state, revisionId, personal };
  if (!revisionId || personal) return result;
  const kinds = idea.state === "confirmed" ? ["research.deep"] : ["idea.validate"];
  const restored = await restore(ventureId, kinds, workspaceId, true, undefined, revisionId);
  if (!current()) return unknown;
  const failure = restored.lastFailure;
  const scopedFailure = failure?.venture_id === ventureId && failure.revision_id === revisionId
    && kinds.includes(failure.kind) ? failure : undefined;
  return { ...result, jobId: restored.id, error: restored.error, failure: restored.id ? undefined : scopedFailure };
}

export function researchEmptyMessage(state: ResearchRecovery): string {
  if (state.error || state.state === "unknown") return "Research status is unavailable. Check saved status before starting another run.";
  if (state.failure) return "Research did not finish. Your saved sources are kept. Run research again, or skip for now.";
  if (state.jobId) return "A saved job is queued or running for this revision. No report has been saved yet; checking status does not restart it.";
  if (state.personal) return "This personal project follows the reviewed local-build route. Market research has not been started.";
  if (state.state === "confirmed") return "Your current idea is confirmed. No deep research report is saved yet. Review the request before starting. Research uses credits. We set them aside when it starts and nothing is published.";
  return "Clarify and confirm the current idea before deep research. Review Idea for its saved validation and next action.";
}

/** A reviewed request must still address the same confirmed revision after fresh reads. */
export function researchStartDecision(reviewedRevision: string, saved: ResearchRecovery): "observe" | "review" | "start" {
  if (saved.jobId && !saved.error) return "observe";
  if (saved.error || saved.state !== "confirmed" || saved.personal || !saved.revisionId || saved.revisionId !== reviewedRevision) return "review";
  return "start";
}

export type ResearchReceiptScope = { id: string; venture: string; revision: string; owner: string | null };
export function researchReceiptMatches(scope: ResearchReceiptScope | null, finished: DurableJob,
  venture: string, owner: string | null): boolean {
  return !!scope && scope.id === finished.id && scope.venture === venture && scope.owner === owner
    && finished.venture_id === venture && scope.revision === finished.revision_id
    && ["research.deep", "idea.validate"].includes(finished.kind);
}
