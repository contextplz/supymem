/** Company operations use the authenticated Supymem gateway and a verified workspace selection. */
import { api, authenticatedFetch } from "@/lib/client";

import { operationsReadPath } from "@/lib/operationsScope";
import { checkedGmailMailboxes, type GmailMailboxes } from "./gmailMailboxes";
export type { GmailMailbox, GmailMailboxes } from "./gmailMailboxes";

let selectedWorkspace = "";
let selectedVenture: string | undefined;
export function setOpsWorkspace(workspaceId: string, ventureId?: string) { selectedWorkspace = workspaceId; selectedVenture = ventureId; }
export function getOpsWorkspace() { return selectedWorkspace; }

function opsPath(path: string, method = "GET", workspaceId = selectedWorkspace): string {
  if (!workspaceId) throw new Error("Choose a company workspace to continue.");
  path = operationsReadPath(path, method, workspaceId === selectedWorkspace ? selectedVenture : undefined);
  const url = new URL(path.startsWith("/") ? path : `/${path}`, "https://scope.invalid");
  if (url.origin !== "https://scope.invalid" || url.hash) throw new Error("Invalid operations path.");
  url.searchParams.set("workspace_id", workspaceId);
  return `/api/api/v1${url.pathname}${url.search}`;
}

async function opsRawFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const target = opsPath(path, options.method || "GET");
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers);
  headers.set("X-Workspace-ID", selectedWorkspace);
  return authenticatedFetch(target, { ...options, method, headers });
}

async function apiFetch<T>(path: string, options: RequestInit = {}, workspaceId = selectedWorkspace): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("X-Workspace-ID", workspaceId);
  const result = await api(opsPath(path, options.method || "GET", workspaceId), { ...options, headers });
  if (!result.ok) {
    if (!result.status) throw new Error("Supymem could not connect. Please try again.");
    const data = result.data as { detail?: unknown; error?: { message?: string; code?: string } } | null;
    const structured = data?.detail && typeof data.detail === "object" ? data.detail as { message?: string; code?: string; errors?: unknown[] } : null;
    const validation = structured?.errors?.filter((value): value is string => typeof value === "string").slice(0, 12).join(" ");
    const detail = data?.error?.message || data?.error?.code || structured?.message || validation || structured?.code || (typeof data?.detail === "string" ? data.detail : "Operation could not be completed.");
    throw Object.assign(new Error(`API ${result.status}: ${detail}`), { status: result.status, detail: data?.detail });
  }
  return result.data as T;
}

export const opsApi = {
  get: <T>(path: string) => apiFetch<T>(path),
  post: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: "PATCH", body: JSON.stringify(body ?? {}) }),
};

export type ActivityEvent = {
  id: string;
  agent_type: string;
  action: string;
  summary: string;
  level: "info" | "success" | "warning" | "error";
  created_at: string;
};

export type DashboardSummary = {
  tasks_today_total: number;
  tasks_today_completed: number;
  tasks_today_pending: number;
  tasks_today_failed: number;
  active_agents: string[];
  kpis?: Record<string, unknown>;
  timezone: string;
  scope: { venture_id: string | null };
  last_report_date: string | null;
};

export type Task = {
  id: string;
  provenance: "durable_job" | "legacy_ops_task";
  error_code?: string | null;
  title: string;
  agent_type: string;
  status: string;
  priority: number;
  created_at: string;
};

export type AgentStatus = {
  id: string;
  label: string;
  provenance: string;
  observed_status: string;
  observed_at: string | null;
  agent_type: string;
  last_run_at: string | null;
  last_run_status: string | null;
  tasks_today: number;
  tasks_total: number;
};

export type FinanceSummary = {
  mrr_cents: number | null;
  arr_cents: number | null;
  active_subscribers: number | null;
  total_ad_spend_usd: number;
  total_expenses_month_cents: number;
  stripe_balance_cents: number | null;
  last_snapshot_date: string | null;
  revenue_basis: "recorded_snapshot" | "unavailable";
  spend_basis: "recorded_entries_not_provider_reconciled";
  recorded_ad_campaign_count: number;
  recorded_expense_count: number;
  expense_currency: "usd";
  expense_period_start: string;
};

export type SocialPost = {
  id: number;
  content: string;
  status: string;
  published_at: string | null;
  engagement: Record<string, number> | null;
};

export type Prospect = {
  id: string;
  email: string;
  first_name: string | null;
  company: string | null;
  status: string;
  created_at: string;
  hook?: string | null;
  role?: string | null;
  linkedin_url?: string | null;
  domain?: string | null;
  email_confidence?: string | null;
};

export type OutreachToday = {
  l1: {
    signals_found: number;
    dossiers_completed: number;
    ready_for_approval: number;
    human_replies: number;
  };
  l2: { conversations: number; offers: number };
  l3: { customers: number };
  first_five: Array<{
    id: string;
    first_name: string | null;
    email: string | null;
    hook: string | null;
    stage: string;
  }>;
  recommended_next: string;
};

export type AdCampaign = {
  id: string;
  platform: string;
  external_id: string;
  name: string;
  objective: string;
  status: string;
  daily_budget_usd: number;
  created_at: string;
};

export type AdProposal = {
  id: string;
  platform: "google" | "meta";
  status: "draft" | "pending_approval" | "approved" | "created" | "blocked" | "rejected";
  idea: string;
  blueprint: {
    name: string;
    goal: string;
    objective: string;
    daily_budget_usd: number;
    landing_url: string;
    audience: string;
    geography: string;
    creative: { headline: string; description: string };
    status_on_create: "PAUSED";
  };
  approval_id: string | null;
  created_at: string;
  updated_at: string;
};

export type MemoryItem = {
  id: number;
  category: string;
  title: string;
  content: string;
  created_at: string;
};

