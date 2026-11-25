import { api, type ApiResult } from "./client.ts";
import type { DiagnosisInput, DiagnosisIntent, DiagnosisQuote, DiagnosisRun, GtmOwner } from "./gtmDiagnosisIntent.ts";
import type { GtmGodStatus } from "./opsApi.ts";
import { errorCodeCopy } from "./errorCopy.ts";
import { announceDiagnostic } from "./uiDiagnostics.ts";

type Transport = (path: string, init?: RequestInit) => Promise<ApiResult>;
export type GtmVenture = { id: string; name?: string; raw_idea?: string; workspace_id: string };
export type GtmDiagnosisContext = {
  idea: string;
  problem: string;
  who: string;
  sources: Array<"idea" | "charter" | "idea_session">;
};
export class DiagnosisApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) { super(message); this.status = status; this.code = code; }
}
export function rejectedBeforeEnqueue(error: DiagnosisApiError): boolean {
  return [402, 422].includes(error.status) || error.status === 409 && error.code === "gtm_quote_changed";
}
function scopedRun(value: DiagnosisRun, owner: GtmOwner): DiagnosisRun {
  if (!value || value.venture_id !== owner.ventureId || typeof value.run_id !== "string" || !value.run_id
      || value.kind !== "diagnose" || !["queued", "running", "completed", "failed", "needs_input", "cancelled"].includes(value.state)) {
    announceDiagnostic("integrity_error", "integrity", "Integrity failure");
    throw new Error("The diagnosis response did not match this company. Recover the same approved request or refresh its status.");
  }
  return value;
}
function object(value: unknown): Record<string, unknown> { return value && typeof value === "object" ? value as Record<string, unknown> : {}; }
export function founderProductContext(rawIdea: string): string {
  const idea = rawIdea.trim();
  if (!/^existing product:\s*/i.test(idea)) return idea;
  const body = idea.replace(/^existing product:\s*/i, "");
  const withoutAnswers = body.replace(/(?:\.\s*)?(website|app|both|internal)\s*·\s*[^·]+\s*·\s*[^·]+\s*·\s*[^·]+\s*$/i, "").trim().replace(/\.$/, "");
  if (withoutAnswers && !withoutAnswers.includes(" · ")) return withoutAnswers;
  const kind = /^(website|app|both|internal)\b/i.exec(body)?.[1]?.toLowerCase();
  return ({ website: "Existing website", app: "Existing app", both: "Existing app and website", internal: "Existing internal tool" } as Record<string, string>)[kind || ""] || "Existing product";
}
export function createDiagnosisClient(transport: Transport = api) {
  async function request<T>(owner: GtmOwner, suffix: string, body?: unknown, idempotencyKey?: string): Promise<T> {
    if (!owner.actorId || !owner.workspaceId || !owner.ventureId) throw new Error("Choose an authenticated company first.");
    const path = `/api/api/v1/ventures/${encodeURIComponent(owner.ventureId)}/gtm/god${suffix}?workspace_id=${encodeURIComponent(owner.workspaceId)}`;
    const headers: Record<string, string> = { "X-Workspace-ID": owner.workspaceId };
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    const response = await transport(path, { method: body === undefined ? "GET" : "POST", headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    if (!response.ok) {
      const data = object(response.data), detail = object(data.detail), nested = object(detail.error), error = object(data.error);
      const code = [error.code, nested.code, detail.code, typeof data.detail === "string" ? data.detail : null].find(v => typeof v === "string") as string | undefined;
      const message = [error.message, nested.message, detail.message].find(v => typeof v === "string") as string | undefined;
      const explanation: Record<string, string> = {
        gtm_quote_changed: "The quoted input, provider, or budget changed. Review a new quote before approving another request.",
        gtm_insufficient_credits: "The selected company cannot fund this diagnosis now. Review its credits and request a new quote.",
        gtm_idempotency_conflict: "The saved approval key conflicts with the server record. Resolve the existing request before approving another diagnosis.",
      };
      throw new DiagnosisApiError(response.status, code || "gtm_request_unresolved", message || explanation[code || ""] || errorCodeCopy(code) || "The diagnosis request could not be confirmed. Refresh status or recover the same approved request.");
    }
    return response.data as T;
  }
  return {
    quote: (owner: GtmOwner, input: DiagnosisInput) => request<DiagnosisQuote>(owner, "/quotes", input),
    start: async (intent: DiagnosisIntent) => scopedRun(await request<DiagnosisRun>(intent.owner, "/runs", intent.request, intent.idempotencyKey), intent.owner),
    async runs(owner: GtmOwner) {
      const value = await request<{ runs: DiagnosisRun[] }>(owner, "/runs");
      if (!Array.isArray(value?.runs)) {
        announceDiagnostic("integrity_error", "integrity", "Integrity failure");
        throw new Error("Saved diagnosis status could not be verified.");
      }
      return { runs: value.runs.map(run => scopedRun(run, owner)) };
    },
    run: async (owner: GtmOwner, runId: string) => scopedRun(await request<DiagnosisRun>(owner, `/runs/${encodeURIComponent(runId)}`), owner),
    async ventures(workspaceId: string): Promise<GtmVenture[]> {
      const result = await transport("/api/ventures");
      if (!result.ok) throw new Error("Companies could not be loaded. Refresh to reconnect.");
      return ((result.data as { items?: GtmVenture[] })?.items || [])
        .filter(row => row && typeof row.id === "string" && row.workspace_id === workspaceId);
    },
    async context(owner: GtmOwner, venture: GtmVenture): Promise<GtmDiagnosisContext> {
      if (!owner.actorId || !owner.workspaceId || !owner.ventureId || venture.id !== owner.ventureId
          || venture.workspace_id !== owner.workspaceId) throw new Error("Choose an authenticated company first.");
      const idea = typeof venture.raw_idea === "string" ? founderProductContext(venture.raw_idea) : "";
      const result = await transport(`/api/ventures/${encodeURIComponent(owner.ventureId)}/charter`);
      if (!result.ok && result.status !== 404) throw new Error("Saved company context could not be loaded. You can still enter it manually.");
      const content = result.ok ? object(object(result.data).content) : {};
      let problem = typeof content.problem === "string" ? content.problem.trim() : "";
      let who = typeof content.audience === "string" ? content.audience.trim() : "";
      const sources: GtmDiagnosisContext["sources"] = [...(idea ? ["idea" as const] : [])];
      if (problem || who) sources.push("charter");
      if (!problem || !who) {
        const sessions = await transport(`/api/ventures/${encodeURIComponent(owner.ventureId)}/idea/sessions`);
        const first = sessions.ok && Array.isArray(object(sessions.data).items) ? (object(sessions.data).items as unknown[])[0] : null;
        const sessionId = typeof object(first).id === "string" ? String(object(first).id) : "";
        if (sessionId) {
          const session = await transport(`/api/ventures/${encodeURIComponent(owner.ventureId)}/idea/sessions/${encodeURIComponent(sessionId)}`);
          if (!session.ok && session.status !== 404) throw new Error("Saved company context could not be loaded. You can still enter it manually.");
          const structured = session.ok ? object(object(object(session.data).current_revision).structured) : {};
          const sessionProblem = typeof structured.problem === "string" ? structured.problem.trim() : "";
          const sessionWho = [structured.customer, structured.audience, structured.target_user, structured.first_customer]
            .find(value => typeof value === "string" && value.trim());
          problem ||= sessionProblem;
          who ||= typeof sessionWho === "string" ? sessionWho.trim() : "";
          if ((sessionProblem || sessionWho) && !sources.includes("idea_session")) sources.push("idea_session");
        }
      }
      return { idea, problem, who, sources };
    },
    async legacy(workspaceId: string): Promise<GtmGodStatus> {
      const result = await transport(`/api/api/v1/gtm/god?workspace_id=${encodeURIComponent(workspaceId)}`,
        { headers: { "X-Workspace-ID": workspaceId } });
      if (!result.ok) throw new Error("Historical GTM records could not be loaded.");
      return result.data as GtmGodStatus;
    },
  };
}
export const diagnosisClient = createDiagnosisClient();
