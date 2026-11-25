export type ResearchNextStep = {
  href: string;
  label: string;
  explanation: string;
  pageChoices?: boolean;
};

export type SavedPlanStatus = "none" | "review" | "saved" | "unknown";

/** A fresh job or changed idea takes precedence over a report selected from history. */
export function researchReportState(state: string, activeJob: boolean, reportRevision?: string, currentRevision?: string | null): string {
  if (activeJob) return "running";
  if (currentRevision && reportRevision && currentRevision !== reportRevision) return "outdated";
  return ["running", "queued", "scheduled", "pending", "leased", "in_progress", "retry_wait"].includes(state) ? "running" : state;
}

/** The plans endpoint lists newest versions first. An unreadable response is not evidence that no plan exists. */
export function savedPlanStatus(data: unknown): SavedPlanStatus {
  if (!data || typeof data !== "object" || !("items" in data) || !Array.isArray(data.items)) return "unknown";
  if (data.items.length === 0) return "none";
  const latest = data.items[0];
  if (!latest || typeof latest !== "object" || typeof latest.id !== "string" || typeof latest.state !== "string") return "unknown";
  return latest.state === "founder_review" ? "review" : "saved";
}

/** Forward guidance; page choices require the caller's current server eligibility. */
export function researchNextStep(ventureId: string, kind: "deep" | "validation", state: string,
  gtmOrder?: "product_first" | "market_first" | null, planStatus: SavedPlanStatus = "none", pageOptional = false): ResearchNextStep {
  const base = `/v/${encodeURIComponent(ventureId)}`;
  if (kind === "validation") {
    return {
      href: `${base}/idea`,
      label: "Continue this idea",
      explanation: "Review the validation, adjust the idea if needed, and confirm it to start deeper research.",
    };
  }
  if (state === "complete" || state === "partial") {
    if (pageOptional) return {
      href: `${base}/site?build=1`, label: "Build the landing page and explore customers", pageChoices: true,
      explanation: "Choose a private landing page preview or find customers first. You can build the landing page later.",
    };
    if (planStatus === "review") return {
      href: `${base}/plan#plan-review`,
      label: "Review execution plan",
      explanation: "Your execution plan is saved. Review its assumptions, scope, and evidence gaps before approval. Missing research sources and unanswered questions remain unverified; reviewing this plan does not require another paid generation.",
    };
    if (planStatus === "saved" || planStatus === "unknown") return {
      href: `${base}/plan`,
      label: "Check execution plan",
      explanation: planStatus === "saved"
        ? "An execution plan is already saved. Check its status and research gaps before deciding whether any further planning is needed."
        : "The saved plan status could not be verified. Open Execution to check for an existing plan before requesting a new generation.",
    };
    if (gtmOrder === "market_first") return {
      href: `/gtm?venture=${encodeURIComponent(ventureId)}`,
      label: "Test demand with a waitlist",
      explanation: "You chose to test demand first. Review this venture's managed waitlist, enable it if needed, and test its public signup before sharing the link. GTM diagnosis is advice, not an acquisition action.",
    };
    return {
      href: `${base}/plan`,
      label: "Open execution planning",
      explanation: "In Planning, save your resource inventory, select Estimate plan generation, review the quote, then choose Generate plan. Building requires a separate reviewed approval.",
    };
  }
  return {
    href: "#research-gaps",
    label: "Review what needs attention",
    explanation: state === "outdated" ? "This report covers an earlier idea. Run research again for the current idea."
      : state === "running" ? "Research is still running. Check its saved progress."
      : "Research didn't finish. Review the saved sources or run research again.",
  };
}
