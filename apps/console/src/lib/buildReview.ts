export type BuildReview = {
  status: string; review_origin?: "founder" | "godmode"; decision?: "accept" | "reject"; source_artifact_id?: string;
  report_digest?: string; qa_id?: string; contract_digest?: string; feedback?: string;
};
export type ReviewBuild = {
  id: string; app_id?: string; artifact_profile?: string; runtime_version?: number; lifecycle_version?: number; state: string;
  mode?: string; verified_source_import?: boolean; source_custody_verified?: boolean;
  verified_execution?: boolean; completion_status?: string; artifact_id?: string | null;
  qa_run_id?: string | null; qa_state?: string; review?: BuildReview | null;
  publication_required?: boolean; task_completion?: { state: string } | null;
  review_readiness?: { schema_version: number; status: string; code: string | null; error_code?: string | null } | null;
  landing_page?: { available: boolean; live_url: string | null; qa_note: string | null; qa_report_available?: boolean } | null;
};
export type QaReceipt = {
  id: string; runtime_version: number; state: string; verified_report: boolean;
  verified_execution: boolean; verified_source_import?: boolean; passed: number | null; failed: number | null;
  unsupported: number | null; source_artifact_id: string; source_sha256: string;
  report_digest: string | null;
};
export type QaReport = {
  schema_version: number; qa_run_id: string; build_run_id: string; source_artifact_id: string;
  source_sha256: string; contract_digest: string; outcome: string; exclusions: string[];
  cases: { id: string; suite: string; target: string; status: string; observed: string; expected: string }[];
  evidence: { id: string; media_type: string; sha256: string; size_bytes: number }[];
};

export function attentionChecks(cases: QaReport["cases"]): { label: string; message: string; count: number }[] {
  const groups = new Map<string, { label: string; message: string; count: number }>();
  for (const item of cases.filter(item => item.status !== "passed")) {
    const target = (item.target || "").replace(/[_-]/g, " ");
    const label = /hero cta|main cta/.test(target) ? "Main button"
      : /^control \d+$/.test(target) ? "A button or link"
      : item.suite === "runtime" ? "Page checks"
      : item.suite === "forms" ? "Signup or contact form"
      : item.suite === "actions" || item.suite === "coverage" ? "A button or link" : "Your page";
    const message = /no declared meaningful action/.test(item.observed) ? "doesn't do anything yet."
      : /absent, ambiguous, hidden or disabled/.test(item.observed) ? "we couldn't find or click it."
      : /keyboard|Tab/.test(item.observed) ? "we couldn't use it with the keyboard."
      : item.suite === "runtime" ? "The check runner stopped before it could finish."
      : /heading.*missing/i.test(item.observed) ? "The main heading is missing."
      : /overflow|layout/i.test(item.observed) ? "Some content doesn't fit on the screen."
      : item.suite === "forms" ? "We couldn't confirm visitors can complete this form."
      : item.suite === "actions" ? "We couldn't confirm it works when clicked."
      : "We couldn't finish checking this part of the page.";
    const key = `${label}:${message}:${item.target || item.suite}`;
    const group = groups.get(key);
    if (group) group.count++;
    else groups.set(key, { label, message, count: 1 });
  }
  return [...groups.values()];
}

export function completedProofMatches(build: ReviewBuild, qa: QaReceipt): boolean {
  if (build.lifecycle_version === 2) return build.verified_execution === true && qa.verified_execution === true;
  return build.lifecycle_version === 3 && (build.mode === "subscription_static_source_qa" || build.mode === "pagebuilder_source_qa") &&
    build.verified_execution === false && qa.verified_execution === false &&
    build.verified_source_import === true && build.source_custody_verified === true && qa.verified_source_import === true;
}

