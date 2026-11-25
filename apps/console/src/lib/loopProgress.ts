/** Display only bounded progress, never raw provider diagnostics or credentials. */
export type LoopIteration = { index: number; phase: string; outcome: string; summary?: string;
  diagnosis?: { failures?: unknown[]; tap_summary?: { pass: number; fail: number } }; caps_used?: { iterations?: number; wall_seconds?: number } };
export type LoopEvent = { at: number | string; text: string };
export type ChangedFile = { path: string; deleted?: boolean };
export type ProductBuildWatch = { state: string; task_verified?: boolean; failure_code?: string; build_deadline_at?: number;
  evidence?: { processes_stopped?: boolean; outcome?: string;
    product_build?: { outcome?: string; changed_files?: ChangedFile[] } } };

export function codingToolLabel(provider: string): string {
  return ({ claude: "Claude Code", codex: "Codex", cursor: "Cursor", opencode: "OpenCode" } as Record<string, string>)[provider]
    || "your coding tool";
}

export function buildStageLabel(stage: string, provider = "", state = ""): string {
  if (["done", "succeeded", "ready"].includes(state)) return "Ready";
  if (["cancelled", "stopped"].includes(state)) return "Stopped";
  if (["failed", "failed_dead", "poison", "needs_you", "reconciliation_required"].includes(state)) return "Needs you";
  if (state === "stopping") return "Stopping";
  if (["Writing the brief", "Checking the page", "Ready", "Stopped", "Needs you", "Stopping"].includes(stage)) return stage;
  if (/^(?:Codex|Claude Code|Cursor|OpenCode) is building$/.test(stage)) return stage;
  if (/^(?:brief|direction|plan|queued|pending|approved|waiting_for_device|not_started)\b/.test(stage.replace(/[_:]+/g, " ")))
    return "Writing the brief";
  if (/^(?:check|checking|critiqu|render|validat|test)/.test(stage)) return "Checking the page";
  return provider ? `${codingToolLabel(provider)} is building` : "Your coding tool is building";
}

export function productBuildStatus(watch?: ProductBuildWatch): "ready" | "running" | "stopping" | "stop_unconfirmed" | "stopped" | "cancelled" | "failed" | "finished" | "missing" | "signed_out" | "disconnected" {
  if (watch?.state === "stopping") return "stopping";
  if (["reconciliation_required", "interrupted_reconciliation_required"].includes(watch?.state || "")
    || watch?.state === "stopped" && watch.evidence?.processes_stopped !== true) return "stop_unconfirmed";
  const code = watch?.failure_code || "";
  if (["coding_provider_not_installed", "coding_cli_missing"].includes(code)) return "missing";
  if (["coding_sign_in_required", "coding_subscription_required"].includes(code)) return "signed_out";
  if (["device_authentication_required", "device_order_device_missing", "device_order_account_changed"].includes(code)) return "disconnected";
  if (watch?.state === "stopped") {
    const outcome = watch.evidence?.product_build?.outcome || watch.evidence?.outcome;
    if (outcome === "stopped" || outcome === "cancelled") return "stopped";
    return !code && watch.task_verified === true && ["pass", "plan_ready"].includes(outcome || "") ? "finished" : "failed";
  }
  if (watch?.state === "cancelled") return "cancelled";
  if (code) return "failed";
  return !watch || ["pending", "approved", "not_started", "waiting_for_device"].includes(watch.state) ? "ready" : "running";
}

