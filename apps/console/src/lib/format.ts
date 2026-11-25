export function stageLabel(stage?: string | null): string {
  const map: Record<string, string> = {
    draft: "Draft",
    intake: "Starting",
    classifying: "Understanding idea",
    clarifying: "Questions",
    charter_review: "Reviewing plan",
    org_planning: "Building team",
    fork_choice: "Choosing path",
    building: "Building product",
    demand_testing: "Testing demand",
    readiness_review: "Launch review",
    hosting: "Hosting",
    live: "Live",
    operate: "Operating",
    pivot: "Pivoting",
    paused: "Paused",
    archived: "Archived",
  };
  if (!stage) return "New";
  return map[stage] || stage.replace(/_/g, " ");
}

/** Prefer server operational status when available. */
export function ventureStatusLabel(
  stage?: string | null,
  status?: { code?: string; label?: string } | null,
): string {
  if (status?.label) return status.label;
  return stageLabel(stage);
}

export function teamCountLabel(hired?: number | null, working?: number | null): string {
  if (!hired) return "No team yet";
  const team = `${hired} ${hired === 1 ? "teammate" : "teammates"}`;
  return working ? `${working} of ${team} working` : team;
}

export function agentStatusLabel(status?: string | null): string {
  const map: Record<string, string> = {
    active: "Available",
    running: "Working",
    quarantined: "Watching (shadow)",
    shadow: "Watching (shadow)",
    draft: "Ready",
    ready: "Ready",
    paused: "Paused",
    retired: "Retired",
    pending: "Ready",
  };
  if (!status) return "Unknown";
  return map[status] || status.replace(/_/g, " ");
}

export function agentStatusHelp(status?: string | null): string {
  const map: Record<string, string> = {
    active: "Can run tasks for this venture right now.",
    running: "Currently executing a task.",
    quarantined:
      "Assigned to the team but not executing yet. Use Activate team to put them to work.",
    shadow:
      "Assigned to the team but not executing yet. Use Activate team to put them to work.",
    draft: "Configured and waiting for the next work cycle.",
    ready: "Configured and waiting for the next work cycle.",
    paused: "Temporarily stopped. Resume from Overview or Settings.",
    retired: "No longer part of this venture’s active team.",
  };
  if (!status) return "";
  return map[status] || "";
}

export function phaseLabel(phase?: string | null): string {
  const map: Record<string, string> = {
    orient: "Orient",
    understand: "Understand",
    validate: "Idea questions",
    commit: "Commit",
    provision: "Provision",
    build: "Build",
    handoff: "Handoff",
    operate: "Operate",
  };
  if (!phase) return "";
  return map[phase] || phase.replace(/_/g, " ");
}

export const ONBOARDING_PHASES = [
  "orient",
  "understand",
  "validate",
  "commit",
  "provision",
  "build",
  "handoff",
] as const;

export function fieldLabel(key: string): string {
  const map: Record<string, string> = {
    one_liner: "One-line pitch",
    problem: "Problem",
    audience: "Who it's for",
    mvp_scope: "MVP scope",
    explicit_non_goals: "Not in scope",
    success_metric: "Success measure",
    gtm_order: "Go-to-market order",
    monetization: "Monetization",
    brand_tone: "Brand tone",
  };
  return map[key] || key.replace(/_/g, " ");
}

export function sourceLabel(source?: string): string {
  const map: Record<string, string> = {
    answered: "You chose this",
    inferred: "Inferred from your idea",
    default: "Sensible default",
    founder_words: "Your words",
    founder_other: "Your words",
  };
  if (!source) return "";
  return map[source] || source;
}

const BAD_NAMES = new Set([
  "",
  "untitled",
  "idea",
  "new venture",
  "venture",
  "t",
  "x",
  "test",
  "smoke",
  "verify",
]);

/**
 * Prefer server-provided venture.name. shortTitle is a last resort for legacy
 * rows that still lack a real name — never show IDs or single-letter junk.
 */
export function displayName(name?: string | null, idea?: string | null): string {
  const n = (name || "").trim();
  const clipped = n.endsWith("…") || n.endsWith("...");
  if (n.length >= 2 && !clipped && !BAD_NAMES.has(n.toLowerCase()) && !looksLikeId(n)) {
    return n;
  }
  return titleFromIdea(idea) || "Untitled venture";
}

/** @deprecated Prefer displayName / server venture.name. Kept for legacy pages. */
export function shortTitle(name?: string, idea?: string): string {
  const n = (name || "").trim();
  if (n.length >= 3 && !BAD_NAMES.has(n.toLowerCase()) && !looksLikeId(n)) {
    return clip(n, 42);
  }
  const fromIdea = titleFromIdea(idea);
  if (fromIdea) return fromIdea;
  if (n && !looksLikeId(n) && n.length >= 2) return clip(n, 42);
  return "Untitled venture";
}

function looksLikeId(s: string): boolean {
  return /^(ven|idea|chr|org|ag|qst|wo|ht|prop|obr|msg|doc|tsk)_/i.test(s);
}