export function reviewMatches(build: ReviewBuild, qa: QaReceipt | null, report: QaReport | null): boolean {
  const review = build.review;
  return build.runtime_version === 1 && (build.lifecycle_version === 2 || build.lifecycle_version === 3) && build.state === "succeeded" &&
    !!qa && completedProofMatches(build, qa) && build.completion_status === "closed" && build.qa_state === "passed" &&
    !!build.artifact_id && review?.status === "pending" && review.source_artifact_id === build.artifact_id &&
    !!qa && qa.runtime_version === 1 && qa.verified_report === true &&
    qa.state === "passed" && qa.id === build.qa_run_id && qa.id === review.qa_id &&
    qa.source_artifact_id === build.artifact_id && !!qa.report_digest && qa.report_digest === review.report_digest &&
    !!report && report.schema_version === 1 && report.qa_run_id === qa.id && report.build_run_id === build.id &&
    report.source_artifact_id === build.artifact_id && report.source_sha256 === qa.source_sha256 &&
    report.contract_digest === review.contract_digest && report.outcome === "passed" &&
    report.cases.length > 0 && report.cases.every(item => item.status === "passed");
}

/** Current server check is distinct from immutable QA. POST repeats it under locks. */
export function currentReviewReady(build: ReviewBuild): boolean {
  const check = build.review_readiness;
  return check?.schema_version === 1 && check.status === "ready" && check.code === null && check.error_code === null;
}

export function reviewReadinessDescription(build: ReviewBuild): string {
  const check = build.review_readiness;
  if (!check || check.schema_version !== 1 || check.status === "check_unavailable")
    return "Current review readiness could not be checked. Refresh the saved build before recording a review; the saved QA results remain available.";
  if (currentReviewReady(build)) return "Current review checks passed. Recording a review checks the source, plan and task again.";
  if (check.status !== "blocked") return "A current review is not available for this build.";
  switch (check.code) {
    case "subscription_static_approved_inputs_changed":
    case "subscription_static_contract_changed":
    case "subscription_static_task_snapshot_changed":
    case "build_review_task_inputs_changed":
    case "build_plan_inputs_changed": return "Current approved inputs differ from this saved build. Review the current plan and source before preparing a fresh build; the saved QA results describe the earlier source.";
    case "build_plan_superseded": return "A newer plan supersedes this build's plan. Review the current plan before preparing new work.";
    case "build_review_source_identity_changed": return "The current source identity differs from this saved review. Review the coding source receipt before preparing new work.";
    case "build_review_request_fingerprint_changed": return "This build no longer matches its saved request fingerprint. Review the source receipt before preparing new work.";
    case "build_review_resource_inputs_changed": return "The resource inventory changed since this plan was approved. Review those resources and the current plan before preparing new work.";
    case "build_review_research_inputs_changed": return "The supporting research changed since this plan was approved. Review the current evidence and plan before preparing new work.";
    case "build_review_idea_inputs_changed": return "The confirmed idea changed since this plan was approved. Review the current idea and plan before preparing new work.";
    case "build_review_memory_inputs_changed": return "Saved venture preferences or constraints changed since this plan was approved. Review the current plan before preparing new work.";
    case "build_review_publication_inputs_changed": return "The managed publication or conversion requirements changed. Review the current plan and publication before preparing new work.";
    case "subscription_static_publication_baseline_changed": return "The publication baseline changed since this source was reviewed. Review the current publication before preparing new work.";
    case "build_review_task_changed":
    case "device_task_not_ready":
    case "build_task_not_executable":
    case "build_plan_task_binding_invalid": return "The current plan task no longer matches this review step. Refresh the plan and review its current task.";
    case "build_task_dependencies_incomplete": return "A required earlier milestone is incomplete. Review the current plan's dependencies.";
    case "build_plan_approval_required": return "The current plan requires approval before this review can be recorded.";
    case "device_coding_qa_feedback_unavailable": return "The prior coding QA feedback could not be qualified for this source. Review the saved coding task and its QA receipt.";
    default: return "The current source, QA receipt or scope does not qualify for review. Refresh the saved build and review its source and plan.";
  }
}

/** Only closed server diagnostic fields are rendered, never raw error text. */
export function reviewFailureDescription(detail: unknown): string | null {
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null;
  const value = detail as {code?: unknown; reasons?: unknown};
  if (!["build_review_not_ready", "build_plan_inputs_changed", "build_review_task_changed", "hosting_revision_baseline_changed"].includes(String(value.code)) ||
      !Array.isArray(value.reasons) || value.reasons.length !== 1 || typeof value.reasons[0] !== "string") return null;
  return reviewReadinessDescription({id: "", state: "", review_readiness: {
    schema_version: 1, status: value.reasons[0] === "build_review_check_unavailable" ? "check_unavailable" : "blocked",
    code: value.reasons[0], error_code: String(value.code),
  }});
}

