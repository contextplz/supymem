export type PlanReviewState = {
  state: string;
  generation_state: string;
  reviewable: boolean;
  stale_dependencies: boolean;
  newer_plan_id?: string | null;
  review_findings?: { code: string; path: string; message: string }[];
};

export function groupPlanFindings(findings: NonNullable<PlanReviewState["review_findings"]>) {
  const grouped = new Map<string, { code: string; message: string; paths: string[] }>();
  for (const finding of findings) {
    const key = `${finding.code}\u0000${finding.message}`;
    const existing = grouped.get(key);
    if (existing) existing.paths.push(finding.path);
    else grouped.set(key, { code: finding.code, message: finding.message, paths: [finding.path] });
  }
  return [...grouped.values()];
}

// Server paths use proposal.foo[0].bar. List items share one multiline editor.
export function findingInSection(path: string, section: string): boolean {
  return path === section || path.startsWith(`${section}.`) || path.startsWith(`${section}[`);
}

export function editableFindingPath(path: string, available: readonly string[]): string | null {
  let candidate = path;
  while (candidate.startsWith("proposal.")) {
    if (available.includes(candidate)) return candidate;
    const shorter = candidate.replace(/\[\d+\]$/, "");
    if (shorter !== candidate) { candidate = shorter; continue; }
    const dot = candidate.lastIndexOf(".");
    if (dot <= "proposal".length) break;
    candidate = candidate.slice(0, dot);
  }
  return null;
}

export function planReviewAction(plan: PlanReviewState): "approved" | "revise" | "review" {
  if (plan.state === "approved" || plan.state === "executing") return "approved";
  if (plan.review_findings?.length || plan.stale_dependencies || plan.newer_plan_id ||
      !plan.reviewable || plan.generation_state !== "completed") return "revise";
  return "review";
}
