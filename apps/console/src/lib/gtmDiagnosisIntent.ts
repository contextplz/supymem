/** Durable approval receipts belong to one authenticated actor and company. */
export type GtmOwner = { actorId: string; workspaceId: string; ventureId: string };
export type DiagnosisInput = { kind: "diagnose"; idea: string; problem: string; who: string; users_count: number };
export type DiagnosisRequest = DiagnosisInput & { input_digest: string; resource_digest: string; ceiling_micro: number };
export type DiagnosisIntent = { owner: GtmOwner; idempotencyKey: string; request: DiagnosisRequest };
export type DiagnosisQuote = {
  provider: string; model: string; payer: "founder" | "platform_credits";
  input_digest: string; resource_digest: string; ceiling_micro: number; call_budget: 1;
  max_output_tokens: number; available_micro: number | null; sufficient_now: boolean;
  basis: string; input_provenance: "founder_reported"; operations: string[];
};
export type Diagnosis = { bottleneck: string; rationale: string; assumptions: string[]; evidence_gaps: string[];
  next_actions: { kind: "research" | "human_interview" | "strategy_review" | "find_customers" | "draft_outreach" | "build_page"; instruction: string; reason: string; capability?: string }[] };
export type DiagnosisRun = {
  run_id: string; job_id: string; plan_id: string; venture_id: string; kind: "diagnose";
  state: "queued" | "running" | "completed" | "failed" | "needs_input" | "cancelled";
  phase: string; error_code: string | null; ceiling_micro: number; reserved_micro: number;
  consumed_micro: number; calls_used: number; call_budget: number; payer: string;
  result: { diagnosis?: Diagnosis; input_provenance?: "founder_reported"; scope?: "diagnosis_only"; strategy_execution_supported?: false };
  created_at: string;
};

export function gtmIntentKey(owner: GtmOwner | null): string {
  return owner?.actorId && owner.workspaceId && owner.ventureId
    ? `supymem.gtm.diagnosis:v1:${JSON.stringify([owner.actorId, owner.workspaceId, owner.ventureId])}` : "";
}
export function validDiagnosisInput(value: DiagnosisInput): boolean {
  return value?.kind === "diagnose" && typeof value.idea === "string" && !!value.idea.trim() && value.idea.length <= 12000
    && typeof value.problem === "string" && value.problem.length <= 12000 && typeof value.who === "string" && value.who.length <= 2000
    && Number.isSafeInteger(value.users_count) && value.users_count >= 0 && value.users_count <= 1_000_000_000;
}
export function approveDiagnosis(owner: GtmOwner, input: DiagnosisInput, quote: DiagnosisQuote, idempotencyKey: string): DiagnosisIntent {
  if (!gtmIntentKey(owner) || !validDiagnosisInput(input) || !quote.sufficient_now || quote.call_budget !== 1
      || !Number.isSafeInteger(quote.ceiling_micro) || quote.ceiling_micro < 0
      || !/^[a-f0-9]{64}$/.test(quote.input_digest) || !/^[a-f0-9]{64}$/.test(quote.resource_digest)
      || (quote.payer === "platform_credits" && (!Number.isSafeInteger(quote.available_micro) || quote.available_micro === null || quote.available_micro < quote.ceiling_micro))
      || !["founder", "platform_credits"].includes(quote.payer) || idempotencyKey.length < 8 || idempotencyKey.length > 160) {
    throw new Error("This diagnosis quote cannot be approved. Review a new quote.");
  }
  return { owner: { ...owner }, idempotencyKey, request: { ...input, input_digest: quote.input_digest,
    resource_digest: quote.resource_digest, ceiling_micro: quote.ceiling_micro } };
}
export function saveGtmIntent(intent: DiagnosisIntent): string { return JSON.stringify({ version: 1, ...intent }); }
export function readGtmIntent(raw: string | null, owner: GtmOwner | null): DiagnosisIntent | null {
  if (!raw || !owner) return null;
  try {
    const value = JSON.parse(raw);
    if (value?.version !== 1 || gtmIntentKey(value.owner) !== gtmIntentKey(owner)
        || typeof value.idempotencyKey !== "string" || value.idempotencyKey.length < 8 || value.idempotencyKey.length > 160
        || !validDiagnosisInput(value.request) || !/^[a-f0-9]{64}$/.test(value.request.input_digest)
        || !/^[a-f0-9]{64}$/.test(value.request.resource_digest)
        || !Number.isSafeInteger(value.request.ceiling_micro) || value.request.ceiling_micro < 0) return null;
    return { owner: { ...owner }, idempotencyKey: value.idempotencyKey, request: value.request };
  } catch { return null; }
}
export function gtmRequestFence() {
  let epoch = 0, owner = "";
  return {
    activate(key: string) { epoch++; owner = key; },
    invalidate() { epoch++; owner = ""; },
    capture() { const ticket = epoch, key = owner; return () => !!key && key === owner && ticket === epoch; },
  };
}
export const diagnosisActive = (run: DiagnosisRun | null) => !!run && ["queued", "running"].includes(run.state);
export function diagnosisNeedsResolution(run: DiagnosisRun | null) {
  return diagnosisActive(run) || !!run && (run.state === "needs_input" || run.reserved_micro > 0);
}

/** Saving an approval is a prerequisite: storage failure means zero transport calls. */
export async function persistDiagnosisIntent<T>(intent: DiagnosisIntent, storage: Pick<Storage, "setItem">,
  submit: (saved: DiagnosisIntent) => Promise<T>): Promise<T> {
  storage.setItem(gtmIntentKey(intent.owner), saveGtmIntent(intent));
  return submit(intent);
}
