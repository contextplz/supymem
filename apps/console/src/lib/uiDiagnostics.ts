import { apiRouteFeature, apiRouteTemplate } from "./apiRoute.ts";

/** Closed diagnostics only. Exception text can contain prompts, URLs and keys. */
export const UI_DIAGNOSTIC_EVENT = "supymem-ui-diagnostic";
// Gateway-generated refs are hex; Core's own req_ IDs use Crockford base-32.
// The strict shape rejects URLs, credentials, and arbitrary error text.
const REQUEST_REF = /^req_[0-9abcdefghjkmnpqrstvwxyz]{12}$/;
export function validDiagnosticReference(value: unknown): value is string {
  return typeof value === "string" && REQUEST_REF.test(value);
}

export type DiagnosticContext = "api" | "native" | "render" | "runtime" | "promise" | "integrity" | "job" | "ui" | "voice";
export type DiagnosticCode = "network_error" | "server_error" | "request_error" | "native_error" | "render_error" | "runtime_error" | "integrity_error" | "job_error" | "ui_error" | "voice_error";

// Only known command names may enter a receipt. Arguments, paths, event names,
// and native exception messages remain local to the caller.
const NATIVE_COMMANDS = [
  "check_for_update", "install_update", "defer_update", "request_update_check",
  "desktop_auth_status", "desktop_auth_start", "desktop_auth_complete_code", "desktop_auth_sign_out",
  "desktop_permission_status", "desktop_permission_targets", "desktop_permission_request", "open_macos_privacy_pane",
  "set_autostart", "open_setup_window", "reveal_in_file_manager", "mac_notice_test",
  "connect_local_tool", "install_local_builder", "uninstall_local_builder", "install_docker", "start_docker",
  "spawn_coding_control", "kill_coding_control", "device_work_order_action", "inspect_device_work_order",
  "desktop_http_request", "desktop_api_request", "desktop_events_subscribe", "listen_event",
  "desktop_auth_reconnect", "desktop_app_inventory", "desktop_launch_installed_app",
  "desktop_events_unsubscribe", "gateway_url", "backend_status", "open_composio_connect_link",
  "store_passphrase", "mac_notice_diagnostic", "data_dir_path", "venture_workspace_path",
  "set_active_venture", "start_cursor_ide_session", "read_venture_landing", "list_local_clis",
  "list_local_tools", "macos_control_status", "show_permission_coach", "docker_status",
  "write_hermes_config", "write_hermes_files", "stdio_write", "autostart_enabled", "app_version",
  "agent_cursor_prefs", "set_agent_cursor_prefs",
] as const;
export type NativeDiagnosticCommand = typeof NATIVE_COMMANDS[number];
export function closedNativeDiagnosticCommand(value: unknown): NativeDiagnosticCommand | null {
  return typeof value === "string" && NATIVE_COMMANDS.includes(value as NativeDiagnosticCommand)
    ? value as NativeDiagnosticCommand : null;
}

const API_OPERATIONS = [
  "auth_session_read", "venture_list_read",
  "venture_read", "venture_dashboard_read", "venture_onboarding_read", "venture_charter_read",
  "venture_waitlist_read", "venture_waitlist_test", "workspace_site_read",
  "venture_plans_read", "venture_plan_read", "venture_resources_read",
  "app_analytics_overview_read", "app_analytics_funnel_read",
  "research_dossiers_read", "workspace_llm_settings_read", "workspace_llm_catalog_read",
  "venture_delete_phrase_read", "mac_order_read", "mac_source_receipt_read",
  "mac_source_preflight_read", "mac_order_approve", "mac_order_cancel",
  "venture_activity_stream", "venture_events_stream",
] as const;
export type ApiDiagnosticOperation = typeof API_OPERATIONS[number];
const API_TRANSPORTS = ["native_bridge", "native_stream", "browser_fetch"] as const;
export type ApiDiagnosticTransport = typeof API_TRANSPORTS[number];