function titleFromIdea(idea?: string | null): string {
  const raw = (idea || "").trim().replace(/\s+/g, " ");
  if (!raw) return "";
  const quoted = raw.match(/[“"‘']([^”"’']{2,40})[”"’']/);
  if (quoted?.[1]?.trim()) {
    return titleCase(quoted[1].trim().match(/[A-Za-z0-9']+/g) || []).slice(0, 48);
  }
  let text = raw.replace(
    /^(i\s+was\s+thinking\s+of\s+(?:building\s+)?|i(?:'d| would)\s+like\s+(?:to\s+)?|i\s+want\s+(?:to\s+)?|build(?:ing)?\s+|create(?:ing)?\s+|make(?:ing)?\s+)/i,
    "",
  );
  text = text.replace(
    /^(?:an?\s+|the\s+)?(?:platform|marketplace|app|application|saas|tool|product|site|website|service)\s+(?:where|for|that|to|which)\s+/i,
    "",
  );
  text = text.replace(/\b(?:kind of|sort of|basically|just|like|you know)\b/gi, " ");
  text = text.replace(/\s+/g, " ").replace(/^[.,;:—-\s]+|[.,;:—-\s]+$/g, "");
  text = (text.split(/[.!?\n]/)[0] || text).trim();
  const tokens = text.match(/[A-Za-z0-9']+/g) || [];
  const stop = new Set([
    "a", "an", "and", "for", "from", "in", "into", "it", "of", "on", "or",
    "that", "the", "this", "to", "we", "where", "which", "who", "with", "you", "your",
  ]);
  const generic = new Set([
    "app", "application", "friends", "marketplace", "people", "platform", "product",
    "saas", "service", "site", "software", "someone", "something", "system", "thing",
    "tool", "users", "website",
  ]);
  const weak = new Set([
    "build", "create", "get", "give", "help", "join", "let", "make", "sing",
    "start", "take", "use",
  ]);
  let content = tokens.filter((t) => !stop.has(t.toLowerCase()) && !generic.has(t.toLowerCase()));
  if (!content.length) content = tokens.filter((t) => !stop.has(t.toLowerCase()));
  if (content.length > 1 && weak.has(content[0].toLowerCase())) content = content.slice(1);
  const nouns = content.filter((t) => !t.toLowerCase().endsWith("ing"));
  const picked = (nouns.length ? nouns : content).slice(0, 2);
  if (!picked.length) return titleCase(tokens.slice(0, 2)).slice(0, 48);
  if (picked.length >= 2 && picked[0].length + picked[1].length <= 12) {
    return titleCase([picked[0] + picked[1].toLowerCase()]).slice(0, 48);
  }
  return titleCase(picked).slice(0, 48);
}

function titleCase(words: string[]): string {
  return words
    .filter(Boolean)
    .map((w) => (w[0] === w[0].toLowerCase() ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

function clip(s: string, max: number): string {
  if (s.length <= max) return s;
  let cut = s.slice(0, max - 1).trimEnd();
  const space = cut.lastIndexOf(" ");
  if (space > 12) cut = cut.slice(0, space).replace(/[.,;:—-]+$/, "");
  return `${cut}…`;
}

export function relativeTime(iso?: string | null): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  const m = Math.round(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

export function formatUsd(n?: number | null, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "$0";
  return `$${n.toFixed(digits)}`;
}

export function formatTokens(n?: number | null): string {
  const value = Number(n || 0);
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value < 1000) return String(Math.round(value));
  if (value < 1_000_000) {
    const k = value / 1000;
    return `${k < 10 ? k.toFixed(1) : Math.round(k)}k`;
  }
  return `${(value / 1_000_000).toFixed(2)}M`;
}

export function formatElapsed(ms?: number | null): string {
  if (ms == null || ms < 0) return "0s";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return `${m}m ${rem}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function pausedReasonLabel(reason?: string | null): string {
  const map: Record<string, string> = {
    awaiting_product_understanding: "What do you actually mean? Answer below.",
    awaiting_product_brief: "Check the product I heard, then I research that.",
    awaiting_clarification: "A few questions to test your idea.",
    awaiting_gstack_plan: "I worked out the plan from your answers.",
    awaiting_builder_choice: "I want to build the landing page. Pick how.",
    awaiting_builder_session: "Landing page is still getting built.",
    awaiting_approval: "Waiting for your approval.",
    awaiting_credential: "A provider connection is needed.",
    rate_limited: "Paused briefly due to rate limits.",
    kill_switch: "Stopped. Kill switch is on.",
  };
  if (!reason) return "Paused. Action needed.";
  return map[reason] || reason.replace(/_/g, " ");
}

export function isLandingBuildPause(reason?: string | null): boolean {
  return reason === "awaiting_builder_choice" || reason === "awaiting_builder_session";
}

export function landingPreviewKind(input: {
  failed?: string | null;
  pausedReason?: string | null;
  building?: boolean;
  empty?: boolean;
  reachable?: boolean;
}): "failed" | "building" | "ready" {
  if (input.reachable) return "ready";
  if (input.failed) return "failed";
  if (input.building || isLandingBuildPause(input.pausedReason) || input.empty) return "building";
  return "ready";
}

export function isReachableLandingUrl(
  url?: string | null,
  opts?: { allowLoopback?: boolean },
): boolean {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "file:") return false;
    const host = parsed.hostname.toLowerCase();
    const loopback = host === "localhost" || host === "127.0.0.1" || host === "0.0.0.0" || host === "[::1]";
    if (loopback) {
      return Boolean(opts?.allowLoopback) && (parsed.protocol === "http:" || parsed.protocol === "https:");
    }
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}
