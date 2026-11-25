/** UI-facing API shapes for Phase 9. Prefer generated types when make types lands. */

export type OnboardingStatus =
  | "pending"
  | "running"
  | "paused"
  | "done"
  | "failed"
  | "cancelled";

export type ActivityStatus = "running" | "done" | "failed" | "skipped" | "info";

export type ActivityLine = {
  seq: number;
  phase?: string;
  kind?: string;
  status: ActivityStatus | string;
  text: string;
  query?: string | null;
  detail?: string[] | null;
  evidence_url?: string | null;
  undo?: { label: string; href: string } | null;
  cost_usd?: number | null;
  ts?: string;
  step_id?: string;
};

export type RunSummary = {
  status: OnboardingStatus | string;
  phase?: string;
  phases_done?: number;
  phases_total?: number;
  cost_usd?: number;
  elapsed_ms?: number;
};

export type FounderAction = {
  label: string;
  href?: string;
};

export type OnboardingState = {
  run_id?: string;
  status: OnboardingStatus | string;
  mode?: "guided" | "auto" | string;
  phase?: string;
  phases_done?: number;
  phases_total?: number;
  paused_reason?: string | null;
  founder_action?: FounderAction | null;
  cost_usd?: number;
  elapsed_ms?: number;
  steps?: Array<{
    step_id: string;
    phase: string;
    status: string;
    duration_ms?: number;
  }>;
  current_step?: { step_id: string; phase: string; status: string } | null;
  flags?: { unvalidated?: boolean };
};

export type Venture = {
  id: string;
  workspace_id?: string;
  name: string;
  stage: string;
  status?: { code?: string; label?: string; detail?: string };
  raw_idea?: string;
  one_liner?: string;
  created_at?: string;
  updated_at?: string;
};

export type ClarifyOption = {
  id: string;
  label: string;
  detail?: string;
  recommended?: boolean;
  implication?: string;
};

export type ClarifyQuestion = {
  id: string;
  slot_key?: string;
  prompt?: string;
  text?: string;
  why_asked?: string;
  implication?: string;
  allow_other?: boolean;
  selection_mode?: "single" | "multiple";
  exclusive_option_ids?: string[];
  options: ClarifyOption[];
  gstack_id?: string;
  gstack_name?: string;
  process?: string;
};

export type GStackStage = {
  id: string;
  name: string;
  slot_key?: string;
  answer?: string;
  implication?: string;
};

export type ProductBrief = {
  process?: string;
  name?: string;
  idea?: string;
  one_liner?: string;
  what_it_is?: string;
  what_it_is_not?: string;
  first_user?: string;
  first_job?: string;
  today?: string;
  expertise?: string;
  market?: string;
  understood?: string;
  research_query?: string;
};

export type GStackPlan = {
  process?: string;
  product?: string;
  product_brief?: ProductBrief;
  title?: string;
  verdict?: string;
  verdict_label?: string;
  thesis?: string;
  wedge?: string;
  first_user?: string;
  status_quo?: string;
  observation?: string;
  future_fit?: string;
  wont_build?: string[];
  next_five?: string[];
  stages?: GStackStage[];
  inferred?: Record<string, string | string[]>;
  principles?: string[];
};

export type ClarifyRound = {
  id?: string;
  index?: number;
  process?: string;
  stages?: GStackStage[];
  questions: ClarifyQuestion[];
};

export type ClarifyAnswer = {
  option_id?: string;
  option_ids?: string[];
  other_text?: string;
};

export type TileState = "ready" | "empty" | "loading" | "degraded" | "error" | string;

export type NextUpTileData = {
  state: TileState;
  headline?: string;
  tasks?: Array<{
    id: string;
    title: string;
    why_now?: string;
    est_minutes?: number;
    tags?: string[];
    runnable?: boolean;
  }>;
};

export type WebsiteTileData = {
  state: TileState;
  url?: string;
  simulated?: boolean;
  published?: boolean;
  provider?: string;
  upsell?: { label: string; href: string };
  screenshot_url?: string;
  stage?: string;
  version?: number;
  interest_count?: number;
  interest_mode?: "live" | "simulated" | "no_data" | string;
  note?: string;
};