export type CompanyConfig = {
  name: string;
  mission: string | null;
  vision: string | null;
  description: string | null;
  target_market: string | null;
  value_prop: string | null;
  website_url: string | null;
  industry: string | null;
  product_type: string | null;
  timezone: string;
  daily_cycle_hour: number;
};

/** Requests retain empty results separately from authenticated API failures. */
export async function getDashboardSummary(): Promise<DashboardSummary | null> {
  return opsApi.get<DashboardSummary>("/dashboard/summary");
}

export async function getTasks(limit = 100): Promise<Task[]> {
  return opsApi.get<Task[]>(`/tasks?limit=${limit}`);
}

export async function getSocialPosts(limit = 50): Promise<SocialPost[]> {
  return opsApi.get<SocialPost[]>(`/social/posts?limit=${limit}`);
}

export async function ingestOutreachPaste(body: {
  source: "profile_paste" | "job_post_paste" | "thread_url_paste" | "founder_note";
  raw_text?: string;
  url?: string;
  trigger_code?: string;
  company_name?: string;
}) {
  return opsApi.post<{
    ok: boolean;
    contact_id: string | null;
    signal_id: string;
    company_id: string | null;
    layer: string;
    quote: string | null;
    personalize_ready: boolean;
  }>("/outreach/ingest", { ...body, enrich: false });
}

export async function getOutreachQueue() {
  return opsApi.get<{
    items: Array<{
      id: string;
      first_name: string | null;
      email: string | null;
      role: string | null;
      band: string | null;
      explain: string | null;
      personalize: string | null;
      company: string | null;
      why_this_week: string | null;
    }>;
    ready: boolean;
    empty_reason?: string;
  }>("/outreach/queue");
}

export async function getOutreachOnboarding() {
  return opsApi.get<{
    ready: boolean;
    icp: boolean;
    excludes: boolean;
    sender_geo: boolean;
    send_path: boolean;
    voice: boolean;
    evidence: boolean;
    outbound_mode?: string;
    sender_geo_code?: string | null;
    sender_postal_address?: string | null;
    segment?: Record<string, unknown>;
    voice_config?: Record<string, unknown>;
    evidence_sources?: Array<{
      provider: string;
      status: string;
      connected_account_id?: string | null;
      fake?: boolean;
    }>;
  }>("/outreach/onboarding");
}

export async function patchOutreachOnboarding(body: Record<string, unknown>) {
  return opsApi.patch<Record<string, unknown>>("/outreach/onboarding", body);
}

export async function patchOutreachContact(
  contactId: string,
  body: Record<string, unknown>,
) {
  return opsApi.patch<Record<string, unknown>>(`/outreach/contacts/${contactId}`, body);
}

export async function createOutreachDraft(contactId: string, kind = "connect") {
  return opsApi.post<{
    id: string;
    contact_id: string;
    kind: string;
    body: string | null;
    blocked: boolean;
    reason: string | null;
    personalize: string;
    explain: string | null;
    band: string | null;
    copied_at: string | null;
    sent_at: string | null;
  }>("/outreach/drafts", { contact_id: contactId, kind });
}

export async function copyOutreachDraft(draftId: string) {
  return opsApi.post<{ id: string; copied_at: string | null; sent_at: string | null }>(
    `/outreach/drafts/${draftId}/copy`,
  );
}

export async function markOutreachDraftSent(draftId: string, channel = "linkedin_connect") {
  return opsApi.post<{ id: string; sent_at: string | null; copied_at: string | null }>(
    `/outreach/drafts/${draftId}/sent`,
    { channel },
  );
}

export async function getOutreachFunnel() {
  try {
    return await opsApi.get<{
      stages: Record<string, { n: number; d: number; d_meaning: string; rate: number | null }>;
    }>("/outreach/funnel");
  } catch {
    return { stages: {} };
  }
}

export async function getOutreachStatus() {
  try {
    return await opsApi.get<{
      ready: boolean;
      queue_count: number;
      examples: Array<Record<string, unknown>>;
      onboarding: Record<string, unknown>;
      remaining?: { connect: number; dm: number; email: number };
      outreach_week?: number;
    }>("/outreach/status");
  } catch {
    return null;
  }
}

export type GtmGodTask = {
  id: string;
  kind: string;
  owner_agent: string;
  status: string;
  horizon: string;
  human_instruction: string | null;
  evidence_tag: string;
  output: Record<string, unknown>;
  depends_on: string[];
};

export type GtmGodStatus = {
  ok: boolean;
  plan: {
    id: string;
    status: string;
    idea: string;
    problem: string;
    brief: Record<string, unknown>;
    diagnosis: {
      bottleneck?: string;
      skill?: string;
      rationale?: string;
      llm_used?: boolean;
      now?: Array<Record<string, unknown>>;
      next?: Array<Record<string, unknown>>;
      later?: Array<Record<string, unknown>>;
    };
    artifacts: Record<string, unknown>;
    log: Array<Record<string, unknown>>;
  } | null;
  tasks: GtmGodTask[];
  messages: Array<{
    id: string;
    from: string;
    to: string;
    body: string;
    task_id: string | null;
    created_at: string | null;
  }>;
};

export async function recordOutreachEvidence(provider: string, action: string) {
  return opsApi.post<{ provider: string; status: string; fake: boolean }>(
    "/outreach/evidence-sources",
    { provider, action },
  );
}

export async function captureOutreachInterview(body: {
  contact_id: string;
  notes_md: string;
  kind?: string;
  mom_test_ok?: boolean;
}) {
  return opsApi.post<{
    id: string;
    extraction: Record<string, unknown>;
    hypotheticals_flagged: string[];
    mom_test_ok: boolean;
  }>("/outreach/interviews", body);
}