/** Closed route families only. Never export IDs, queries, path fragments, or arbitrary URLs. */
export function apiDiagnosticOperation(path: string, method = "GET"): ApiDiagnosticOperation | null {
  const verb = method.toUpperCase();
  const route = path.split("?", 1)[0];
  if (verb === "GET") {
    if (route === "/auth/session") return "auth_session_read";
    if (route === "/api/ventures") return "venture_list_read";
    if (route === "/api/ventures/delete-phrase") return "venture_delete_phrase_read";
    if (/^\/api\/ventures\/[^/]+\/research\/dossiers$/.test(route)) return "research_dossiers_read";
    if (/^\/api\/workspaces\/[^/]+\/llm-settings$/.test(route)) return "workspace_llm_settings_read";
    if (/^\/api\/workspaces\/[^/]+\/llm-catalog$/.test(route)) return "workspace_llm_catalog_read";
    if (/^\/api\/ventures\/[^/]+\/device-work-orders\/dwo_[A-Za-z0-9_-]+$/.test(route)) return "mac_order_read";
    if (/^\/api\/ventures\/[^/]+\/device-work-orders\/dwo_[A-Za-z0-9_-]+\/source-receipt$/.test(route)) return "mac_source_receipt_read";
    if (/^\/api\/ventures\/[^/]+\/device-work-orders\/dwo_[A-Za-z0-9_-]+\/source-preflight$/.test(route)) return "mac_source_preflight_read";
    if (/^\/api\/ventures\/[^/]+\/activity\/stream$/.test(route)) return "venture_activity_stream";
    if (route === "/events" && /^\?venture_id=[A-Za-z0-9_-]+(?:&|$)/.test(path.slice(route.length))) return "venture_events_stream";
    if (/^\/api\/ventures\/[^/]+$/.test(route)) return "venture_read";
    if (/^\/api\/ventures\/[^/]+\/dashboard$/.test(route)) return "venture_dashboard_read";
    if (/^\/api\/ventures\/[^/]+\/onboarding$/.test(route)) return "venture_onboarding_read";
    if (/^\/api\/ventures\/[^/]+\/charter$/.test(route)) return "venture_charter_read";
    if (/^\/api\/ventures\/[^/]+\/waitlist$/.test(route)) return "venture_waitlist_read";
    if (/^\/api\/workspaces\/[^/]+\/site$/.test(route)) return "workspace_site_read";
    if (/^\/api\/ventures\/[^/]+\/plans$/.test(route)) return "venture_plans_read";
    if (/^\/api\/ventures\/[^/]+\/plans\/[^/]+$/.test(route)) return "venture_plan_read";
    if (/^\/api\/ventures\/[^/]+\/resources$/.test(route)) return "venture_resources_read";
    if (/^\/api\/apps\/[^/]+\/analytics\/overview$/.test(route)) return "app_analytics_overview_read";
    if (/^\/api\/apps\/[^/]+\/analytics\/funnel$/.test(route)) return "app_analytics_funnel_read";
  }
  if (verb === "POST" && /^\/api\/ventures\/[^/]+\/waitlist\/test$/.test(route)) return "venture_waitlist_test";
  if (verb === "POST" && /^\/api\/ventures\/[^/]+\/device-work-orders\/dwo_[A-Za-z0-9_-]+\/approve$/.test(route)) return "mac_order_approve";
  if (verb === "POST" && /^\/api\/ventures\/[^/]+\/device-work-orders\/dwo_[A-Za-z0-9_-]+\/cancel$/.test(route)) return "mac_order_cancel";
  return null;
}

/** Associate only an exact Mac-order API request with its opaque saved order. */
export function apiDiagnosticOrderId(path: string): string | null {
  const route = path.split("?", 1)[0];
  const match = /^\/api\/ventures\/[^/]+\/device-work-orders\/(dwo_[A-Za-z0-9_-]{1,64})(?:\/(?:approve|cancel|source-receipt|source-preflight))?$/.exec(route);
  return match?.[1] ?? null;
}

export function closedApiDiagnosticOperation(value: unknown): ApiDiagnosticOperation | null {
  return typeof value === "string" && API_OPERATIONS.includes(value as ApiDiagnosticOperation)
    ? value as ApiDiagnosticOperation : null;
}

const NATIVE_503_CODES = ["native_auth_busy", "native_auth_unavailable", "credential_store_failed",
  "secure_store_pending", "keychain_unavailable", "native_auth_quiescing"] as const;
export type Native503Origin = typeof NATIVE_503_CODES[number];

/** Only exact, known Mac-generated responses can identify a native 503. */
export function native503Origin(status: number, data: unknown): Native503Origin | null {
  if (status !== 503 || !data || typeof data !== "object") return null;
  const code = (data as { error?: { code?: unknown } }).error?.code;
  return typeof code === "string" && NATIVE_503_CODES.includes(code as Native503Origin)
    ? code as Native503Origin : null;
}

export function closedNative503Origin(value: unknown): Native503Origin | null {
  return typeof value === "string" && NATIVE_503_CODES.includes(value as Native503Origin)
    ? value as Native503Origin : null;
}

export function closedApiDiagnosticTransport(value: unknown): ApiDiagnosticTransport | null {
  return typeof value === "string" && API_TRANSPORTS.includes(value as ApiDiagnosticTransport)
    ? value as ApiDiagnosticTransport : null;
}