export type ValidationTileData = {
  state: TileState;
  questions_answered?: number;
  rounds?: number;
  demand?: string;
  confidence?: number;
  recommendation?: string;
  competitors?: string[];
  queries?: string[];
  unvalidated?: boolean;
};

export type TeamTileData = {
  state: TileState;
  active?: number;
  quarantined?: number;
  hired?: number;
  working?: number;
  watching?: number;
  ready?: number;
  paused?: number;
  note?: string;
  roles?: string[];
};

export type TasksTileData = {
  state: TileState;
  open?: number;
  running?: number;
  done_7d?: number;
};

export type CreditBalance = {
  granted?: number;
  available?: number;
  spent?: number;
  reserved?: number;
  status?: string;
  credits_per_usd?: number;
};

export type CostTileData = {
  state: TileState;
  today_usd?: number;
  onboarding_usd?: number;
  cap_usd?: number;
  top_purpose?: string;
  prompt_tokens_total?: number;
  completion_tokens_total?: number;
  total_tokens?: number;
  estimate_usd_total?: number;
  estimate_usd_month?: number;
  prompt_tokens_month?: number;
  completion_tokens_month?: number;
  cached_tokens_total?: number;
  reasoning_tokens_total?: number;
  calls?: number;
  last_run_usd?: number | null;
  last_run_at?: string | null;
  forecast?: CostForecast;
  all_time?: TokenUsage["all_time"];
  this_month?: TokenUsage["this_month"];
  credits?: CreditBalance;
  byok?: TokenUsage["byok"];
  platform?: TokenUsage["platform"];
  last_run_source?: TokenUsage["last_run_source"];
};

export type CostForecast = {
  possible?: boolean;
  kind?: string;
  confidence?: "none" | "thin" | "steady" | string;
  basis?: string;
  window_days?: number;
  calls_in_window?: number;
  daily_usd?: number;
  month_end_usd?: number;
  next_30d_usd?: number;
  days_remaining_in_month?: number;
  rates_as_of?: string;
  current_model?: string | null;
  current_rates?: {
    model?: string;
    input_per_mtok?: number;
    output_per_mtok?: number;
  } | null;
  what_if?: Array<{ model: string; next_30d_usd: number; current?: boolean }>;
  note?: string;
};

export type TokenUsage = {
  month?: string;
  prompt_tokens_total?: number;
  completion_tokens_total?: number;
  total_tokens?: number;
  estimate_usd_total?: number;
  estimate_usd_month?: number;
  prompt_tokens_month?: number;
  completion_tokens_month?: number;
  cached_tokens_total?: number;
  reasoning_tokens_total?: number;
  calls?: number;
  last_run_usd?: number | null;
  last_run_at?: string | null;
  forecast?: CostForecast;
  all_time?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    estimate_usd?: number;
    calls?: number;
    cached_tokens?: number;
    reasoning_tokens?: number;
  };
  this_month?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    estimate_usd?: number;
    calls?: number;
  };
  credits?: CreditBalance;
  byok?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    estimate_usd?: number;
    calls?: number;
  };
  platform?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    estimate_usd?: number;
    calls?: number;
  };
  last_run_source?: "byok" | "platform" | string | null;
};

export type DocumentsTileData = {
  state: TileState;
  items?: Array<{ id: string; kind?: string; title: string; updated_at?: string }>;
};

export type EmailTileData = {
  state: TileState;
  address?: string;
  simulated?: boolean;
  outbox?: number;
  sent?: number;
};

export type SocialTileData = {
  state: TileState;
  drafts?: number;
  posted?: number;
  channels?: string[];
};

export type BusinessTileData = {
  state: TileState;
  visitors_7d?: number;
  signups_7d?: number;
  revenue_usd?: number;
  note?: string;
};

export type EvolutionTileData = {
  state: TileState;
  cycles?: number;
  proposals_open?: number;
  last_cycle_at?: string | null;
};