export async function checkedReport(bytes: ArrayBuffer, build: ReviewBuild, qa: QaReceipt): Promise<QaReport> {
  if (!qa.verified_report || bytes.byteLength > 1024 * 1024 || !qa.report_digest) throw new Error("The QA report is not verified.");
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
  if (hash !== qa.report_digest) throw new Error("The QA report does not match its saved receipt. Refresh the run before reviewing it.");
  const data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as QaReport;
  if (data.schema_version !== 1 || data.qa_run_id !== qa.id || data.build_run_id !== build.id ||
      data.source_artifact_id !== qa.source_artifact_id || data.source_sha256 !== qa.source_sha256 ||
      !Array.isArray(data.cases) || data.cases.length < 1 || data.cases.length > 1800 ||
      !Array.isArray(data.evidence) || data.evidence.length > 6 || !Array.isArray(data.exclusions)) {
    throw new Error("The QA report does not belong to this build.");
  }
  return data;
}

export function acceptedReviewDescription(review: BuildReview | null | undefined, build?: ReviewBuild): string {
  if (build?.publication_required && build.task_completion?.state !== "completed") return "Source accepted. This plan task still requires verified publication and public waitlist submission.";
  if (build?.publication_required) return "Accepted source, publication and public waitlist submission are verified. This plan task is complete.";
  if (review?.review_origin === "founder") return "You accepted this milestone. The saved plan records it as complete.";
  if (review?.review_origin === "godmode") return "Godmode accepted this milestone. Review its saved content evidence and decision.";
  return "This milestone has a saved acceptance. Historical reviewer provenance is not recorded.";
}

/** Bind a receipt handoff to the currently selected Builds route, including before a React rerender. */
export function buildReceiptRoute(location: {pathname: string; search: string}, build: ReviewBuild): string | null {
  const match = /^\/v\/([^/]+)\/(builds|site)\/?$/.exec(location.pathname);
  if (!match || !build.id || !build.app_id) return null;
  if (match[2] === "site" && build.artifact_profile !== "static_landing_v1") return null;
  try {
    const venture = decodeURIComponent(match[1]);
    if (!venture || venture.includes("/")) return null;
    const query = new URLSearchParams(location.search);
    if ((query.has("run") && query.get("run") !== build.id) ||
        (query.has("app") && query.get("app") !== build.app_id)) return null;
    return JSON.stringify([venture, query.get("app"), query.get("run"), query.get("task"), match[2]]);
  } catch { return null; }
}

/** Download the fresh authenticated GET body verbatim; it is a private receipt, not a client status summary. */
export async function downloadBuildReceipt(build: ReviewBuild, read: () => Promise<Response>, current: () => boolean): Promise<boolean> {
  let preparedUrl: string | null = null, handedOff = false;
  try {
    if (!current() || !build.app_id) return false;
    const response = await read();
    if (!current() || !response.ok || !/^application\/json(?:\s*;|$)/i.test(response.headers.get("content-type") || "")) return false;
    const bytes = await response.arrayBuffer();
    if (!current() || bytes.byteLength > 1024 * 1024) return false;
    const receipt = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes));
    if (!receipt || Array.isArray(receipt) || receipt.id !== build.id || receipt.app_id !== build.app_id || receipt.runtime_version !== 1 || !current()) return false;
    preparedUrl = URL.createObjectURL(new Blob([bytes], {type: "application/json"}));
    if (!current()) return false;
    const anchor = document.createElement("a");
    anchor.href = preparedUrl; anchor.download = `build-${build.id}.json`;
    if (!current()) return false;
    anchor.click(); handedOff = true;
    const url = preparedUrl;
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return true;
  } catch { return false; }
  finally {
    if (preparedUrl && !handedOff) {
      try { URL.revokeObjectURL(preparedUrl); } catch { /* Cleanup cannot authorize a retired handoff. */ }
    }
  }
}