export async function listOutreachPilots() {
  return opsApi.get<{ items: Array<Record<string, unknown>> }>("/outreach/pilots");
}

export async function createOutreachPilot(body: {
  contact_id: string;
  use_case: string;
  success_criterion: string;
  team?: string;
}) {
  return opsApi.post<Record<string, unknown>>("/outreach/pilots", body);
}

export async function patchOutreachPilot(pilotId: string, body: Record<string, unknown>) {
  return opsApi.patch<Record<string, unknown>>(`/outreach/pilots/${pilotId}`, body);
}

export async function getAdCampaigns(): Promise<AdCampaign[]> {
  const result = await opsApi.get<{ items: AdCampaign[] }>("/ads/campaigns");
  return result.items;
}

export async function getAdsReadiness() {
  return opsApi.get<{
    providers: Record<string, { ok: boolean; missing: string[]; note: string }>;
    creation: string;
    activation: string;
  }>("/ads/readiness");
}

export async function getAdProposals() {
  return opsApi.get<{ items: AdProposal[] }>("/ads/proposals");
}

export async function createAdProposal(body: {
  platform: "google" | "meta";
  idea: string;
  goal: "leads" | "sales" | "traffic";
  landing_url: string;
  audience: string;
  geography: string;
  daily_budget_usd: number;
  name?: string;
  headline?: string;
  description?: string;
}) {
  return opsApi.post<AdProposal>("/ads/proposals", body);
}

export async function requestAdLaunch(proposalId: string) {
  return opsApi.post<AdProposal>(`/ads/proposals/${proposalId}/request-launch`);
}

export async function decideAdLaunch(proposalId: string, decision: "approved" | "rejected") {
  return opsApi.post<AdProposal>(`/ads/proposals/${proposalId}/decide`, { decision });
}

export async function createPausedAdCampaign(proposalId: string) {
  return opsApi.post<{ ok: boolean; campaign?: AdCampaign; outcome?: Record<string, unknown> }>(
    `/ads/proposals/${proposalId}/create-paused`,
  );
}

export async function getFinanceSummary(): Promise<FinanceSummary | null> {
  return opsApi.get<FinanceSummary>("/finance/summary");
}

export type AiGtmLead = {
  first_name: string;
  last_name: string;
  email: string;
  title: string;
  organization: string;
  industry: string;
  employees: string;
  linkedin_url: string;
  city: string;
  country: string;
  website: string;
  email_confidence: string;
  email_score: string;
  source: string;
  vertical: string;
  email_tier: string;
  confidence_pct: string;
};

export type AiGtmFindResult = {
  leads: AiGtmLead[];
  count: number;
  state?: "completed" | "partial" | "unavailable" | "no_matches";
  safe: number;
  risky: number;
  query: string;
  industry: string | null;
  source: string;
  deep: boolean;
  message?: string | null;
  log_tail?: string;
  ingested?: number;
  hunter_filled?: number;
  contacts?: AiGtmContact[];
  outreach?: AiGtmOutreachResult;
};

export type AiGtmEngine = {
  workspace_id: string;
  discovery_enabled: boolean;
  send_enabled: boolean;
  icp_query: string;
  find_batch_size: number;
  ready_email_buffer: number;
  ready_linkedin_buffer: number;
  week_index: number;
  email_monthly_target: number;
  linkedin_monthly_target: number;
  email_daily_cap: number;
  linkedin_daily_cap: number;
  last_discovery_at: string | null;
  last_learn_at: string | null;
};

export type AiGtmFunnel = {
  by_stage: Record<string, number>;
  email_ready: number;
  linkedin_ready: number;
  emailed: number;
  replied: number;
};

export type AiGtmStatus = {
  ready: boolean;
  bin?: string;
  home?: string;
  version?: string | null;
  hint?: string | null;
  error?: string;
  openleads?: { ready?: boolean; version?: string | null };
  scrapy?: { ready?: boolean; version?: string | null; error?: string };
  opengtm?: {
    ready?: boolean;
    version?: string | null;
    hint?: string | null;
    gemini?: { ready?: boolean };
    crm_webhook?: { ready?: boolean };
    offline_ok?: boolean;
  };
  hunter?: { ready?: boolean };
  brightdata?: { ready?: boolean };
  gmail?: { ready?: boolean; configured?: boolean; hint?: string | null };
  llm?: { ready?: boolean; provider?: string; model?: string; label?: string };
  dry_run?: boolean;
  linkedin_dm?: { supported?: boolean; reason?: string; mode?: string };
  unipile?: { ready?: boolean };
  engine?: AiGtmEngine;
  funnel?: AiGtmFunnel;
  localization?: {
    allowed: boolean;
    mode: "legacy" | "thesis" | "approval_required";
    reason?: string;
    thesis?: AiGtmThesis | null;
  };
};

export type AiGtmMarketProfile = {
  id: string;
  workspace_id: string;
  version: number;
  name: string;
  status: "draft" | "active" | "archived";
  seller: Record<string, unknown>;
  product: Record<string, unknown>;
  segment: Record<string, unknown>;
  constraints: Record<string, unknown>;
  evidence: Array<Record<string, unknown>>;
  created_at: string;
  updated_at: string;
};