/** Route template, status and the service's machine error code for one failed call. The feature comes from the
 *  server catalog's route table, so a new route needs no console edit. No id, query, message or body reaches it. */
export type ApiDiagnosticDetail = { route: string | null; status: number; serverCode: string | null; feature: string | null };
const SERVER_CODE = /^[a-z][a-z0-9_]{0,39}$/;
export function apiDiagnosticServerCode(data: unknown): string | null {
  const root = data && typeof data === "object" ? data as {
    error?: { code?: unknown }; detail?: string | { error?: { code?: unknown }; code?: unknown }; code?: unknown;
  } : null;
  const detail = root?.detail;
  const code = root?.error?.code ?? (typeof detail === "string" ? detail : detail?.error?.code ?? detail?.code) ?? root?.code;
  return typeof code === "string" && SERVER_CODE.test(code) ? code : null;
}
export function apiDiagnosticDetail(path: string, status: number, data?: unknown): ApiDiagnosticDetail {
  const route = apiRouteTemplate(path);
  return { route, status: Number.isInteger(status) && status >= 0 && status <= 599 ? status : 0,
    serverCode: apiDiagnosticServerCode(data), feature: apiRouteFeature(route) };
}

const SAFE_DIAGNOSTIC = /^(?:API request_failed \(status (?:0|[1-5][0-9]{2})\)|You're going a bit fast - try again in [1-9][0-9]{0,4} seconds|Mac command_failed|(?:Render|Runtime|Promise|Integrity|Job) failure)$/;
const ATTENTION_CODE = /^(?:(?:[a-z][a-z0-9_]*_)?(?:missing_key|key_required|credential_required|credentials_missing|api_key_missing|not_connected|reconnect_required|setup_required|permission_required|permission_denied|approval_required|unauthenticated|invalid_credentials|invalid_password|csrf_failed|validation_error)|composio_missing|gmail_not_ready|(?:gmail|google_ads|meta_ads)_env|consent_required|email_verification_required|insufficient_credits|out_of_credits|reauth_required|secure_store_pending|keychain_unavailable|bad_api_key|invalid_api_key)$/;

export function sanitizeDiagnostic(value: unknown): string {
  const text = typeof value === "string" ? value : "";
  return SAFE_DIAGNOSTIC.test(text) ? text : "Runtime failure";
}

export function apiDiagnostic(status: number, data?: unknown, operation?: ApiDiagnosticOperation | null): DiagnosticCode | null {
  const root = data && typeof data === "object" ? data as {
    error?: { code?: unknown }; detail?: string | { error?: { code?: unknown }; code?: unknown };
  } : null;
  const detail = root?.detail;
  const rawCode = root?.error?.code || (typeof detail === "string" ? detail : detail?.error?.code || detail?.code);
  if (typeof rawCode === "string" && ATTENTION_CODE.test(rawCode)) return null;
  // These GETs model an optional saved resource. Keep any different 404 on
  // the same screen visible as an error (for example a missing venture).
  if (status === 404 && (
    (operation === "venture_onboarding_read" && rawCode === "no_onboarding") ||
    (operation === "venture_charter_read" && rawCode === "no_charter") ||
    (operation === "workspace_site_read" && rawCode === "no_site")
  )) return null;
  if (status === 0 || (status === 503 && (rawCode === "gateway_unreachable" || rawCode === "session_verification_failed"))) return "network_error";
  if (status >= 500) return "server_error";
  // Auth and input validation need founder attention. All other unsuccessful
  // service responses indicate a failed interaction worth investigating.
  if (status >= 400) return "request_error";
  return null;
}

/** Service bodies may contain user text or secrets; only status enters telemetry. */
export function apiDiagnosticMessage(status: number, data?: unknown): string {
  if (status === 429) {
    const root = data && typeof data === "object" ? data as { error?: { retry_after?: unknown }; detail?: { retry_after?: unknown } } : null;
    const raw = root?.error?.retry_after ?? root?.detail?.retry_after;
    const seconds = typeof raw === "number" && Number.isFinite(raw) ? Math.max(1, Math.min(86400, Math.ceil(raw))) : 60;
    return `You're going a bit fast - try again in ${seconds} seconds`;
  }
  return `API request_failed (status ${status})`;
}

/** Opaque request reference only; never store arbitrary response or error text. */
export function apiDiagnosticReference(data: unknown, responseHeader?: unknown): string | null {
  const root = data && typeof data === "object" ? data as {
    error?: { request_id?: unknown }; detail?: { error?: { request_id?: unknown }; request_id?: unknown };
  } : null;
  const value = root?.error?.request_id ?? root?.detail?.error?.request_id ?? root?.detail?.request_id;
  if (validDiagnosticReference(value)) return value;
  return validDiagnosticReference(responseHeader) ? responseHeader : null;
}

/** A native permission or credential request is a task for the founder, not a defect. */
export function nativeDiagnosticMessage(command: string, value: unknown): string | null {
  const message = value instanceof Error ? value.message : String(value ?? "");
  if (/\b(?:permission[_ ]denied|permission[_ ]required|approval[_ ]required|keychain[_ ]unavailable|secure[_ ]store[_ ]pending|credential[_ ]required|not[_ ]connected|unauthenticated)\b/i.test(message)) return null;
  void command;
  return "Mac command_failed";
}

/** "transient" events are recorded but excluded from user-facing error counts. */
export type DiagnosticSeverity = "error" | "transient";
/** The local bridge is still starting; callers retry. Recorded as transient, not as a defect. */
export function nativeDiagnosticSeverity(value: unknown): DiagnosticSeverity {
  const message = value instanceof Error ? value.message : String(value ?? "");
  return /\b(?:cu_bridge_not_ready|native_desktop_permissions_unavailable)\b/.test(message) ? "transient" : "error";
}

/** One-minute client dedupe key. A gateway ref identifies a separate failed request. */
export function diagnosticFingerprint(code: DiagnosticCode, context: DiagnosticContext, text: string,
  feature: string, ref?: string | null, jobId?: string | null, operation?: string | null, origin?: string | null,
  transport?: string | null, orderId?: string | null, nativeCommand?: string | null, route?: string | null): string {
  return JSON.stringify([feature, code, context, sanitizeDiagnostic(text),
    validDiagnosticReference(ref) ? ref : null,
    context === "job" && jobId && /^job_[A-Za-z0-9_-]{1,64}$/.test(jobId) ? jobId : null,
    context === "api" ? closedApiDiagnosticOperation(operation) : null,
    context === "api" ? closedNative503Origin(origin) : null,
    context === "api" ? closedApiDiagnosticTransport(transport) : null,
    context === "api" && orderId && /^dwo_[A-Za-z0-9_-]{1,64}$/.test(orderId) ? orderId : null,
    context === "native" ? closedNativeDiagnosticCommand(nativeCommand) : null,
    context === "api" ? route ?? null : null]);
}

export function announceDiagnostic(code: DiagnosticCode, context: DiagnosticContext, value: unknown, ref?: string | null,
  operation?: string | null, origin?: string | null, transport?: string | null, orderId?: string | null,
  nativeCommand?: string | null, detail?: ApiDiagnosticDetail | null, severity: DiagnosticSeverity = "error"): void {
  if (typeof window === "undefined") return;
  if (context === "api" && detail?.route === "/me/greeting") severity = "transient";
  if (context === "api" && code === "network_error" && detail?.status === 503 &&
    (detail.serverCode === "gateway_unreachable" || detail.serverCode === "session_verification_failed")) severity = "transient";
  const text = context === "api" || context === "native"
    ? sanitizeDiagnostic(value)
    : `${context === "render" ? "Render" : context === "promise" ? "Promise" : context === "integrity" ? "Integrity" : context === "job" ? "Job" : "Runtime"} failure`;
  window.dispatchEvent(new CustomEvent(UI_DIAGNOSTIC_EVENT, {
    detail: { code, context, text, ...(severity === "transient" ? { severity } : {}), ...(validDiagnosticReference(ref) ? { ref } : {}),
      ...(context === "api" && detail ? { route: detail.route, status: detail.status, serverCode: detail.serverCode, feature: detail.feature } : {}),
      ...(context === "api" && closedApiDiagnosticOperation(operation) ? { operation } : {}),
      ...(context === "api" && closedNative503Origin(origin) ? { origin } : {}),
      ...(context === "api" && closedApiDiagnosticTransport(transport) ? { transport } : {}),
      ...(context === "api" && orderId && /^dwo_[A-Za-z0-9_-]{1,64}$/.test(orderId) ? { orderId } : {}),
      ...(context === "native" && closedNativeDiagnosticCommand(nativeCommand) ? { nativeCommand } : {}) },
  }));
}

/** Saved job IDs are opaque and stable across app sessions. */
export function announceJobDiagnostic(jobId: string): void {
  if (!/^job_[A-Za-z0-9_-]{1,64}$/.test(jobId)) {
    announceDiagnostic("job_error", "job", "Job failure");
    return;
  }
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(UI_DIAGNOSTIC_EVENT, {
    detail: { code: "job_error", context: "job", text: "Job failure", jobId },
  }));
}
