import type { ApiResult } from "./client.ts";
import { apiErrorCode, apiFailure } from "./apiFailure.ts";

const DEFAULT_UNSUPPORTED =
  "That Mac task is not supported yet. Supported tasks: Open this venture's Builds or Settings in Supymem; Check git status in the terminal for the venture workspace; Inspect the venture workspace with Cursor, read-only; Verify the live site and managed waitlist in the browser; Use Codex for the current approved coding task.";

function detailMessage(result: ApiResult): string | undefined {
  const data = result.data as {
    detail?: { message?: string; error?: { message?: string } };
    error?: { message?: string };
  } | null;
  const message = data?.detail?.message || data?.detail?.error?.message || data?.error?.message;
  return typeof message === "string" && message.trim() ? message.trim() : undefined;
}

/** Founder-facing copy when from-chat cannot prepare a reviewed Mac task. */
export function macTaskPrepareFailure(result: ApiResult): string {
  const code = apiErrorCode(result);
  const fromServer = detailMessage(result);
  if (fromServer) return fromServer;
  if (code === "device_terminal_task_unavailable") {
    return "This request could not be linked to an open approved plan task in this venture. Review its saved work before continuing.";
  }
  if (code === "device_terminal_task_ambiguous") {
    return "More than one open plan task could match this request. Open Work and review the intended task before continuing.";
  }
  if (code === "device_browser_ai_configuration_unavailable") {
    return "Browser tasks are unavailable or their settings changed. Refresh Browser tasks in Settings, or ask Charlie for manual steps.";
  }
  if (code === "device_browser_ai_request_invalid") {
    return "The browser request could not be qualified. Include the exact HTTPS websites and a clear goal, without credentials or native app controls, then review the request again.";
  }
  if (code === "device_browser_ai_request_bounds") {
    return "This browser request exceeds the supported limits. Shorten the goal or reduce its reviewed scope before preparing a new review.";
  }
  if (code === "device_read_only_browser_task_unavailable") {
    return "The selected browser task profile cannot enforce this read-only goal. Check the configured browser profile and its reviewed permissions before continuing.";
  }
  if (code === "device_desktop_execution_unavailable") {
    return "Native Mac app control is unavailable. Browser tasks use a separate reviewed profile; preparing a browser task does not enable native app control.";
  }
  if (code === "device_budget_requires_reconciliation" || code === "device_effect_requires_reconciliation"
      || code === "device_browser_ai_provider_or_usage_unconfirmed") {
    return "A previous task action or model charge is not yet confirmed. Its reservation may remain held. Check the saved task and credits before continuing; do not repeat the task.";
  }
  if (code === "device_budget_unavailable") {
    return "The task budget could not be confirmed. Check the venture's credits and saved Mac tasks before preparing another review.";
  }
  if (code === "device_task_not_ready" || code === "device_order_task_changed"
      || code === "device_order_review_changed") {
    return "The task or its review changed. Refresh the saved task and review its current scope before continuing.";
  }
  if (code === "device_general_browser_execution_unavailable") return "General background browser goals are unavailable in this beta. No new task was prepared or started, and no additional credits were reserved. Reviewed published-page checks, supported static coding and terminal checks use separate task profiles.";
  if (code === "device_desktop_multisurface_unavailable") return "Multiple-window execution is not enabled yet. Use one reviewed window. No task was started.";
  if (code === "device_task_has_active_order") {
    return "Finish or cancel the current Mac task first.";
  }
  if (code === "device_chat_intent_unsupported") return DEFAULT_UNSUPPORTED;
  if (code === "device_desktop_app_required" || code === "device_desktop_window_required") {
    return "Supymem could not pin the selected Mac app and window. Keep that app open, then send the request again.";
  }
  if (code === "device_chat_secret_use_form") {
    return "Do not paste credentials into a Mac task. Use the secure connection form.";
  }
  if (code === "device_chat_email_required") {
    return "I don't have a verified email address for this Mac task. Tell me the exact address to use in chat, then review it before Supymem types it.";
  }
  if (code === "device_inspection_provider_unsupported") {
    return "A read-only workspace inspection is available for Cursor. Asking another provider to inspect does not grant an edit session.";
  }
  if (code === "device_coding_task_unavailable") {
    return "The current approved plan step is not a coding task. Complete or review that step before asking a coding agent to edit the venture workspace.";
  }
  if (code === "device_coding_provider_unsupported") {
    return "Open Settings > Keys and connections > Coding tools, check your connection and allow Charlie to use your tool, then ask Charlie to retry the build.";
  }
  if (code === "device_personal_build_unavailable") {
    return "Confirm the current personal project idea before reviewing a local build.";
  }
  if (code === "device_personal_build_app_ambiguous") {
    return "This project has more than one product. Open Builds to choose which one to work on.";
  }
  return apiFailure(result, "preparing this Mac task for review");
}

/** Same-app review path for the order that blocks a new from-chat prepare. */
export function activeMacTaskReviewPath(result: ApiResult): string | undefined {
  if (apiErrorCode(result) !== "device_task_has_active_order") return undefined;
  const data = result.data as {
    detail?: { review_path?: string };
  } | null;
  const path = data?.detail?.review_path;
  if (typeof path !== "string" || !path.startsWith("/v/")) return undefined;
  if (path.includes("?") || path.includes("..") || path.includes("%")) return undefined;
  if (!/^\/v\/ven_[A-Za-z0-9_-]+\/device-work-orders\/dwo_[A-Za-z0-9_-]+$/.test(path)) {
    return undefined;
  }
  return path;
}