export type AiGtmThesis = {
  id: string;
  workspace_id: string;
  profile_id: string;
  version: number;
  status: "draft" | "approved" | "rejected" | "superseded";
  archetype: {
    dominant?: string;
    weights?: Record<string, number>;
  };
  buying_profile: {
    primary_motivations?: string[];
    secondary_motivations?: string[];
    objections?: string[];
    proof_required?: string[];
    decision_speed?: string;
    relationship_requirement?: string;
    pricing_sensitivity?: string;
    buyer_roles?: string[];
  };
  strategy: {
    target?: Record<string, unknown>;
    why_now?: string;
    channel_portfolio?: Record<string, number>;
    message_angle?: string;
    offer?: string;
    proof?: string[];
    cadence?: Record<string, unknown>;
    experiment?: Record<string, unknown>;
  };
  assumptions: string[];
  evidence: Array<Record<string, unknown>>;
  policy: {
    allowed?: boolean;
    execution_mode?: string;
    violations?: Array<{ code: string; path: string; message: string }>;
    warnings?: string[];
  };
  confidence: number;
  model: string | null;
  approved_at: string | null;
  created_at: string;
};

export type AiGtmExperiment = {
  id: string;
  thesis_id: string;
  hypothesis: string;
  primary_metric: string;
  threshold: number;
  minimum_sample_size: number;
  window_days: number;
  variants: Array<Record<string, unknown>>;
  allocation: Record<string, number>;
  guardrails: Record<string, unknown>;
  status: "planned" | "running" | "paused" | "completed" | "cancelled";
  started_at: string | null;
  created_at: string;
};

export type AiGtmContact = {
  id: string;
  email: string;
  first_name: string | null;
  last_name: string | null;
  company: string | null;
  domain: string | null;
  role: string | null;
  linkedin_url: string | null;
  email_confidence: string | null;
  status: string;
  source: string | null;
  hook: string | null;
  times_emailed: number;
  times_linkedin?: number;
  last_emailed_at: string | null;
  replied_at?: string | null;
  bounced_at?: string | null;
  sequence_kind?: string;
  preferred_channel?: string;
  enrichment_level?: number;
  linkedin_stage?: string | null;
  secondary_email?: string | null;
  icp_score?: number | null;
  icp_tier?: string | null;
  outreach_pattern?: string | null;
  opengtm_messages?: {
    pattern?: string;
    connection_note?: string;
    first_dm?: string;
    followup?: string;
    followup_2?: string;
    followup_3?: string;
  } | null;
  sequence?: Record<
    string,
    { subject?: string; body?: string; sent_at?: string; simulated?: boolean; hook?: string; source?: string; pattern?: string }
  >;
  snapshots?: Array<{ id: string; day: number | null; richness: number; notes: string; created_at: string }>;
  linkedin_jobs?: Array<{
    id: string;
    step: string;
    status: string;
    body: string | null;
    scheduled_for: string;
  }>;
};

export type AiGtmMailbox = {
  id: string;
  email: string | null;
  provider: string;
  status: string;
  daily_send_limit: number;
  sent_today: number;
};

export type AiGtmOutreachResult = {
  enrolled: string[];
  refused: Array<{ id: string; reason: string }>;
  queued: number;
  sent: Array<{ contact_id?: string; email?: string; day?: number; simulated?: boolean; error?: string }>;
  dry_run?: boolean;
  blocker?: string | null;
};

export async function getAiGtmStatus(): Promise<AiGtmStatus | null> {
  return opsApi.get<AiGtmStatus>("/ai-gtm/status");
}

export async function runAiGtmFind(body: {
  query?: string;
  count?: number;
  industry?: string | null;
  source?: string | null;
  deep?: boolean;
  verified_only?: boolean;
  auto_outreach?: boolean;
  region?: string | null;
  icp_profile?: string;
}): Promise<AiGtmFindResult> {
  return opsApi.post<AiGtmFindResult>("/ai-gtm/find", {
    query: body.query ?? "B2B SaaS founders",
    count: body.count ?? 50,
    industry: body.industry ?? "B2B",
    source: body.source ?? "auto",
    deep: body.deep ?? true,
    verified_only: body.verified_only ?? false,
    auto_outreach: body.auto_outreach ?? true,
    region: body.region ?? null,
    icp_profile: body.icp_profile ?? "saas",
  });
}

export async function getAiGtmContacts(): Promise<{
  contacts: AiGtmContact[];
  mailboxes: AiGtmMailbox[];
}> {
  return opsApi.get("/ai-gtm/contacts");
}

export async function connectAiGtmGmail(): Promise<{
  redirect_url: string | null;
  connection_id: string | null;
  app_user_id: string;
}> {
  return opsApi.post("/ai-gtm/gmail/connect", {});
}

export async function syncAiGtmGmail(): Promise<{
  accounts: AiGtmMailbox[];
  synced: number;
}> {
  return opsApi.get("/ai-gtm/gmail/accounts");
}

export async function startAiGtmOutreach(body?: {
  contact_ids?: string[];
  auto_send?: boolean;
}): Promise<AiGtmOutreachResult> {
  return opsApi.post("/ai-gtm/outreach/start", {
    contact_ids: body?.contact_ids,
    auto_send: body?.auto_send ?? false,
  });
}

export async function setAiGtmRun(body: {
  discovery?: boolean;
  send?: boolean;
  icp_query?: string;
  week_index?: number;
  find_batch_size?: number;
}): Promise<AiGtmEngine> {
  return opsApi.post("/ai-gtm/run", body);
}

export async function startAiGtmLinkedIn(body?: {
  contact_ids?: string[];
}): Promise<{ enrolled: string[]; skipped: string[]; provider: string; blocker?: string }> {
  return opsApi.post("/ai-gtm/linkedin/start", {
    contact_ids: body?.contact_ids,
  });
}