export type GovernanceBlock = {
  autonomy?: {
    level?: string;
    label?: string;
    description?: string;
  };
  kill_switch?: boolean;
  pending_approvals?: number;
  caps?: {
    spend_daily_usd?: number;
    spend_used_usd?: number;
    agents_max?: number;
    agents_active?: number;
  };
};

export type ResearchBlock = {
  available?: boolean;
  href?: string | null;
};

export type ControlBlock = {
  you_own_this?: boolean;
  paused?: boolean;
  domain_policy?: string;
  posts_need_you?: boolean;
  landing?: { url?: string | null; simulated?: boolean; href?: string };
  plan_href?: string;
  research_href?: string | null;
  work_href?: string;
  now_href?: string;
  settings_href?: string;
  export_path?: string;
  support_path?: string;
  files?: string;
  credits?: CreditBalance | null;
};

export type DashboardPayload = {
  venture: Venture;
  onboarding?: OnboardingState;
  governance?: GovernanceBlock;
  research?: ResearchBlock;
  control?: ControlBlock;
  needs_you?: Array<{
    id: string;
    title: string;
    why?: string | null;
    provider?: string | null;
    href?: string | null;
    action_label?: string | null;
    steps?: Array<{ text?: string; url?: string | null }>;
  }>;
  tiles?: {
    next_up?: NextUpTileData;
    tasks?: TasksTileData;
    website?: WebsiteTileData;
    external_site?: { url: string; note: string; verified?: boolean; host?: string | null } | null;
    documents?: DocumentsTileData;
    email?: EmailTileData;
    social?: SocialTileData;
    business?: BusinessTileData;
    team?: TeamTileData;
    validation?: ValidationTileData;
    cost?: CostTileData;
    evolution?: EvolutionTileData;
  };
  etag?: string;
};

export type MoneyProvenance = {
  ledger_entries?: number;
  sources?: Record<string, number>;
  stripe_refs?: Array<string | null>;
  reason?: string;
};

export type MoneyPayload = {
  earned_minor?: number | null;
  gross_received_minor?: number | null;
  minor_unit_exponent?: number | null;
  currency?: string | null;
  mode?: "live" | "simulated" | "no_data" | string | null;
  charges?: unknown[];
  provenance?: MoneyProvenance;
};

export type OrgAgentWork = {
  did?: string;
  doing?: string;
  next?: string;
  live_state?: string | null;
};

export type OrgAgent = {
  id: string;
  name?: string;
  role?: string;
  title?: string;
  status?: string;
  work?: OrgAgentWork;
};

export type OrgPayload = {
  agents?: OrgAgent[];
  summary?: {
    hired?: number;
    working?: number;
    watching?: number;
    ready?: number;
    paused?: number;
  };
  stage?: string;
};

export type AgentLiveItem = {
  agent_id: string;
  state?: string;
  current_task_desc?: string | null;
  last_heartbeat?: string | null;
  blocked_reason?: string | null;
};

export type ChatConfirmation = {
  state?: "starting" | "started" | "not_started" | "denied";
  action?: string;
  capability?: string;
  interpretation?: string;
  consequences?: string[];
  resulting_state?: Record<string, unknown>;
  cancel_label?: string;
};

export type ExperimentThreshold = {
  op: string;
  value: number;
  metric?: string;
  min_events?: number;
};

export type ExperimentSpec = {
  id: string;
  venture_id: string;
  state: string;
  title?: string;
  hypothesis?: string;
  scan?: string;
  catalog_key?: string;
  runner?: string;
  approval?: string;
  metric?: string;
  min_sample?: number;
  max_duration_days?: number;
  founder_minutes?: number;
  ceiling_micro?: number;
  approach: "sensible" | "red-pill";
  contacts_people: boolean;
  success_threshold?: ExperimentThreshold;
  failure_threshold?: ExperimentThreshold;
};

