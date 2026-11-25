import { api, type ApiResult } from "./client.ts";

export const ACTION_COMPONENTS = {
  prep_call_brief: "call_brief", log_call_learnings: "call_notes", follow_up_commitment: "draft_editor",
  synthesize_learnings: "learning_summary", decide_pivot_persevere: "venture_decision", run_pricing_test: "pricing_test",
  landing_page_waitlist: "landing_page", mvp_scope: "mvp_scope", collect_first_payment: "payment_draft",
  ask_for_referral: "draft_editor", reengage_outreach: "journey_entry", clarify: "journey_entry", validate: "journey_entry",
  research: "journey_entry", find_customers: "journey_entry", review_leads: "journey_entry",
  schedule_call: "call_schedule", review_progress: "journey_entry",
} as const;
export type ActionId = keyof typeof ACTION_COMPONENTS;
export type NextStepComponent = typeof ACTION_COMPONENTS[ActionId];
export type StepEvidence = { id: string; kind: string; summary: string; display_summary?: string | null; contact_id: string | null; observations?: StepObservations | null };

export function evidenceLabel(kind: string): string {
  const labels: Record<string, string> = {
    venture: "Your idea", idea: "Your idea", founder_fact: "What you told us", review: "Your review",
    website: "Your website", page: "Your website", research: "Research", journey: "Your customer search",
    lead: "Your prospect", sent: "Your outreach", reply: "A reply", call: "Your call", call_schedule: "Your call",
    manual_outreach: "Your outreach", brief: "Your call brief", call_notes: "Your call notes", learnings: "What you learned",
    pricing: "Your pricing feedback", payment: "Your payment", venture_decision: "Your decision",
    mvp_scope: "Your product scope", learning_deadline: "Your learning deadline", waitlist: "Your waitlist", credits: "Your credits",
  };
  return Object.hasOwn(labels, kind) ? labels[kind] : "Saved evidence";
}

export function evidenceSummary(evidence: StepEvidence): string {
  return evidence.display_summary?.trim() || "Saved evidence supports this step.";
}
export type ActionCard = { action_id: ActionId; title: string; component: NextStepComponent;
  risk: "none" | "sends" | "spends" | "publishes"; draft_only: boolean;
  params: { contact_id?: string | null; subject?: string; body?: string; text?: string; context?: string };
  why: { text: string; evidence_ids: string[] }[]; required_evidence_ids: string[] };
export type NextStepDecision = { decision_id: string; snapshot_hash: string; venture_id: string; workspace_id: string;
  hero: ActionCard | null; alternatives: ActionCard[]; evidence: Record<string, StepEvidence>; source: "rules" | "llm"; refining: boolean };
export type StepObservations = { pains_confirmed: string[]; pains_refuted: string[]; objections: string[];
  current_workaround: string; money_or_time_spent: string; willingness_to_pay: "yes" | "maybe" | "no" | "unknown";
  commitment: "none" | "follow_up" | "intro" | "loi" | "pay" };
export type StepResult = { text?: string; viewed?: boolean; decision?: "persevere" | "narrow_segment" | "pivot_problem";
  price_response?: "yes" | "maybe" | "no"; commitment?: StepObservations["commitment"];
  payment_received?: boolean; sent_by_founder?: boolean; scheduled_at?: string; observations?: StepObservations };
export type StepOutcome = "done" | "skip" | "not_relevant";

