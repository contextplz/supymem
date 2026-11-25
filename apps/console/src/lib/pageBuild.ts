/** The Website "Build your page" card: Claude Code / Codex builds the page from the approved plan. */
export type PageBuildTool = { name: string; label: string; installed: boolean; cli_path?: string | null };
export type PageBuildOrder = { id: string; title?: string; state?: string; review_path?: string;
  task_id?: string; plan_id?: string; artifact_profile?: string };
export type PagePublication = { run_id: string; state: "checking" | "publishing" | "live" | "failed";
  live_url: string | null; error_code: string | null; retryable: boolean; job_attempt: number | null;
  error_reason?: string | null; failure_code?: string | null; qa_report_available?: boolean;
  /** What the founder can do after a failure; older servers omit it. */
  next_action?: "retry_publication" | "publish_again" | "build_new_version" | null };
export type PageBuilderProfile = {
  server_admission_enabled?: boolean;
  renderer_ready?: boolean;
  task_id: string | null; plan_id: string | null; artifact_profile: "static_landing_v1";
  quote: { ready: boolean; credits: number; quote_fingerprint: string; payer?: string; reason?: string | null } | null;
  current: { job_id: string; state: string; stage?: string; progress?: number; queue_state?: string; queue_attempt?: number; consecutive_failures?: number; error?: string; error_reason?: string; native_ready: boolean;
    server_publish_ready?: boolean; server_candidates?: { variant: number; direction?: { family?: string } }[];
    publication?: PagePublication | null; watch_url?: string;
    original_creator_required?: boolean; improvement_available?: boolean; improvement_reason?: string; image_note?: string | null;
    receipt?: { spent_credits?: number }; candidates: { variant: number; direction?: { family?: string } }[] } | null;
};
export type PageBuildView =
  | { kind: "connect" }
  | { kind: "ready"; tool: "claude" | "codex" }
  | { kind: "approve"; order: PageBuildOrder }
  | { kind: "working"; order: PageBuildOrder }
  | { kind: "stopped"; order: PageBuildOrder }
  | { kind: "failed"; order: PageBuildOrder };

/** Claude Code first, then Codex; Cursor cannot run reviewed background builds. */
export function pickTool(tools: PageBuildTool[]): "claude" | "codex" | null {
  for (const name of ["claude", "codex"] as const) {
    if (tools.some((t) => t.name === name && t.installed && t.cli_path)) return name;
  }
  return null;
}

/** The exact phrase the server classifies as a reviewed coding task for the current plan task. */
export function buildInstruction(tool: "claude" | "codex", taskId?: string): string {
  return `Use ${tool === "claude" ? "Claude" : "Codex"} to build the approved landing page${taskId ? ` for task ${taskId}` : ""}`;
}

/** Newest coding order decides the card; orders arrive newest first. */
export function pageBuildView(tool: "claude" | "codex" | null, orders: PageBuildOrder[], taskId?: string): PageBuildView {
  const order = orders.find((o) => o.artifact_profile === "static_landing_v1" && (!taskId || o.task_id === taskId));
  if (order?.state === "pending") return { kind: "approve", order };
  if (order?.state === "approved" || order?.state === "claimed") return { kind: "working", order };
  if (order?.state === "stopped") return { kind: "stopped", order };
  if (order && (order.state === "cancelled" || order.state === "expired") && tool) return { kind: "failed", order };
  return tool ? { kind: "ready", tool } : { kind: "connect" };
}