/** Download leads as JSON, CSV, or Excel via the Ops API. */
export async function downloadAiGtmExport(
  leads: AiGtmLead[],
  format: "json" | "xlsx" | "csv"
): Promise<void> {
  const res = await opsRawFetch("/ai-gtm/export", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ leads, format }),
    cache: "no-store",
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`Export failed: ${detail}`);
  }
  const blob = await res.blob();
  const ext = format === "xlsx" ? "xlsx" : format === "csv" ? "csv" : "json";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `ai_gtm_leads.${ext}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export type AiGtmLabScenario = {
  id: string;
  label: string;
  industry: string;
  role: string;
  company_size?: string | null;
  geo?: string | null;
  keyword?: string | null;
  query: string;
  mode?: string;
};

export type AiGtmLabGenerateResult = AiGtmFindResult & {
  ok?: boolean;
  lab?: boolean;
  auto_outreach?: boolean;
  scenario?: AiGtmLabScenario;
  region?: string | null;
  engine?: AiGtmEngine;
  funnel?: AiGtmFunnel;
};

/** Lab generate: persists leads, never starts outreach. */
export async function runAiGtmLabGenerate(body: {
  count?: number;
  random?: boolean;
  scenario_id?: string | null;
  industry?: string | null;
  role?: string | null;
  company_size?: string | null;
  geo?: string | null;
  keyword?: string | null;
  query?: string | null;
  source?: string | null;
  deep?: boolean;
  enable_discovery?: boolean;
}): Promise<AiGtmLabGenerateResult> {
  return opsApi.post<AiGtmLabGenerateResult>("/ai-gtm/lab/generate", {
    count: body.count ?? 15,
    random: body.random ?? false,
    scenario_id: body.scenario_id ?? null,
    industry: body.industry ?? null,
    role: body.role ?? null,
    company_size: body.company_size ?? null,
    geo: body.geo ?? null,
    keyword: body.keyword ?? null,
    query: body.query ?? null,
    source: body.source ?? "auto",
    deep: body.deep ?? true,
    enable_discovery: body.enable_discovery ?? false,
  });
}

/* ------------------------------------------------------------------ */
/* Content engine                                                      */
/* ------------------------------------------------------------------ */

export type ContentProvider = {
  platform: string;
  label: string;
  configured: boolean;
  native_configured?: boolean;
  composio?: boolean;
  composio_managed?: boolean;
  auth_via_default?: string;
  client_id_env: string;
  client_secret_env: string;
  scopes: string[];
  max_chars: number;
  allows_replies: boolean;
  caveat: string;
};

export type ContentAccount = {
  id: string;
  platform: string;
  handle: string;
  external_account: string;
  status: string;
  health: string;
  scopes: string[];
  expires_at: string | null;
  daily_cap: number;
  posts_today: number;
  paused_reason: string | null;
  last_error: string | null;
  last_post_at: string | null;
  auth_via?: string;
  author_urn?: string | null;
};

export type ContentBrief = {
  id: string;
  venture_id: string | null;
  version: number;
  status: string;
  source_name: string | null;
  product: Record<string, unknown>;
  icp: { segments?: Array<Record<string, unknown>> };
  competitors: Array<Record<string, unknown>>;
  voice: Record<string, unknown>;
  claims: Array<Record<string, unknown>>;
  cadence: Record<string, unknown>;
  created_at: string;
};

export type ContentAgentStatus = {
  enabled: boolean;
  dry_run: boolean;
  mode?: string;
  loop?: string;
  accounts?: ContentAccount[];
  policies?: Record<
    string,
    {
      can_post?: boolean;
      can_reply?: boolean;
      can_listen_comments?: boolean;
      can_listen_reactions?: boolean;
      can_listen_sector?: boolean;
      reply_caveat?: string;
    }
  >;
};

export type ContentStatus = {
  workspace_id: string;
  brief: ContentBrief | null;
  has_brief: boolean;
  accounts: ContentAccount[];
  providers: ContentProvider[];
  dry_run: boolean;
  engine_enabled: boolean;
  queued_topics: number;
  next_scheduled: string | null;
  composio_configured?: boolean;
  composio_synced?: Array<Record<string, unknown>>;
  agent?: ContentAgentStatus;
};

export type ContentPost = {
  id: string;
  handoff?: { text: string; markdown: string; filename: string; next_step: string; image_next_step: string };
  generation_run_id?: string | null;
  provenance?: string;
  edit_digest?: string;
  platform: string;
  intent: string;
  state: string;
  title: string | null;
  body: string;
  target: string | null;
  lint: { ok?: boolean; reasons?: string[]; warnings?: string[] };
  critic: { score?: number | null; verdict?: string; problems?: string[]; claims_reviewed?: boolean;
    claim_checks?: Array<{ draft_quote: string; status: string; reason: string;
      evidence: Array<{ source_id: string; quote: string }> }> };
  refusal_reason: string | null;
  scheduled_for: string | null;
  published_at: string | null;
  external_url: string | null;
  dry_run: boolean;
  created_at: string;
};

export type ContentEvent = {
  id: string;
  kind: string;
  platform: string | null;
  post_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
};

export async function getContentStatus(
  opts?: { sync?: boolean; ventureId?: string }
): Promise<ContentStatus> {
  const params = new URLSearchParams();
  if (opts?.sync) params.set("sync", "true");
  if (opts?.ventureId) params.set("venture_id", opts.ventureId);
  const q = params.size ? `?${params.toString()}` : "";
  return opsApi.get<ContentStatus>(`/content/status${q}`);
}

export async function getContentBriefTemplate(): Promise<unknown> {
  return opsApi.get<unknown>("/content/brief/template");
}

export async function uploadContentBrief(
  brief: unknown,
  sourceName: string | undefined,
  ventureId: string
): Promise<ContentBrief> {
  return opsApi.post<ContentBrief>("/content/brief", {
    brief,
    source_name: sourceName ?? null,
    venture_id: ventureId,
  });
}

export async function startContentOAuth(
  platform: string
): Promise<{
  authorize_url: string;
  state: string;
  redirect_uri: string;
  auth_via?: string;
}> {
  return opsApi.post(`/content/oauth/${platform}/start`, {});
}

export async function syncContentAccounts(): Promise<{
  synced: Array<Record<string, unknown>>;
  accounts: ContentAccount[];
}> {
  return opsApi.post("/content/accounts/sync", {});
}

export async function forgetContentAccounts(): Promise<{
  revoked_accounts: string[];
  composio_removed: string[];
  accounts: ContentAccount[];
}> {
  return opsApi.post("/content/accounts/forget", {});
}

export async function revokeContentAccount(
  accountId: string
): Promise<ContentAccount> {
  return opsApi.post<ContentAccount>(
    `/content/accounts/${accountId}/revoke`,
    {}
  );
}

export async function runContentTick(): Promise<Record<string, unknown>> {
  return opsApi.post("/content/tick", {});
}

export async function runContentAgentTick(): Promise<Record<string, unknown>> {
  return opsApi.post("/content/agent/tick", {});
}

export async function getContentPosts(limit = 40, ventureId?: string): Promise<ContentPost[]> {
  const res = await opsApi.get<{ posts: ContentPost[] }>(
    `/content/posts?limit=${limit}${ventureId ? `&venture_id=${encodeURIComponent(ventureId)}` : ""}`
  );
  return res.posts || [];
}

export async function getContentEvents(limit = 40): Promise<ContentEvent[]> {
  const res = await opsApi.get<{ events: ContentEvent[] }>(
    `/content/events?limit=${limit}`
  );
  return res.events || [];
}

export type MarketingAsset = {
  id: string;
  kind: string;
  source: string;
  title: string | null;
  mime: string;
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  review_status: string;
  url: string;
  license?: Record<string, unknown>;
  meta?: Record<string, unknown>;
  created_at: string | null;
};

export type MarketingJobRow = {
  id: string;
  workspace_id?: string;
  plan_id?: string;
  kind: string;
  title: string;
  status: string;
  platform: string | null;
  priority?: number;
  attempt_count?: number;
  executor?: string | null;
  block_reason: string | null;
  error_message: string | null;
  scheduled_for?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string | null;
  updated_at?: string | null;
  depends_on?: string[];
  payload?: Record<string, unknown>;
  proof?: Record<string, unknown>;
  result?: Record<string, unknown>;
};

export type MarketingPlanView = {
  id: string;
  workspace_id?: string;
  brief_id?: string | null;
  horizon_days?: number;
  status?: string;
  source?: string;
  strategy?: Record<string, unknown>;
  constraints?: Record<string, unknown>;
  job_counts?: Record<string, number>;
};

export type MarketingLearning = {
  events?: number;
  kinds?: Record<string, number>;
  job_status?: Record<string, number>;
  recommendations?: string[];
  published?: number;
  linkedin_published?: number;
};

export type MarketingStatus = {
  enabled: boolean;
  plan: MarketingPlanView | null;
  job_counts: Record<string, number>;
  adapters?: string[];
  learning?: MarketingLearning;
  openclaw_browser?: boolean;
  note?: string;
  loop?: string;
  doc?: string;
  flags?: {
    CONTENT_DRY_RUN?: boolean;
    CHANNELS_DRY_RUN?: boolean;
    MARKETING_OUTBOUND_ENABLED?: boolean;
    MARKETING_EXECUTION_ENABLED?: boolean;
  };
};

export type MarketingConnector = {
  ok?: boolean;
  configured?: boolean;
  connected?: boolean;
  active?: number;
  missing?: string[];
  paused_reason?: string | null;
  last_error?: string | null;
  handle?: string | null;
  auth_via?: string | null;
  mode?: string;
  note?: string | null;
  hint?: string | null;
  composio?: { ok?: boolean; missing?: string[] };
};

export type MarketingLabBlocker = {
  code: string;
  severity: string;
  message: string;
};

export type MarketingLabStatus = {
  workspace_id: string;
  flags: {
    CONTENT_DRY_RUN: boolean;
    CHANNELS_DRY_RUN: boolean;
    MARKETING_OUTBOUND_ENABLED: boolean;
    MARKETING_EXECUTION_ENABLED: boolean;
    MARKETING_OWNS_CONTENT?: boolean;
  };
  connectors: Record<string, MarketingConnector>;
  providers?: unknown;
  accounts?: ContentAccount[];
  blockers: MarketingLabBlocker[];
  has_brief: boolean;
  brief_id?: string | null;
  marketing: MarketingStatus;
  gtm: Record<string, unknown>;
  linkedin_dm: {
    mode: string;
    live: boolean;
    enrolled: number;
    banner?: string | null;
  };
  linkedin_actions?: {
    connected?: boolean;
    dry_run?: boolean;
    unipile?: boolean;
    account?: Record<string, unknown> | null;
    actions?: Record<
      string,
      {
        action?: string;
        built?: boolean;
        backend?: string;
        live?: boolean | string;
        workable_now?: boolean;
        blocker?: string | null;
        notes?: string;
      }
    >;
    error?: string;
  };
  media_capabilities?: MediaCapabilities;
  blocked_jobs?: MarketingJobRow[];
  lab_log_path?: string;
  note?: string;
};

export type MarketingLabLogEntry = {
  ts?: string | null;
  level?: string;
  category?: string;
  action?: string;
  http_status?: number | null;
  request?: unknown;
  response?: unknown;
  error?: string | null;
  workspace_id?: string;
};

export type LabCallResult<T = unknown> = {
  ok: boolean;
  status: number;
  data: T | null;
  error?: string;
  routeMissing?: boolean;
};

export type MediaCapabilities = {
  execution_location?: "hosted_server" | "local_backend";
  legacy_execution_available?: boolean;
  unavailable_reason?: string;
  local_first: boolean;
  image: Record<string, unknown>;
  video: Record<string, unknown>;
  stock: Record<string, unknown>;
  browser: Record<string, unknown>;
  desktop: Record<string, unknown>;
  ads: Record<string, unknown>;
};

export async function getMediaCapabilities(): Promise<MediaCapabilities> {
  return opsApi.get<MediaCapabilities>("/marketing/media/capabilities");
}

export async function getMarketingAssets(
  kind?: string
): Promise<MarketingAsset[]> {
  const q = kind ? `?kind=${encodeURIComponent(kind)}` : "";
  const res = await opsApi.get<{ assets: MarketingAsset[] }>(
    `/marketing/assets${q}`
  );
  return res.assets || [];
}

export async function uploadMarketingAsset(
  file: File,
  kind: string
): Promise<{ ok: boolean; asset: MarketingAsset }> {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("kind", kind);
  const res = await opsRawFetch("/marketing/assets", {
    method: "POST",
    body: fd,
    credentials: "same-origin",
    cache: "no-store",
  });
  const json = (await res.json()) as {
    ok?: boolean;
    asset?: MarketingAsset;
    error?: string;
    block_reason?: string;
  };
  if (!res.ok || json.ok === false) {
    throw new Error(json.block_reason || json.error || `API ${res.status}`);
  }
  return json as { ok: boolean; asset: MarketingAsset };
}

export async function reviewMarketingAsset(
  assetId: string,
  status: "approved" | "rejected" | "pending"
): Promise<{ ok: boolean; asset: MarketingAsset }> {
  return opsApi.post(`/marketing/assets/${assetId}/review`, { status });
}

export async function generateMarketingImage(body: {
  headline: string;
  scene?: string;
  want_person?: boolean;
  allow_remote?: boolean;
}): Promise<Record<string, unknown>> {
  return opsApi.post("/marketing/media/image", body);
}

export async function generateMarketingVideo(body: {
  headline: string;
  scene?: string;
  seconds?: number;
  want_person?: boolean;
}): Promise<Record<string, unknown>> {
  return opsApi.post("/marketing/media/video", body);
}

export async function recordMarketingDemo(body: {
  url?: string;
  headline?: string;
  seconds?: number;
  headed?: boolean;
  ingest_path?: string;
  launch_recordly?: boolean;
  short_clip?: boolean;
}): Promise<Record<string, unknown>> {
  return opsApi.post("/marketing/media/demo", body);
}

export async function probeMarketingBrowser(body?: {
  url?: string;
  attach?: boolean;
}): Promise<Record<string, unknown>> {
  return opsApi.post("/marketing/media/browser", body || {});
}

export async function probeMarketingDesktop(body?: {
  app?: string;
}): Promise<Record<string, unknown>> {
  return opsApi.post("/marketing/media/desktop", body || {});
}

export async function createPausedMarketingAd(body: {
  platform: string;
  name?: string;
  daily_budget?: string | number;
  headlines?: string[];
  descriptions?: string[];
  final_url?: string;
}): Promise<Record<string, unknown>> {
  return opsApi.post("/marketing/media/ads", body);
}

export async function getMarketingMediaJobs(): Promise<{
  plan: Record<string, unknown> | null;
  jobs: MarketingJobRow[];
}> {
  return opsApi.get("/marketing/media/jobs");
}

export async function getMarketingAssetBlob(assetId: string): Promise<Blob> {
  const response = await opsRawFetch(`/marketing/assets/${encodeURIComponent(assetId)}/file`);
  if (!response.ok) throw new Error(response.status === 401 ? "Sign in to view this asset." : "This asset could not be loaded.");
  const type = response.headers.get("Content-Type") || "";
  if (!type.startsWith("image/") && !type.startsWith("video/")) throw new Error("The asset is not a supported image or video.");
  return response.blob();
}

// --- Community Department ---

export type CommunityGate = {
  id: string;
  label: string;
  passed: boolean;
  fail_action?: string | null;
  evidence?: string;
};

export type CommunityDimension = {
  id: string;
  label: string;
  weight: number;
  score: number;
  evidence?: string[];
};

export type CommunityThesis = {
  thesis_score: number;
  verdict: string;
  because: string;
  gates: CommunityGate[];
  dimensions: CommunityDimension[];
  blocking_dimension_ids?: string[];
  motion?: string;
  rescore_after_days?: number;
  build_owned?: boolean;
  primary_segment?: Record<string, unknown>;
};

export type CommunityPlan = {
  id: string;
  workspace_id: string;
  status: string;
  verdict?: string;
  thesis_score?: number;
  thesis?: CommunityThesis;
  objectives?: Record<string, unknown> | null;
  identity?: Record<string, unknown> | null;
  program_30d?: Record<string, unknown> | null;
  work_orders?: Record<string, unknown>[];
};

export type CommunityStatus = {
  department: string;
  positioning: string;
  plan: CommunityPlan | null;
  discord: Record<string, unknown>;
  servers: Record<string, unknown>[];
  funnel: {
    stages: Record<string, number>;
    note?: string;
    person_rows?: number;
  };
  phases?: Record<string, boolean>;
  reddit_note?: string;
};

export async function getCommunityStatus(): Promise<CommunityStatus> {
  return opsApi.get("/community/status");
}

export async function postCommunityThesis(
  body?: Record<string, unknown>
): Promise<{
  ok: boolean;
  thesis: CommunityThesis;
  plan?: CommunityPlan;
  research?: Record<string, unknown>;
}> {
  return opsApi.post("/community/thesis", body || {});
}

export async function postCommunityPlan(
  body?: Record<string, unknown>
): Promise<{ ok: boolean; plan: CommunityPlan }> {
  return opsApi.post("/community/plan", body || {});
}

export async function connectCommunityDiscord(): Promise<{
  ok?: boolean;
  redirect_url?: string | null;
  connection_id?: string | null;
  app_user_id?: string;
  auth_config_id?: string;
  toolkit?: string;
  note?: string;
  block_reason?: string;
  needs_you?: boolean;
  error?: string;
}> {
  return opsApi.post("/community/discord/connect", {});
}

export async function getCommunityDiscordStatus(): Promise<
  Record<string, unknown>
> {
  return opsApi.get("/community/discord/status");
}

export async function provisionCommunityDiscord(body?: {
  guild_id?: string;
  plan_id?: string;
}): Promise<Record<string, unknown>> {
  return opsApi.post("/community/discord/provision", body || {});
}

export async function communityDiscordAction(body: {
  action: string;
  params?: Record<string, unknown>;
  [key: string]: unknown;
}): Promise<Record<string, unknown>> {
  return opsApi.post("/community/discord/action", body);
}

export async function postCommunityLabLog(
  entry: Record<string, unknown>
): Promise<{ ok: boolean }> {
  return opsApi.post("/community/lab/log", entry);
}

// —— Connections hub ——

export type ConnectionsHubBlocker = {
  severity?: string;
  message?: string;
  code?: string;
  operator_detail?: string;
};

export type ConnectionsHubCard = {
  mailbox_id?: string;
  needs_reconnect?: boolean;
  id?: string;
  name?: string;
  ok?: boolean;
  ready?: boolean;
  configured?: boolean;
  connected?: boolean;
  missing?: string[];
  blocker?: ConnectionsHubBlocker | null;
  action?: "connect" | "sync" | "fix" | null;
  connectable?: boolean;
  hint?: string | null;
  note?: string | null;
  docs?: string | null;
  handle?: string | null;
  account_email?: string | null;
  paused_reason?: string | null;
  last_error?: string | null;
  last_sync?: string | null;
  auth_via?: string | null;
  composio_managed?: boolean;
  mode?: string;
};

export type ConnectionsHubStatus = {
  workspace_id: string;
  checked_at?: string;
  ready_count: number;
  total_count: number;
  connections: Record<string, ConnectionsHubCard>;
  order: string[];
  blockers: ConnectionsHubBlocker[];
  note?: string;
};

export async function getConnectionsStatus(workspaceId?: string): Promise<ConnectionsHubStatus> {
  return apiFetch("/connections/status", {}, workspaceId);
}

export async function connectHubProvider(
  provider: string,
  body?: { return_to?: string; intent?: "add" | "reconnect"; mailbox_id?: string },
  workspaceId?: string
): Promise<{
  ok?: boolean;
  provider?: string;
  redirect_url?: string | null;
  authorize_url?: string | null;
  needs_you?: boolean;
  missing?: string[];
  message?: string | null;
  note?: string | null;
  docs?: string | null;
  error?: string;
  open_in_new_tab?: boolean;
  auth_config_id?: string;
  connection_id?: string | null;
}> {
  return apiFetch(`/connections/${provider}/connect`, { method: "POST", body: JSON.stringify(body || {}) }, workspaceId);
}

export async function syncHubProvider(
  provider: string,
  workspaceId?: string
): Promise<Record<string, unknown>> {
  return apiFetch(`/connections/${provider}/sync`, { method: "POST", body: "{}" }, workspaceId);
}

export type GmailVerification = {
  ok: boolean;
  status: "verified" | "not_connected" | "provider_error" | "unconfirmed" | "account_mismatch";
  email: string | null;
  log_id: string | null;
  account_id: string | null;
  receipt_id: string | null;
  receipt_saved: boolean;
};

export async function verifyHubGmail(ventureId?: string, mailboxId?: string, workspaceId?: string): Promise<GmailVerification> {
  return apiFetch("/connections/gmail/verify", { method: "POST", body: JSON.stringify({ venture_id: ventureId || null, ...(mailboxId ? { mailbox_id: mailboxId } : {}) }) }, workspaceId);
}

export async function getGmailMailboxes(workspaceId: string, ventureId?: string): Promise<GmailMailboxes | null> {
  const query = ventureId ? `?venture_id=${encodeURIComponent(ventureId)}` : "";
  let data: unknown;
  try { data = await apiFetch(`/connections/gmail/mailboxes${query}`, {}, workspaceId); }
  catch (error) { if ((error as { status?: number }).status === 404) return null; throw error; }
  return checkedGmailMailboxes(data, workspaceId, ventureId);
}

export async function setDefaultGmailMailbox(workspaceId: string, mailboxId: string | null, ventureId?: string): Promise<void> {
  await apiFetch("/connections/gmail/mailboxes/default", { method: "POST", body: JSON.stringify({ mailbox_id: mailboxId, ...(ventureId ? { venture_id: ventureId } : {}) }) }, workspaceId);
}

export async function disconnectGmailMailbox(workspaceId: string, mailboxId: string, expectedUpdatedAt: string): Promise<void> {
  await apiFetch(`/connections/gmail/mailboxes/${encodeURIComponent(mailboxId)}`, { method: "DELETE",
    body: JSON.stringify({ confirmed: true, expected_updated_at: expectedUpdatedAt }) }, workspaceId);
}

export type GmailVerificationReceipt = {
  receipt_id: string;
  venture_id: string | null;
  checked_at: string | null;
  request: "GMAIL_GET_PROFILE";
  status: GmailVerification["status"];
  account_id: string | null;
  log_id: string | null;
  mail_sent: false;
};

export async function getHubGmailVerification(ventureId?: string): Promise<{ receipt: GmailVerificationReceipt | null }> {
  const query = ventureId ? `?venture_id=${encodeURIComponent(ventureId)}` : "";
  return opsApi.get(`/connections/gmail/verification${query}`);
}