export function validCard(value: unknown, evidence: Record<string, StepEvidence>): value is ActionCard {
  if (!value || typeof value !== "object") return false;
  const row = value as ActionCard;
  if (!Object.hasOwn(ACTION_COMPONENTS, row.action_id) || ACTION_COMPONENTS[row.action_id] !== row.component
    || !["none", "sends", "spends", "publishes"].includes(row.risk) || row.draft_only !== (row.risk !== "none")
    || typeof row.title !== "string" || !row.title || row.title.length > 200 || !row.params || typeof row.params !== "object"
    || !Array.isArray(row.why) || !row.why.length || row.why.length > 3
    || !Array.isArray(row.required_evidence_ids) || !row.required_evidence_ids.length) return false;
  const cited = new Set<string>();
  if (!row.why.every(why => typeof why?.text === "string" && why.text.length > 0 && why.text.length <= 600
    && Array.isArray(why.evidence_ids) && why.evidence_ids.length > 0 && why.evidence_ids.every(identifier => {
      if (typeof identifier !== "string" || !Object.hasOwn(evidence, identifier)) return false;
      cited.add(identifier); return true;
    }))) return false;
  if (!row.required_evidence_ids.every(identifier => cited.has(identifier))) return false;
  if (["call_brief", "call_notes", "call_schedule"].includes(row.component) && !row.params.contact_id) return false;
  if (row.params.contact_id != null && (typeof row.params.contact_id !== "string" || !row.required_evidence_ids.some(
    identifier => evidence[identifier]?.contact_id === row.params.contact_id))) return false;
  if (["draft_editor", "payment_draft", "pricing_test"].includes(row.component)
      && (typeof row.params.subject !== "string" || !row.params.subject || row.params.subject.length > 120
        || typeof row.params.body !== "string" || !row.params.body || row.params.body.length > 4000)) return false;
  if (["learning_summary", "venture_decision", "mvp_scope"].includes(row.component) && (typeof row.params.text !== "string" || !row.params.text || row.params.text.length > 8000)) return false;
  if (["landing_page", "journey_entry"].includes(row.component) && (typeof row.params.context !== "string" || !row.params.context || row.params.context.length > 2000)) return false;
  return true;
}

export function checkedNextStep(value: unknown, ventureId: string): NextStepDecision {
  const row = value as NextStepDecision;
  if (!row || row.venture_id !== ventureId || typeof row.workspace_id !== "string" || !row.workspace_id
      || !["rules", "llm"].includes(row.source) || typeof row.refining !== "boolean" || row.source === "llm" && row.refining
      || typeof row.decision_id !== "string" || !/^[a-f0-9]{64}$/.test(row.snapshot_hash)
      || !row.evidence || typeof row.evidence !== "object" || Array.isArray(row.evidence)
      || !Object.entries(row.evidence).every(([identifier, item]) => item?.id === identifier && typeof item.kind === "string"
        && typeof item.summary === "string" && (item.display_summary == null || typeof item.display_summary === "string"))
      || row.hero !== null && !validCard(row.hero, row.evidence)
      || !Array.isArray(row.alternatives) || row.alternatives.length > 2 || !row.alternatives.every(card => validCard(card, row.evidence))) {
    throw new Error("Next step could not be verified for this venture. Refresh your saved work.");
  }
  const identifiers = [row.hero?.action_id, ...row.alternatives.map(action => action.action_id)].filter(Boolean);
  if (new Set(identifiers).size !== identifiers.length) throw new Error("Next-step actions are duplicated; refresh saved work.");
  return row;
}

export function stepDestination(action: ActionId, ventureId: string): string {
  const base = `/v/${encodeURIComponent(ventureId)}`;
  if (["clarify", "validate"].includes(action)) return `${base}/idea`;
  if (action === "research") return `${base}/research`;
  if (action === "landing_page_waitlist") return `${base}/site`;
  if (action === "review_progress") return base;
  return `/gtm?venture=${encodeURIComponent(ventureId)}${action === "review_leads" ? "&step=review" : ""}`;
}

export function createNextStepClient(transport: (path: string, init?: RequestInit) => Promise<ApiResult> = api) {
  const path = (ventureId: string) => `/api/ventures/${encodeURIComponent(ventureId)}/next-step`;
  return {
    async get(ventureId: string) {
      const response = await transport(path(ventureId), { cache: "no-store" });
      if (!response.ok) throw new Error("Next step is unavailable. Your saved work is unchanged; retry loading.");
      return checkedNextStep(response.data, ventureId);
    },
    async outcome(decision: NextStepDecision, action: ActionCard, outcome: StepOutcome, result: StepResult = {}) {
      checkedNextStep(decision, decision.venture_id);
      if (!([decision.hero, ...decision.alternatives].some(card => card?.action_id === action.action_id))) throw new Error("Action was not offered.");
      const response = await transport(`${path(decision.venture_id)}/outcome`, { method: "POST",
        body: JSON.stringify({ decision_id: decision.decision_id, action_id: action.action_id, outcome, result }) });
      if (!response.ok) {
        const detail = (response.data as { detail?: unknown } | null)?.detail;
        throw new Error(typeof detail === "string" ? detail : "Outcome was not confirmed. Refresh saved work before retrying; it may already be saved.");
      }
    },
  };
}
export const nextStepClient = createNextStepClient();