export type ExperimentAutopilot = {
  venture_id: string;
  experiments: Array<{
    id: string;
    venture_id: string;
    hypothesis: string;
    state: string;
    spec: ExperimentSpec;
    started_at: string | null;
    outcome: "supported" | "refuted" | "inconclusive" | null;
    collected: number | null;
    n_min: number | null;
    review_at: string | null;
    result: { outcome?: string; measured: { events?: number } } | null;
  }>;
  proposed_experiments: ExperimentSpec[];
  decision_recommendation: { evidence_line: string; actions: NonNullable<ChatMessage["actions"]> } | null;
};

export type InboxBriefingSettings = {
  windows: string[];
  quiet_hours: { start: string; end: string };
  email_copy: boolean;
};

export type InboxBriefingItem = {
  id: string;
  from: string;
  subject: string;
  reason: string;
  category: "important" | "worth_a_look" | "unclassified";
  actions: NonNullable<ChatMessage["actions"]>;
};

export type InboxBriefingPayload = {
  items: InboxBriefingItem[];
  filtered: { newsletters: number; notifications: number };
};

export type CosBriefPayload = {
  week_of: string;
  sections: Array<{ key: string; title: string; items: Array<{ id: string; label: string; href: string }> }>;
  next_move: NonNullable<ChatMessage["actions"]>[number] | null;
  alternatives: NonNullable<ChatMessage["actions"]>;
};

export type InboxBriefingAction =
  | { action: "inbox.briefing.consent"; payload: { mailbox_id: string; consent: boolean } }
  | { action: "inbox.briefing.settings"; payload: InboxBriefingSettings }
  | { action: "inbox.item.feedback"; payload: { item_id: string; category: "important" | "ignore" } };

export type InboxBriefingUiConfig = {
  inbox_briefing_v1?: boolean;
  inbox_briefing?: {
    settings: InboxBriefingSettings;
    mailboxes: Array<{ mailbox_id: string; email: string; status: string; consent: boolean }>;
  };
};

export type InboxBriefingMessage = ChatMessage & { kind: "inbox_briefing" | "inbox_urgent" };

export type ChatMessage = {
  run_id?: string;
  next_moves?: { kind: "next_moves"; body_md: string; stage: string; state_hash: string; actions: ChatMessage["actions"] };
  gtm_search_progress?: { job_id?: string | null; run_id?: string | null; audience?: string | null; state?: string | null };
  confirmed_action?: { message_id: string; action: string; run_id?: string; state?: "starting" | "started" | "not_started" };
  id: string;
  role: "assistant" | "founder" | "user" | "system";
  body_md?: string;
  text?: string;
  kind?: string;
  payload?: InboxBriefingPayload | Record<string, unknown>;
  /** Nonce of the send request that produced this message (set locally; saved twin may echo it). */
  client_request_id?: string;
  reply_to_message_id?: string;
  reply_request_ids?: string[];
  citations?: Array<{ label?: string; href?: string; question?: boolean; inbox_briefing?: InboxBriefingPayload; cos_brief?: CosBriefPayload; job_started?: { job_id: string };
    browse_progress?: { job_id: string; state: string; percent: number; step_label: string; started_at: string | null } }>;
  founder_references?: {
    startups: Array<{ id: string; name: string; what_they_did: string; outcome: string; lesson: string; url: string; relevance_reason: string; important_difference: string }>;
    yc_advice: Array<{ id: string; title: string; line: string; url: string }>;
  };
  actions?: Array<{
    label: string;
    href?: string;
    action?: string;
    name?: string;
    task_id?: string;
    text?: string;
    payload?: Record<string, unknown>;
    style?: "primary" | "ghost" | "secondary";
    disabled_reason?: string;
    state_hash?: string;
    reply_choice?: boolean;
    confirmation?: ChatConfirmation;
  }>;
  /** Ordered reply parts; "diagram" parts carry a server-rendered SVG shown by DiagramCard. */
  parts?: Array<{ type: string; text?: string; title?: string; svg?: string }>;
  ts?: string;
  created_at?: string | null;
};

export type ConnectionState = "live" | "reconnecting" | "offline" | "idle";