export function safeLoopText(value: unknown, maximum = 200): string {
  if (typeof value !== "string") return "";
  let raw: string = value;
  const finding = raw.match(/\b(?:pagebuilder_plan_copy_rejected|honest_copy):\s*(\[[\s\S]*)$/);
  if (finding) {
    let reasons: string[] = [];
    try {
      const findings: unknown = JSON.parse(finding[1]);
      reasons = Array.isArray(findings) ? findings.flatMap(item => item && typeof item.reason === "string" ? [item.reason] : []) : [];
    } catch {}
    raw = raw.slice(0, raw.indexOf(finding[1])) + (reasons.join("; ") || "Your page text needs a correction")
      + ". On Website, request the fix and choose Retry.";
  }
  raw = raw.replace(/sections(?:\.\d+|\[\d+\])\.headline: String should have at most (\d+) characters/g,
    "Your page heading is too long. Shorten it to $1 characters or fewer. On Website, request the fix and choose Retry.")
    .replace(/reviewed_body_missing:[\w-]+/g, "Your page is missing text from your reviewed plan. On Website, request the fix and choose Retry.")
    .replace(/\b(?:brief|sections|controller|facts)(?:\[\d+\]|\.[\w]+)+/g, "your reviewed page text");
  const text = raw.replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "").replace(/[\u0000-\u001f\u007f]/g, " ").replace(/—/g, ", ")
    .replace(/https?:\/\/[^\s]+/g, "a website").trim();
  const labels: Record<string, string> = {
    pagebuilder_plan_copy_rejected: "Your page text does not match your confirmed offer",
    pagebuilder_qa_failed: "Your page did not pass its checks",
    pagebuilder_check_failed: "Your page could not be checked",
    approved_action_id_missing: "Your page is missing a reviewed button",
    approved_action_missing_or_ambiguous: "Your page has a missing or repeated button",
    required_text_missing: "Your page is missing reviewed text",
    honest_copy: "Your page contains unsupported text",
    customer_copy: "Your page layout does not match your reviewed sections",
    unapproved_controls: "Your page has a control you did not request",
    hosted_waitlist_unwired: "Your signup link is not connected",
    inline_form_backend_unsupported_use_hosted_consent: "Your signup form is not connected",
    local_signup_changed: "Your signup form does not match your saved consent and destination",
    coding_sandbox_denied: "Your tool tried to change a protected file",
    coding_processes_unconfirmed: "Your tool did not stop before the page checks",
  };
  const plain = text.replace(new RegExp(`\\b(?:${Object.keys(labels).join("|")})\\b`, "g"), code => labels[code])
    .replace(/\b(?:pagebuilder|coding|device)_[a-z0-9_]+\b/g, "A build check needs a fix");
  if (/\b(?:job_|dex_|Error\b)|[A-Za-z0-9_-]{32,}|(?:api[_ -]?key|password|secret|token|authorization|credential)["']?\s*[:=]|--(?:api[_-]?key|password|token|secret)\s|\b(?:sk-|ghp_|AKIA|Bearer\s|Basic\s)|-----BEGIN.*PRIVATE KEY-----/i.test(plain))
    return "A private detail was removed from this update.";
  return plain
    .replace(/(^|[\s="'(:`])(?:file:\/\/)?(?:\/(?:Users|home|private|tmp|var|root)\/|\/?[A-Za-z]:[\\/])[^\s]+/g, "$1a local file")
    .slice(0, maximum);
}

export function safeChangedFiles(files: ChangedFile[] | undefined): ChangedFile[] {
  if (!Array.isArray(files)) return [];
  return files.filter(file => typeof file?.path === "string" && file.path.length <= 240
    && !file.path.startsWith("/") && !file.path.includes("\\") && !/^[A-Za-z]:/.test(file.path)
    && !file.path.split("/").some(part => !part || part === "." || part === ".."
      || /^(?:\.env(?:\..*)?|\.git|.*(?:secret|credential).*|.*\.(?:pem|key))$/i.test(part))
    && safeLoopText(file.path, 240) === file.path).slice(0, 100);
}
const OUTCOMES: Record<string, string> = {
  pass: "Local checks passed", tests_failed: "Checks still failing, fixing", build_error: "The app needs another fix",
  no_change: "No changes were made", timeout: "The time limit was reached", cli_error: "The coding tool stopped",
  quota_paused: "Your coding plan's usage limit was reached", auth_required: "Sign in to your coding tool in Terminal",
  sandbox_denied: "A change outside the approved access was blocked", secret_detected: "A private detail was found, so the run stopped",
  stopped: "Stopped safely", no_progress: "The same checks kept failing, so the run stopped",
  plan_ready: "Your build plan is ready for review", plan_invalid: "The plan needs correction before coding",
  stopped_unclean: "Your tool did not stop cleanly; your last saved version was restored",
  checks_unavailable: "Install Node in Terminal before running local checks",
};
export function loopIterationLabel(row: LoopIteration, maximum: number): string {
  const prefix = row.phase === "plan" ? "Plan" : `Try ${Math.min(Math.max(1, row.index + 1), maximum)} of ${maximum}`;
  return `${prefix}: ${OUTCOMES[row.outcome] || "Review the saved result"}`;
}
