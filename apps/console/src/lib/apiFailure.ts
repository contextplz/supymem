import type { ApiResult } from "./client.ts";
import { errorCodeCopy } from "./errorCopy.ts";
import { apiDiagnosticServerCode } from "./uiDiagnostics.ts";
import { usageLimitMessage } from "./credits.ts";
import { isLocalConsole } from "./localMode.ts";

export async function resourceReadFailure(response: Response, resource: string): Promise<string> {
  let data: unknown = null;
  if ((response.headers.get("Content-Type") || "").includes("application/json") &&
      Number(response.headers.get("Content-Length") || 0) <= 16384) {
    try { data = await response.clone().json(); } catch {}
  }
  const code = apiDiagnosticServerCode(data);
  if (response.status === 402 || code?.endsWith("insufficient_credits")) return usageLimitMessage();
  const local = isLocalConsole();
  let nextStep = local ? "Open your venture's Website screen and refresh the saved preview. If it still cannot load, restart your local app and try again."
    : errorCodeCopy(code) || "Open your venture's Website screen and refresh the saved preview, then try again.";
  if (local && response.status === 401) nextStep = "Restart your local app to reconnect your session, then open your venture's Website screen and refresh the saved preview.";
  if (local && [403, 423].includes(response.status)) nextStep = "Open your venture from All ventures, then open Website and refresh its saved preview. If access still fails, restart your local app to reconnect your owner session.";
  if (local && response.status === 429) nextStep = "Wait a moment, then open your venture's Website screen and refresh the saved preview.";
  return `${resource} could not be read. ${nextStep}`;
}

export function apiErrorCode(result: ApiResult): string | undefined {
  const data = result.data as { error?: { code?: string }; detail?: { code?: string; error?: { code?: string } } } | null;
  return data?.error?.code || data?.detail?.error?.code || data?.detail?.code;
}

export function isTransientNativeIdentityFence(result: ApiResult): boolean {
  return result.status === 401 && apiErrorCode(result) === "account_changed_during_request";
}

/** Native transport failed before a response (status 0) or the credential store was busy (503).
 * Retry only idempotent calls with this: the request may or may not have reached the server. */
export function isRetryableNativeOpenFailure(result: ApiResult): boolean {
  return result.status === 0 || result.status === 503;
}

export function apiFailure(result: ApiResult, action: string): string {
  const code = apiErrorCode(result);
  if (result.status === 402 || code?.endsWith("insufficient_credits")) return usageLimitMessage();
  if (code === "device_budget_unavailable") return "Your task budget could not be checked. Try again in a moment.";
  if (code === "experiment_results_unavailable") return "Your experiment results could not be read. Try again in a moment.";
  if (code === "idea_generation_unavailable") return "Your idea results are unavailable. The cause could not be confirmed. Try again in a moment.";
  if (code === "page_ingest_failed") return "Your page upload could not be confirmed. Try uploading it again.";
  if (code === "account_changed_during_request") return isLocalConsole()
    ? errorCodeCopy(code)!
    : "Your account connection changed while this step finished. Check its saved status before trying again.";
  if (code === "capability_unavailable") return isLocalConsole()
    ? "This task is not available in your local app yet. Your saved work is retained. Update your app, then try again."
    : "This operation is not available in the hosted service yet. Your saved work is retained.";
  if (code === "plan_model_required") return "Open Keys and connections and save your model-provider key, then generate your plan again.";
  if (code === "confirmed_revision_required") return "Confirm your idea before creating an execution plan.";
  if (code === "research_confirmation_required") return "Confirm the current idea before starting deep research.";
  if (code === "validation_revision_superseded") return "This finding belongs to an earlier idea revision. Validate the current revision before making a decision.";
  if (code === "clarification_in_progress" || code === "validation_in_progress") return "The current idea still has work running. Wait for it to finish or refresh its status.";
  if (code === "validation_pending" || code === "validation_required") return "Review and accept the current validation findings before confirming this idea.";
  if (code === "plan_research_required") return "Complete the research for your confirmed idea before generating a plan.";
  if (code === "plan_resource_inventory_required") return "Save your existing resource list, or confirm that you have no resources to include.";
  if (["plan_inputs_changed", "plan_review_conflict", "plan_edit_conflict", "plan_version_superseded"].includes(code || "")) return "The plan or its supporting evidence changed. Refresh and review the latest version before continuing.";
  if (code === "plan_context_too_large") return "The planning context exceeds the supported size. Narrow the idea or requested revision before generating it.";
  if (code === "session_closed") return "This idea revision is already confirmed. Start a new revision to change it.";
  if (code === "idea_review_conflict") return "The idea changed after you opened it. Refresh and review the current revision before confirming.";
  if (["build_plan_inputs_changed", "build_plan_superseded", "build_plan_approval_required"].includes(code || "")) return "Review and approve the current execution plan before building. Its idea, resources or evidence may have changed.";
  if (code === "build_task_requires_founder_action") return "This task requires your action. Open its instructions in the execution plan before continuing.";
  if (code === "build_task_dependencies_incomplete") return "Complete the earlier milestones listed in the plan before starting this task.";
  if (["build_source_artifact_required", "build_source_artifact_unverified", "build_reuse_manifest_required"].includes(code || "")) return "This task needs a verified source project or reuse review. Open Import or review the task’s dependencies in the plan.";
  if (code === "build_quote_expired") return "This build review has expired. Review it again before starting.";
  if (["build_quote_consumed", "build_active", "build_idempotency_conflict"].includes(code || "")) return "A build request is already recorded or changed. Refresh the saved runs and check its status before starting another.";
  if (code === "build_credit_account_missing") return "Your account setup is incomplete. Open Settings before requesting a build.";
  if (code === "build_budget_unknown") return "This build is not ready to start yet.";
  if (["hosting_build_not_accepted", "hosting_source_receipt_invalid", "hosting_source_manifest_changed", "private_correction_static_source_required"].includes(code || "")) return "This build does not have the current accepted source and QA proof required for a private correction. Review a current verified build or prepare a new scoped build; no correction was started.";
  if (code === "private_correction_baseline_changed") return "The accepted private source changed. Refresh Builds and choose the current accepted result before saving a correction.";
  if (code === "private_correction_plan_changed") return "The accepted build's plan or supporting evidence changed. Review the current approved plan before preparing a correction.";
  if (code === "private_correction_use_published_revision") return "This product has a published or unresolved publication. Review its Website revision flow instead of a private correction.";
  if (code === "private_correction_venture_paused") return "This venture is paused. Resume it explicitly before requesting correction work.";
  const known = errorCodeCopy(code);
  if (known) return known;
  if (result.status === 0) return `Could not confirm ${action}. Check the connection and refresh before repeating the action.`;
  if (result.status === 401) return isLocalConsole()
    ? errorCodeCopy("unauthorized")!
    : "Sign in again to continue.";
  if (result.status === 403 || result.status === 423) return isLocalConsole()
    ? errorCodeCopy("forbidden")!
    : "Your account cannot perform this operation. Check company access and account status.";
  if (result.status === 404) return "This company workflow could not be found. Refresh to check its current state.";
  if (result.status === 409) return `Could not complete ${action} because the workflow state changed. Refresh and review the current step.`;
  if (result.status === 422) return `Could not complete ${action}. Review the information supplied for this step.`;
  if (result.status === 429) return "Too many requests. Wait a moment and try again.";
  return `Supymem could not complete ${action}. Your saved work is retained; try again when the service is available.`;
}
