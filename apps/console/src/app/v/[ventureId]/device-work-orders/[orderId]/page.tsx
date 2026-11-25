"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "@/components/chat/ChatLink";
import { AppShell } from "@/components/layout/AppShell";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { desktopInvokeOrThrow, invokeErrorMessage, isDesktopShell } from "@/lib/desktop";
import { apiFailure } from "@/lib/apiFailure";
import { errorCodeCopy, plainErrorText } from "@/lib/errorCopy";
import { revisionBuildHref } from "@/lib/siteRevision";
import { deviceOrderStatus } from "@/lib/deviceOrderStatus";
import { announceMacStartExposure } from "@/lib/usageControl";
import { isVerifiedMacTask, macResultHeadline, macWrongPrefill } from "@/lib/macResult";
import { prefillChat } from "@/lib/chatPrefill";
import styles from "./review.module.css";
import { BrowserWatchPanel } from "@/components/desktop/BrowserWatchPanel";
import { needsYouInstruction, nativePollMs, type BrowserWatchFrame } from "@/lib/browserWatch";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { desktopExecutionAvailability, desktopExecutionMessage, isNativeDesktopManifest, type DesktopExecutionAvailability } from "@/lib/desktopExecutionAvailability";
import { generalBrowserOrderAvailability, generalBrowserExecutionMessage, type GeneralBrowserExecutionAvailability } from "@/lib/generalBrowserExecutionAvailability";
import { parseBrowserExecutionProfile, browserReadOnlyAuthority } from "@/lib/browserExecutionProfile";
import { BrowserExecutionProfileReview } from "@/components/desktop/BrowserExecutionProfileReview";
import { BrowserReadUrlsReview } from "@/components/desktop/BrowserReadUrlsReview";
import { browserProfileWireAvailable, BROWSER_PROFILE_WIRE_MESSAGE } from "@/lib/browserProfileWireAvailability";
import { LoopProgress } from "@/components/desktop/LoopProgress";
import { productBuildStatus, safeLoopText, type ChangedFile, type LoopEvent, type LoopIteration } from "@/lib/loopProgress";
import { isLocalConsole } from "@/lib/localMode";
import { useFeatureVisibility } from "@/components/layout/LocalModeBoundary";
import { ProductBuildReview, productPreviewUrl, type ProductLoopReview, type ProductDeployReview, type ProductPlan,
  type ProductScope, type ProductDeployReceipt, type ProductPreviewReceipt } from "@/components/desktop/ProductBuildReview";
import { ProductBuildCapsEditor } from "@/components/desktop/ProductBuildCapsEditor";
import { mergeProductOrderDetails, parseProductBuildLoop, prepareProductBuildCaps, productBuildActorMatches, type ProductBuildCaps, type ProductBuildPhase } from "@/lib/productBuildCaps";

type Order = {
  id: string; venture_id: string; state: "pending" | "approved" | "claimed" | "waiting_for_device" | "cancelled" | "stopped" | "failed";
  actor_id?: string; device_id?: string; workspace_id?: string;
  events?: LoopEvent[];
  browser_qualification_grant?: unknown; browser_qualification_trial_binding?: unknown;
  review_digest: string; expires_at: string; claimed_at?: string; reviewed_at?: string;
  stop_reconciliation_required?: boolean;
  native_stop?: { task_verified?: boolean; processes_stopped?: boolean };
  requested_build?: { request_sha256: string; caps: ProductBuildCaps };
  task_snapshot?: { product_scope?: ProductScope; approved_plan?: ProductPlan };
  manifest: { task_id: string; title: string; instruction: string; applications: string[]; origins: string[];
    actions: string[]; credit_ceiling_micro: number; duration_seconds: number;
    app_navigation_verification?: { operation: string; route: "builds" | "settings" };
    general_browser_verification?: { operation: string; start_url: string; allow_type?: boolean;
      max_steps?: number; fill_values?: Record<string, string>; execution_profile?: unknown; read_urls?: string[] };
    desktop_verification?: { operation: string; application_bundle: string; process_id: number;
      reviewed_surfaces?: { application_bundle: string; process_id: number; process_start_identity: string; window_id: number; window_title: string }[];
      process_start_identity: string; window_id: number; window_title: string; delivery_mode: "background" | "foreground_fallback";
      max_steps: number; reviewed_text: string[]; founder_goal?: string; visual_review?: boolean };
    coding_verification?: { operation: string; provider: string; workspace_slug: string;
      read_scope: string; write_scope: string; command_network: string };
    coding_loop_verification?: ProductLoopReview;
    deploy_verification?: ProductDeployReview;
    inspection_verification?: { operation: string; provider: string; workspace_slug: string;
      read_scope: string; write_scope: string } };
};

type NativeReceipt = {
  state: string;
  order_id?: string;
  result_folder?: string;
  execution_id?: string;
  tool?: "browser" | "terminal" | "coding" | "coding_loop" | "deploy" | "app_navigation" | "inspect" | "general_browser" | "desktop";
  events?: LoopEvent[];
  changed_files?: ChangedFile[];
  stop_after_iteration?: boolean;
  pending_plan?: unknown;
  plan_posted?: boolean;
  next_order_id?: string;
  continuation_message?: string;
  iteration_receipts?: Array<{ posted?: boolean }>;
  deploy_receipt_posted?: boolean;
  deploy_receipt?: ProductDeployReceipt;
  product_preview?: ProductPreviewReceipt;
  journal_unconfirmed?: boolean;
  credits_reconciled?: boolean;
  actual_usage_unconfirmed?: boolean;
  effect_outcome_unconfirmed?: boolean;
  reconciliation_reviewed_at?: string;
  task_verified: boolean;
  failure_code?: string;
  driver_failure_code?: string;
  reason_code?: string;
  target_label?: string;
  driver?: "embedded_cua" | "peekaboo";
  operation?: string;
  founder_prompt?: string;
  pause_kind?: string;
  message?: string;
  result_summary?: string;
  handoff?: string;
  watch?: BrowserWatchFrame;
  source_receipt?: { sha256: string; qa_verified: boolean };
  source_storage_state?: "stored" | "needs_reconciliation";
  evidence?: { order_id?: string; execution_id?: string; outcome?: string; observed_urls?: string[]; driver?: "embedded_cua" | "peekaboo";
    processes_stopped?: boolean;
    product_build?: { outcome?: string; source_commit?: string; file_manifest_sha256?: string; changed_files?: ChangedFile[] };
    steps?: Array<{ kind?: string; operation?: string; effect?: string; target_label?: string;
      after_effect?: string; visible_facts?: string[]; verdict?: string;
      same_process?: boolean; reviewed_window_present?: boolean; other_window_count?: number;
      reason?: "consequential_control" | "unavailable_control";
      driver?: string; cursor_indicator?: "overlay_requested" | "not_shown" }>;
    visual_review?: { verdict: "done" | "continue" | "blocked"; visible_facts: string[];
      next_step: string; image_sha256: string; snapshot_id: string } | null };
};
function attestedDesktopPreDispatch(receipt: NativeReceipt | null): boolean {
  const code = receipt?.driver_failure_code;
  return receipt?.tool === "desktop" &&
    (code === "desktop_fresh_observation_required" ||
      typeof code === "string" && /^desktop_pre_dispatch_[a-z0-9_]{1,60}$/.test(code));
}
function boundProductPreview(receipt: NativeReceipt | null) {
  const preview = receipt?.product_preview, build = receipt?.evidence?.product_build;
  return preview?.transport === "device_order_v1" && preview.source_commit === build?.source_commit
    && preview.file_manifest_sha256 === build?.file_manifest_sha256 ? preview : undefined;
}
const buildStatusUnavailable = "Your latest build status could not be checked. Choose Refresh build to check it again. You can still use Stop build when it is shown.";
const buildStopUnconfirmed = "Your tool has not confirmed Stop. Let your tool finish, then choose Check Stop in Build activity. If it still cannot be checked, reopen your desktop app and choose Refresh build.";
const activityLoadError = "Your full activity log could not load. Close and reopen Full activity log to try again.";
type SourceReceipt = { state: string; sha256: string; qa_verified: false; hosting_authorized: false };
type StaticQaQuote = { quote_id: string; quote_digest: string; expires_at: string; qa_ceiling_micro: number;
  model_ceiling_micro: 0; builder_compute_ceiling_micro: 0; source_fingerprint: string };
type SourceQaRun = { id: string; app_id: string };
type SourcePreflight = { status: "blocked_runtime_contract" | "requires_runtime_qa"; source_sha256: string;
  runtime?: "python_product_v1" | "unsupported_node_review_shell" | "static_landing_v1"; findings: string[];
  report_sha256: string; runtime_qa_verified: false; hosting_authorized: false };
const sourceFindings: Record<string, string> = {
  static_landing_index_missing: "The saved landing source needs an index.html at its root.",
  static_landing_index_invalid: "The landing page is empty or exceeds the supported size.",
  static_landing_server_code_present: "This landing source contains server code; prepare a self-contained static page.",
  static_landing_non_public_file: "The source contains unsupported files outside Supymem's private review evidence folder. Keep the public landing files and move local checks into review/.",
  hosted_product_artifact_missing: "No supported Python product routes and bundled page exist in this source. A Node review shell alone cannot be hosted as the product.",
  product_artifact_invalid: "The Python product routes or page are empty or exceed the bounded contract size.",
  product_routes_syntax_invalid: "The Python product routes do not parse; correct the source before runtime QA.",
  root_package_json_missing: "No package.json exists at the source root.",
  root_package_json_invalid: "The root package.json cannot be read as a valid package object.",
  start_script_missing: "The Node review shell has no start script; it is not the supported hosted Python product.",
  dependency_lock_missing: "Dependencies are declared but no supported lockfile is present at the source root.",
};
const nativeLabels: Record<string, string> = {
  starting: "Preparing this Mac", claiming: "Claiming the approved task", opening_browser: "Opening Supymem’s background browser",
  observing: "Reading the approved website", deciding: "Choosing the next action", admitting_action: "Checking action permission",
  reviewing_screen: "Reviewing the selected Mac window with the visual model",
  acting: "Performing the approved action", closing: "Closing the owned Mac task", stopped: "Mac task stopped; review its outcome",
  running_terminal: "Running the fixed reviewed terminal operation",
  running_coding: "Running the approved coding task in the venture workspace",
  running_inspect: "Reading the venture workspace without opening an editing session",
  awaiting_native_navigation: "Opening the approved Supymem screen in this Mac app",
  cancellation_requested: "Cancellation requested; waiting for the browser to stop", not_started: "No execution started",
  attention_required: "This action needs your review",
  needs_you: "Needs you",
  reconciliation_required: "Execution or final usage needs reconciliation",
  interrupted_reconciliation_required: "The previous execution was interrupted; its result needs reconciliation",
};
// Stable `cu_*` reason codes emitted by the Mac executor (packages/desktop_runtime/cu_reliability.py).
const cuReasons: Record<string, string> = {
  cu_browser_decision_unavailable: "Your browser task stopped because its next step could not be verified. Review its saved result before preparing another task.",
  cu_permission_missing: "Supymem does not have every macOS permission it needs (Accessibility and Screen Recording). Open Mac permissions, grant them, then prepare a new task.",
  cu_bridge_not_ready: "Supymem's local helper is still starting. Wait about 30 seconds after the window opens, then try again.",
  cu_session_unavailable: "Supymem could not start its computer-use session on this Mac. Nothing was done. Reopen the Mac app and prepare a new task.",
  cu_field_not_cleared: "Supymem could not clear the text field before typing, so it stopped rather than type into old text. Check the field, then prepare a new task.",
  cu_field_value_mismatch: "What Supymem typed did not match what the field shows, so it stopped. Check the field, then prepare a new task.",
  cu_effect_unverified: "Supymem could not confirm what its last action did, so it stopped. Check the app before preparing a new task.",
  cu_repeated_action: "Supymem was repeating the same step without progress, so it stopped. Check the app and try a simpler or more specific task.",
  cu_window_changed: "The reviewed app or window changed or closed. Nothing more was done; prepare a fresh review of the current window.",
  cu_reconcile_unavailable: "Your task's saved outcome could not be resolved. Refresh in a minute and try again.",
  cu_stopped: "The Mac task stopped.",
  cu_claim_expired: "Supymem could not start this task because its approval expired. Prepare a fresh request.",
  cu_claim_busy: "Supymem could not start this task because another Mac task for this project is still active. Prepare a fresh request.",
  cu_claim_changed: "Supymem could not start this task because the plan or the reviewed request changed after you approved it. Prepare a fresh request.",
  cu_claim_rejected: "Supymem could not start this task because the server would not accept it. Prepare a fresh request.",
};
const nativeErrors: Record<string, string> = {
  ...cuReasons,
  native_auth_required: "Supymem’s native executor is not authenticated. Reopen the Mac app and reconnect this account.",
  native_browser_not_enabled: "Background browser control is unavailable in this Mac build.",
  native_journal_unavailable: "Supymem could not open its durable execution journal. Reopen the Mac app before retrying.",
  native_order_verification_unavailable: "Supymem could not re-verify this saved task. Refresh it before retrying.",
  native_executor_busy_or_unreconciled: "Another local task is active or needs reconciliation before this one can start.",
  native_order_preflight_unconfirmed: "Supymem could not verify the local browser preflight. Reopen the Mac app before retrying.",
  native_order_result_unconfirmed: "The local executor did not return a confirmed result. Refresh this task before retrying.",
  native_order_request_rejected: "The local executor rejected this request. Refresh this task and review its saved state.",
  native_order_bridge_auth_rejected: "The local executor rejected this Mac's authorization. Refresh the task and reconnect the Mac app before another Start attempt.",
  native_order_bridge_route_unavailable: "The installed Mac executor does not provide this action. Check the app version and refresh the task.",
  native_order_bridge_conflict_unclassified: "The local executor reported a conflict without a known diagnostic code. Refresh the saved task and inspect its execution state before retrying.",
  native_order_bridge_request_invalid: "The local executor could not accept this request. Refresh the task; an app update may be required.",
  native_order_bridge_rate_limited: "The local executor is temporarily rate-limited. Refresh the saved task after the cooldown before retrying.",
  native_order_bridge_server_error: "The local executor reported an internal failure. Refresh the saved task to check whether execution started before retrying.",
  native_bridge_session_unavailable: "Supymem could not restore this Mac's private executor session. No action started. Reopen the Mac app before preparing a new task.",
  verified_account_required: "The Mac executor lost its authenticated session. No action started. Reconnect this Mac and review the task again.",
  desktop_cua_host_invalid: "The Mac app could not attach its bounded computer-use driver to this reviewed task. No action started. Reopen Supymem before preparing a new task.",
  desktop_cua_start_unconfirmed: "The Mac computer-use driver did not confirm startup. No action started; inspect the installed driver before preparing a new task.",
  native_order_account_changed: "The connected account changed during this action. Reconnect before retrying.",
  native_order_transport_unavailable: "Supymem could not reach its local executor. Reopen the Mac app before retrying.",
  native_startup_reconciliation_unavailable: "This interrupted task cannot use automatic startup reconciliation. Its saved evidence needs operator review.",
  native_desktop_pre_dispatch_reconciliation_unavailable: "Supymem could not verify that this Mac action was rejected before dispatch. Its saved evidence needs operator review.",
  native_desktop_text_reconciliation_unavailable: "Supymem could not verify the stopped Mac text action from its saved evidence. Its credit hold remains for operator review.",
  native_desktop_click_reconciliation_unavailable: "Supymem could not match your review to the stopped click and its saved evidence. Its credit hold remains for operator review.",
  native_navigation_reconciliation_unavailable: "This stopped navigation could not be reconciled from its saved evidence. Operator review is required.",
  native_process_cleanup_reconciliation_unavailable: "Supymem could not prove that every recorded coding process stopped. Review the saved evidence before continuing.",
  device_order_review_unavailable: "Supymem could not reload this task for native verification. Refresh before retrying.",
  device_order_response_invalid: "Supymem received a saved task this Mac build cannot read. Update the app, then refresh this task.",
  device_order_scope_invalid: "The saved task no longer matches the reviewed Mac scope. Create a new request instead of retrying it.",
  device_order_identity_mismatch: "This saved task belongs to a different account or Mac. Reconnect the Mac that reviewed it.",
  device_order_response_bounds: "The saved task is too large for the native safety boundary. Create a smaller reviewed request.",
  device_order_account_changed: "The Mac app is connected to a different account. Reconnect before retrying.",
  device_order_device_missing: "This Mac is not registered to the connected account. Reconnect before retrying.",
  device_authentication_required: "The local executor needs the Mac account reconnected before this task can start.",
  coding_provider_not_installed: "The reviewed coding provider is not installed on this Mac.",
  coding_provider_not_yet_supported: "This provider is detected but does not yet have Supymem’s reviewed background-control adapter.",
  coding_provider_failed: "The coding provider stopped without a successful result. Review its saved evidence before retrying.",
  coding_provider_upgrade_required: "Update Claude Code to version 2.1.284 or newer, then prepare a new reviewed task.",
  coding_provider_result_unconfirmed: "The coding agent did not confirm a successful run. Review the workspace before preparing another task.",
  coding_cancelled_or_expired: "The coding process was stopped because permission was cancelled or expired.",
  coding_secret_detected: "A possible secret appeared in the changed files. Supymem stopped before accepting the result.",
  coding_change_set_invalid: "The provider did not produce a bounded, reviewable file change.",
  inspect_workspace_missing: "Supymem could not find this venture’s local workspace. Create it with the reviewed terminal check before inspecting it.",
  inspect_workspace_owner_mismatch: "The local workspace is not marked for this venture. Inspection stopped before reading it.",
  inspect_workspace_changed: "The workspace changed while it was being read. Inspection stopped without accepting the result.",
  inspect_scope_unsupported: "This saved request is not a read-only workspace inspection.",
};

function maskFillPreview(kind: string, value: string): string {
  if (kind === "email") {
    const at = value.indexOf("@");
    if (at <= 0) return "the reviewed email";
    const user = value.slice(0, at);
    const domain = value.slice(at + 1);
    const shown = user.slice(0, Math.min(2, user.length));
    return `the reviewed email (${shown}…@${domain})`;
  }
  if (kind === "name") return "your name";
  if (kind === "tel") return "your phone number";
  return "a reviewed non-secret field";
}

function generalBrowserTypingSummary(fills: Record<string, string> | undefined): string {
  const entries = Object.entries(fills || {}).filter(([, value]) => typeof value === "string" && value.length > 0);
  if (!entries.length) {
    return "Supymem will not type anything secret. It only clicks reviewed non-secret controls.";
  }
  const parts = entries.map(([kind, value]) => maskFillPreview(kind, value));
  if (parts.length === 1) {
    return `Supymem will type ${parts[0]} and nothing secret.`;
  }
  const last = parts[parts.length - 1];
  return `Supymem will type ${parts.slice(0, -1).join(", ")} and ${last}, and nothing secret.`;
}

export default function DeviceOrderPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const orderId = String(params.orderId || "");
  return <AppShell title={isLocalConsole() ? "Your build activity" : "Review Mac task"} ventureId={ventureId}
    subtitle={isLocalConsole() ? "Watch your tool, stop it, or open your result" : "Review the scope before granting permission"}>
    <Review key={`${ventureId}:${orderId}`} ventureId={ventureId} orderId={orderId} />
  </AppShell>;
}

function isCodingBuildOrder(order: Order | null) {
  return Boolean(order && (order.requested_build || order.manifest.coding_verification
    || order.manifest.coding_loop_verification || order.manifest.deploy_verification
    || order.manifest.inspection_verification || order.manifest.app_navigation_verification?.route === "builds"));
}

function Review({ ventureId, orderId }: { ventureId: string; orderId: string }) {
  const featureVisible = useFeatureVisibility();
  const [order, setOrder] = useState<Order | null>(null);
  const [desktopAvailability, setDesktopAvailability] = useState<DesktopExecutionAvailability>("unknown");
  const [generalAvailability, setGeneralAvailability] = useState<GeneralBrowserExecutionAvailability>("unknown");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [uncertainClickAcknowledged, setUncertainClickAcknowledged] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [nativeReceipt, setNativeReceipt] = useState<NativeReceipt | null>(null);
  const [sourceReceipt, setSourceReceipt] = useState<SourceReceipt | null>(null);
  const [preflight, setPreflight] = useState<SourcePreflight | null>(null);
  const [qaQuote, setQaQuote] = useState<StaticQaQuote | null>(null);
  const [qaRun, setQaRun] = useState<SourceQaRun | null>(null);
  const [productAvailable, setProductAvailable] = useState(false);
  const [productScope, setProductScope] = useState<ProductScope | undefined>();
  const [productPlan, setProductPlan] = useState<ProductPlan | null>(null);
  const [productCapsDirty, setProductCapsDirty] = useState(false);
  function capsChanged(dirty: boolean) {
    setProductCapsDirty(dirty);
    if (dirty) setAcknowledged(false);
  }
  const [loopIterations, setLoopIterations] = useState<LoopIteration[]>([]);
  const [deployReceipt, setDeployReceipt] = useState<ProductDeployReceipt | null>(null);
  const [checking, setChecking] = useState(false);
  const [stopGeneration, setStopGeneration] = useState<number | null>(null);
  const [fullActivity, setFullActivity] = useState<LoopEvent[] | null>(null);
  const [copiedFolder, setCopiedFolder] = useState("");
  const alive = useRef(true);
  const serial = useRef(0);
  const stoppingBuild = stopGeneration === serial.current;
  const inFlight = useRef(false);
  const watching = useRef(false);
  watching.current = Boolean(nativeReceipt?.watch || order?.manifest.coding_loop_verification && nativeReceipt
    && !["stopped", "not_started", "reconciliation_required"].includes(nativeReceipt.state));
  const actionsRef = useRef<HTMLDivElement>(null);
  const startButtonRef = useRef<HTMLButtonElement>(null);
  const startExposureReported = useRef(false);
  const requestedStart = useRef(false);
  const lifecycleRefresh = useRef("");
  const router = useRouter();
  const path = `/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(orderId)}`;
  const browserBuild = !isDesktopShell() && isLocalConsole() && Boolean(order?.manifest.coding_loop_verification
    && productBuildActorMatches(order.actor_id, verifiedSessionSubject()));
  const nativeProduct = nativeReceipt?.order_id === orderId ? nativeReceipt : null;
  const resultFolder = browserBuild && order?.state === "stopped" && !order.stop_reconciliation_required
    && nativeProduct?.state === "stopped" && nativeProduct.task_verified === true ? nativeProduct.result_folder : undefined;
  const localPreview = order?.manifest.coding_loop_verification?.phase === "implement"
    ? nativeProduct?.product_preview : undefined;
  const cleanBuild = order?.state === "stopped" && nativeProduct?.state === "stopped" && nativeProduct.task_verified === true
    && (isLocalConsole() ? nativeProduct.evidence?.processes_stopped === true : nativeProduct.credits_reconciled === true)
    && !nativeProduct.journal_unconfirmed && !nativeProduct.actual_usage_unconfirmed
    && !nativeProduct.effect_outcome_unconfirmed && nativeProduct.evidence?.product_build?.outcome === "pass";
  const previewBound = cleanBuild && boundProductPreview(nativeProduct);
  const previewSupported = (isDesktopShell() || browserBuild) && (localPreview?.transport === "device_order_v1" || isLocalConsole()
    && order?.manifest.coding_loop_verification?.profile === "web_app_vercel_v1"
    && order.manifest.coding_loop_verification.phase === "implement");
  const productPreview = localPreview?.status === "ready" && (!previewBound || !previewSupported)
    ? {...localPreview, status: "unavailable" as const, ready: false, url: undefined} : localPreview;
  const deployment = order?.manifest.deploy_verification;
  const localDeployment = nativeProduct?.deploy_receipt;
  const productDeployment = deployment && localDeployment?.target === deployment.target
    && localDeployment.source_commit === deployment.source_commit
    && localDeployment.file_manifest_sha256 === deployment.file_manifest_sha256
    && (!deployment.project_link_sha256 || localDeployment.project_link_sha256 === deployment.project_link_sha256)
    ? localDeployment : deployReceipt;

  async function codingAction(action: "start" | "status" | "cancel" | "reconcile") {
    if (!browserBuild) return desktopInvokeOrThrow<NativeReceipt>("device_work_order_action", { ventureId, orderId, action }, {quiet: true});
    const result = await api(`/native/device-work-orders/${encodeURIComponent(ventureId)}/${encodeURIComponent(orderId)}/${action}`,
      action === "status" ? undefined : {method: "POST"});
    const receipt = result.data as NativeReceipt | null;
    if (!result.ok || receipt?.order_id !== orderId) throw new Error("native_action_unconfirmed");
    return receipt;
  }

  async function openActivity() {
    const generation = serial.current, epoch = verifiedSessionEpoch();
    const current = () => alive.current && serial.current === generation && verifiedSessionEpoch() === epoch;
    try {
      const result = await api(`${path}/events`);
      const rows = (result.data as { events?: LoopEvent[] } | null)?.events;
      if (!result.ok || !Array.isArray(rows)) throw new Error("build_activity_unavailable");
      if (current()) {
        setFullActivity(rows.filter(row => row && typeof row.text === "string" && ["number", "string"].includes(typeof row.at)));
        setError(previous => previous === activityLoadError ? "" : previous);
      }
    } catch {
      if (current()) setError(activityLoadError);
    }
  }

  async function copyResultFolder(folder: string) {
    const generation = serial.current, epoch = verifiedSessionEpoch();
    const current = () => alive.current && generation === serial.current && verifiedSessionEpoch() === epoch;
    try {
      await navigator.clipboard.writeText(folder);
      if (current()) setCopiedFolder(folder);
    } catch {
      if (current()) setError("The folder path could not be copied. Select the path shown in Build activity to copy it manually.");
    }
  }

  async function previewAction(action: "start" | "cancel") {
    if (inFlight.current || busy || !previewSupported
      || action === "start" && (!cleanBuild || localPreview?.stop_available)
      || action === "cancel" && !localPreview?.stop_available) return;
    inFlight.current = true; setBusy(true); setError("");
    const generation = ++serial.current;
    try {
      const receipt = await codingAction(action);
      if (receipt?.order_id !== orderId) throw new Error("native_product_preview_unconfirmed");
      if (alive.current && serial.current === generation) {
        setNativeReceipt(receipt);
        if (isLocalConsole() && action === "start") {
          const url = productPreviewUrl(boundProductPreview(receipt));
          if (!url) throw new Error("native_product_preview_unconfirmed");
          // Same-window navigation lets the desktop navigation guard open this in your browser.
          window.location.assign(url);
        }
      }
    } catch {
      if (alive.current && serial.current === generation) setError(!isLocalConsole()
        ? `Your local preview could not confirm ${action === "start" ? "Start" : "Stop"}. Refresh to check its status.`
        : action === "start"
        ? `Your preview could not open. Choose Refresh build, then Open preview again.${browserBuild ? " If it still does not open, check the last result in Full activity log." : " If it still does not open, choose Open result folder to check dev-server.mjs."}`
        : "Your preview may still be running. Choose Refresh build, then Stop preview again.");
    } finally {
      if (alive.current && serial.current === generation) { inFlight.current = false; setBusy(false); }
    }
  }

  async function refreshOrderLifecycle(saved: Order, generation: number, receipt: NativeReceipt) {
    if (!alive.current || generation !== serial.current || !receipt?.state
      || ["starting", "claiming", "not_started"].includes(receipt.state)
      || receipt.order_id && receipt.order_id !== saved.id) return;
    const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const current = () => alive.current && generation === serial.current && productBuildActorMatches(saved.actor_id, subject)
      && verifiedSessionSubject() === subject && verifiedSessionEpoch() === epoch;
    if (!current()) return;
    const terminal = ["stopped", "reconciliation_required", "interrupted_reconciliation_required"].includes(receipt.state);
    // The local Stop is published before Core acknowledges its receipt. Fetch
    // once again on acknowledgment so an earlier claimed read cannot stick.
    const lifecycle = terminal ? `${receipt.state}:${receipt.credits_reconciled === true ? "reconciled" : "pending"}` : "claimed";
    const key = `${generation}:${receipt.execution_id || receipt.evidence?.execution_id || ""}:${lifecycle}`;
    if (terminal && lifecycleRefresh.current === key) return;
    const previousKey = lifecycleRefresh.current;
    lifecycleRefresh.current = key;
    try {
      const result = await api(path);
      if (!current()) return;
      const body = result.data as Order | null;
      if (!result.ok || body?.id !== saved.id || body?.venture_id !== saved.venture_id
        || body?.review_digest !== saved.review_digest || body?.actor_id !== saved.actor_id
        || body?.device_id !== saved.device_id || body?.workspace_id !== saved.workspace_id
        || !["pending", "approved", "claimed", "waiting_for_device", "cancelled", "stopped"].includes(body.state)) {
        if (lifecycleRefresh.current === key) lifecycleRefresh.current = previousKey;
        return;
      }
      setOrder(previous => {
        if (!current() || previous?.id !== saved.id || previous.venture_id !== saved.venture_id
          || previous.review_digest !== saved.review_digest || previous.actor_id !== saved.actor_id
          || previous.device_id !== saved.device_id || previous.workspace_id !== saved.workspace_id
          || previous.claimed_at && !body.claimed_at || previous.state === "stopped" && body.state !== "stopped") return previous;
        if (previous.manifest.coding_loop_verification) {
          const merged = mergeProductOrderDetails(previous, body);
          if (!merged && lifecycleRefresh.current === key) lifecycleRefresh.current = previousKey;
          return (merged as Order | null) ?? previous;
        }
        return { ...previous, state: body.state, claimed_at: body.claimed_at, reviewed_at: body.reviewed_at };
      });
    } catch {
      if (lifecycleRefresh.current === key) lifecycleRefresh.current = previousKey;
      // Retain the native receipt and reviewed acknowledgment if Core is unreachable.
    }
  }

  async function refreshProductReceipts(saved: Order, generation: number) {
    const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const current = () => alive.current && generation === serial.current && productBuildActorMatches(saved.actor_id, subject)
      && verifiedSessionSubject() === subject && verifiedSessionEpoch() === epoch;
    if (!current()) return;
    const [iterations, plan, deploy] = await Promise.all([
      saved.manifest.coding_loop_verification ? api(`${path}/iterations`).catch(() => null) : null,
      saved.manifest.coding_loop_verification?.phase === "plan" ? api(`${path}/plan`).catch(() => null) : null,
      saved.manifest.deploy_verification ? api(`${path}/deploy-receipt`).catch(() => null) : null,
    ]);
    if (!current()) return;
    if (saved.manifest.coding_loop_verification) {
      const rows = iterations?.data as { iterations?: LoopIteration[] } | null;
      if (iterations?.ok && Array.isArray(rows?.iterations)) setLoopIterations(rows.iterations);
      if (plan?.ok) setProductPlan((plan.data as { plan?: ProductPlan } | null)?.plan ?? null);
    }
    if (deploy?.ok) setDeployReceipt((deploy.data as { receipt?: ProductDeployReceipt } | null)?.receipt ?? null);
  }

  function focusActions() {
    const node = actionsRef.current;
    if (!node) return;
    node.scrollIntoView({ block: "nearest", behavior: "smooth" });
    const focusable = node.querySelector<HTMLElement>("input[type='checkbox'], button:not([disabled])");
    focusable?.focus();
  }

  async function load() {
    const generation = ++serial.current;
    inFlight.current = true; setBusy(true); setOrder(null); setAcknowledged(false); setError("");
    setChecking(true);
    setNativeReceipt(null); setSourceReceipt(null); setPreflight(null); setDesktopAvailability("unknown"); setGeneralAvailability("unknown");
    setProductAvailable(false); setProductScope(undefined); setProductPlan(null); setProductCapsDirty(false); setLoopIterations([]); setDeployReceipt(null); setCopiedFolder("");
    setFullActivity(null);
    try {
      await ensureSession();
      if (!alive.current || generation !== serial.current) return;
      const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
      const current = () => alive.current && generation === serial.current && subject !== null
        && verifiedSessionSubject() === subject && verifiedSessionEpoch() === epoch;
      if (!current()) return;
      const result = isDesktopShell()
        ? { ok: true, status: 200, data: await desktopInvokeOrThrow<Order>("inspect_device_work_order", { ventureId, orderId }) }
        : await api(path);
      if (!current()) return;
      if (!result.ok) { setError(apiFailure(result, "loading this Mac task")); return; }
      const saved = result.data as Order;
      if (saved?.id !== orderId || saved?.venture_id !== ventureId || !saved.manifest || !/^[a-f0-9]{64}$/.test(saved.review_digest)) {
        setError("The saved request could not be verified. Refresh before continuing."); return;
      }
      setOrder(saved); setNow(Date.now());
      if (!featureVisible("settings", "mac") && !isCodingBuildOrder(saved)) return;
      if (saved.manifest.coding_loop_verification || saved.manifest.deploy_verification) {
        if (!productBuildActorMatches(saved.actor_id, subject)) { setError(nativeErrors.device_order_identity_mismatch); return; }
        const [capabilities, metadata] = await Promise.all([
          api(`${path}/capabilities`).catch(() => null),
          api(path).catch(() => null),
        ]);
        const body = metadata?.data as Order | null;
        if (!current()) return;
        setProductAvailable(capabilities?.ok === true && (capabilities.data as { available?: boolean } | null)?.available === true);
        if (saved.manifest.coding_loop_verification) {
          const merged = metadata?.ok ? mergeProductOrderDetails(saved, body) : null;
          if (!merged) {
            setProductAvailable(false);
            setError("The saved product review could not be verified. Refresh before continuing.");
            return;
          }
          setOrder({ ...merged, ...(isLocalConsole() && /^[a-f0-9]{64}$/.test(body?.requested_build?.request_sha256 || "")
            ? {requested_build: body!.requested_build!} : {}) } as Order);
        }
        if (metadata?.ok && body?.id === saved.id && body?.review_digest === saved.review_digest
          && body?.venture_id === saved.venture_id && body?.workspace_id === saved.workspace_id
          && body?.actor_id === saved.actor_id && body?.device_id === saved.device_id) {
          setProductScope(body.task_snapshot?.product_scope);
          setProductPlan(body.task_snapshot?.approved_plan ?? null);
        }
        await refreshProductReceipts(saved, generation);
        if (!current()) return;
      }
      if (saved.manifest.general_browser_verification) {
        const capabilities = await api(`${path}/capabilities`);
        if (!alive.current || generation !== serial.current) return;
        const status = await generalBrowserOrderAvailability(capabilities, saved.manifest.general_browser_verification.execution_profile);
        if (!alive.current || generation !== serial.current) return;
        if (saved.manifest.general_browser_verification.execution_profile != null) {
          const wire = await browserProfileWireAvailable(parseBrowserExecutionProfile(saved.manifest.general_browser_verification.execution_profile)?.version ?? "", browserReadOnlyAuthority(saved.manifest), saved.browser_qualification_grant != null || saved.browser_qualification_trial_binding != null);
          if (!alive.current || generation !== serial.current) return;
          if (!wire) { setGeneralAvailability("unavailable"); setError(BROWSER_PROFILE_WIRE_MESSAGE); return; }
        }
        setGeneralAvailability(status);
      }
      if (isNativeDesktopManifest(saved.manifest)) {
        const paused = await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-desktop-pause`);
        if (!alive.current || generation !== serial.current) return;
        setDesktopAvailability(desktopExecutionAvailability(paused));
      }
      if (saved.state === "stopped" && saved.manifest.coding_verification) {
        const source = await api(`${path}/source-receipt`);
        const stored = source.data as SourceReceipt | undefined;
        if (alive.current && generation === serial.current && source.ok && stored?.state === "stored") {
          setSourceReceipt(stored);
          const checked = await api(`${path}/source-preflight`);
          if (alive.current && generation === serial.current && checked.ok) setPreflight(checked.data as SourcePreflight);
        }
      }
      if (current()) return generation;
    } catch (caught) {
      const code = invokeErrorMessage(caught, "device_order_load_unconfirmed");
      if (alive.current && generation === serial.current) setError(nativeErrors[code]
        || plainErrorText(code, "Could not load this Mac task. Refresh to check its saved state."));
    }
    finally { if (alive.current && generation === serial.current) { inFlight.current = false; setBusy(false); setChecking(false); } }
  }

  useEffect(() => {
    alive.current = true;
    void load();
    const timer = window.setInterval(() => setNow(Date.now()), 5000);
    const changed = () => { void load(); };
    window.addEventListener("supymem-session-changed", changed);
    window.addEventListener("supymem-desktop-pause-changed", changed);
    return () => { alive.current = false; serial.current++; window.clearInterval(timer); window.removeEventListener("supymem-session-changed", changed); window.removeEventListener("supymem-desktop-pause-changed", changed); };
    // This component is keyed by both route identifiers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // A native Start can outlive this page's load (or a session-change refresh).
    // Always recover the durable local receipt for a reviewed order; otherwise
    // the UI can return to "approved" while its worker is already claiming.
    if ((!isDesktopShell() && !browserBuild) || !order || order.state === "pending" || busy) return;
    let stopped = false, polling = false;
    const generation = serial.current;
    const refresh = async () => {
      if (polling) return;
      polling = true;
      try {
        const result = await codingAction("status");
        if ((order.manifest.coding_loop_verification || order.manifest.coding_verification) && result?.order_id !== orderId) {
          throw new Error("native_action_unconfirmed");
        }
        if (!stopped && alive.current && serial.current === generation) {
          setNativeReceipt(result);
          setError(previous => previous === buildStatusUnavailable || previous === buildStopUnconfirmed
            && result.state === "stopped" && result.evidence?.processes_stopped === true ? "" : previous);
        }
        if (!stopped && alive.current && serial.current === generation && result.next_order_id && /^[A-Za-z0-9_-]{1,128}$/.test(result.next_order_id)) {
          router.replace(`/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(result.next_order_id)}`);
          return;
        }
        if (!stopped && alive.current && serial.current === generation) await refreshOrderLifecycle(order, generation, result);
        if (!stopped && alive.current && serial.current === generation) await refreshProductReceipts(order, generation);
      } catch {
        if (!stopped && alive.current && serial.current === generation && isLocalConsole()
          && (order.manifest.coding_loop_verification || order.manifest.coding_verification)) setError(buildStatusUnavailable);
        if (!stopped && alive.current && serial.current === generation) setNativeReceipt(previous =>
          previous?.product_preview?.ready ? {...previous, product_preview: {...previous.product_preview,
            status: "unavailable", ready: false, url: undefined}} : previous);
      }
      finally { polling = false; }
    };
    // Poll quickly while the watch panel is streaming frames, slowly otherwise.
    let timer = 0;
    const tick = () => { void refresh(); timer = window.setTimeout(tick, nativePollMs(watching.current)); };
    tick();
    return () => { stopped = true; window.clearTimeout(timer); };
    // Keep polling across native progress updates; only a saved-order reload
    // or a completed action replaces this poller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [order?.state, order?.claimed_at, busy, browserBuild]);

  async function nativeAction(action: "start" | "cancel" | "reconcile" | "continue" | "handoff" | "source-upload" | "soft-stop") {
    if (["handoff", "continue"].includes(action) && (browserReadOnlyAuthority(order?.manifest) || order?.browser_qualification_grant != null)) {
      setError(order?.browser_qualification_grant != null ? "This qualification task permits only its reviewed controller inputs. Manual browser input is unavailable." : "This task is read only. No browser input access is permitted."); return;
    }
    const localCodingStop = action === "cancel"
      && !!(order?.manifest.coding_loop_verification || order?.manifest.coding_verification) && isLocalConsole();
    if (!order || inFlight.current && !localCodingStop || localCodingStop && stoppingBuild) return;
    const localCheckStop = action === "reconcile" && isLocalConsole()
      && !!(order.manifest.coding_loop_verification || order.manifest.coding_verification);
    if (localCheckStop && !isDesktopShell() && !browserBuild) {
      setError("Open Builds in your desktop app on the laptop that ran your tool. Open this build's Build activity and choose Check Stop.");
      return;
    }
    if (action === "start" && productGrantBlocked) { setError("Local product builds are not enabled for this account yet. Your saved review has not started."); return; }
    const needsNativeAdmission = isNativeDesktopManifest(order.manifest) && ["start", "continue", "handoff"].includes(action);
    const needsGeneralAdmission = Boolean(order.manifest.general_browser_verification) && ["start", "continue", "handoff"].includes(action);
    if (needsGeneralAdmission && generalAvailability !== "available") { setError(generalBrowserExecutionMessage(generalAvailability, order.manifest.general_browser_verification?.execution_profile)); return; }
    if (needsNativeAdmission && desktopGrantBlocked) { setError(desktopExecutionMessage(desktopAvailability)); return; }
    inFlight.current = true; setBusy(true); setError("");
    const generation = localCodingStop ? ++serial.current : serial.current;
    const coding = !!(order.manifest.coding_loop_verification || order.manifest.coding_verification);
    if (coding && action === "cancel") setStopGeneration(generation);
    try {
      if (needsGeneralAdmission) {
        const status = await generalBrowserOrderAvailability(await api(`${path}/capabilities`), order.manifest.general_browser_verification?.execution_profile);
        if (!alive.current || serial.current !== generation) return;
        setGeneralAvailability(status);
        if (status !== "available") { setError(generalBrowserExecutionMessage(status, order.manifest.general_browser_verification?.execution_profile)); return; }
        if (order.manifest.general_browser_verification?.execution_profile != null) {
          const wire = await browserProfileWireAvailable(parseBrowserExecutionProfile(order?.manifest.general_browser_verification?.execution_profile)?.version ?? "", browserReadOnlyAuthority(order?.manifest), order?.browser_qualification_grant != null || order?.browser_qualification_trial_binding != null);
          if (!alive.current || serial.current !== generation) return;
          if (!wire) { setGeneralAvailability("unavailable"); setError(BROWSER_PROFILE_WIRE_MESSAGE); return; }
        }
      }
      if (needsNativeAdmission) {
        const status = desktopExecutionAvailability(await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-desktop-pause`));
        if (!alive.current || serial.current !== generation) return;
        setDesktopAvailability(status);
        if (status !== "available") { setError(desktopExecutionMessage(status)); return; }
      }
      const result = browserBuild && (action === "start" || action === "cancel" || action === "reconcile")
        ? await codingAction(action)
        : await desktopInvokeOrThrow<NativeReceipt>("device_work_order_action", { ventureId, orderId, action });
      if (coding && result?.order_id !== orderId) throw new Error("native_action_unconfirmed");
      if (alive.current && serial.current === generation) setNativeReceipt(result);
      if (result?.state && alive.current && serial.current === generation) await refreshOrderLifecycle(order, generation, result);
    } catch (caught) {
      const code = invokeErrorMessage(caught, "native_action_unconfirmed");
      if (alive.current && serial.current === generation) setError(browserBuild
        ? `Your build did not confirm ${action === "start" ? "Start" : "Stop"}. Choose Refresh build in Build activity to check its status before trying again.`
        : localCheckStop ? buildStopUnconfirmed : nativeErrors[code]
        || plainErrorText(code, "Could not confirm that action on your Mac. Refresh the saved task before retrying."));
    } finally {
      if (coding && action === "cancel" && alive.current && serial.current === generation) setStopGeneration(null);
      if (alive.current && serial.current === generation) { inFlight.current = false; setBusy(false); }
    }
  }

  async function prepareLimits(caps: ProductBuildCaps, phase: ProductBuildPhase) {
    if (!order || inFlight.current || !isDesktopShell() || !productAvailable) return;
    const captured = order, subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const generation = ++serial.current;
    const current = () => alive.current && serial.current === generation && productBuildActorMatches(captured.actor_id, subject)
      && verifiedSessionSubject() === subject && verifiedSessionEpoch() === epoch;
    inFlight.current = true; setBusy(true); setAcknowledged(false); setError("");
    try {
      const result = await prepareProductBuildCaps({ order: captured, caps, phase, parserSupported: true, api, current,
        onCancelled: saved => { if (current()) setOrder(saved as unknown as Order); } });
      if (!current()) return;
      if (!result.ok) {
        setError(apiFailure(result, "preparing a new review with these limits"));
        return;
      }
      const next = result.data as Order;
      router.push(`/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(next.id)}`);
    } finally {
      if (alive.current && serial.current === generation) { inFlight.current = false; setBusy(false); }
    }
  }

  async function prepareProductNext(kind: "preview" | "production") {
    if (!order || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError("");
    const generation = serial.current;
    try {
      const sourcePath = order.manifest.deploy_verification
        ? `/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(order.manifest.deploy_verification.source_order_id)}` : path;
      const result = await api(`${sourcePath}/prepare-deploy`, {
        method: "POST", body: JSON.stringify({ target: kind }),
      });
      if (!alive.current || generation !== serial.current) return;
      if (!result.ok) { setError(apiFailure(result, "preparing your next approval")); return; }
      const next = result.data as { id?: string } | null;
      if (typeof next?.id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(next.id)) {
        setError("The next review could not be confirmed. Refresh before continuing."); return;
      }
      router.push(`/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(next.id)}`);
    } catch { if (alive.current && generation === serial.current) setError("The next review is unconfirmed. Refresh before trying again."); }
    finally { if (alive.current && generation === serial.current) { inFlight.current = false; setBusy(false); } }
  }

  async function closeUncertainClick() {
    if (inFlight.current || !order || !uncertainClickAcknowledged) return;
    inFlight.current = true; setBusy(true); setError("");
    const generation = serial.current;
    try {
      const ack = await api(`${path}/uncertain-click-ack`, {
        method: "POST", body: JSON.stringify({ acknowledged: true }),
      });
      if (!ack.ok) {
        if (alive.current && serial.current === generation) setError(apiFailure(ack, "recording your review of the uncertain click"));
        return;
      }
      const result = await desktopInvokeOrThrow<NativeReceipt>("device_work_order_action", {
        ventureId, orderId, action: "reconcile",
      });
      if (alive.current && serial.current === generation) {
        setNativeReceipt(result);
        setUncertainClickAcknowledged(false);
      }
    } catch (caught) {
      const code = invokeErrorMessage(caught, "native_desktop_click_reconciliation_unavailable");
      if (alive.current && serial.current === generation) setError(nativeErrors[code]
        || plainErrorText(code, "Could not confirm that click on your Mac. Refresh the saved task before retrying."));
    } finally {
      if (alive.current && serial.current === generation) { inFlight.current = false; setBusy(false); }
    }
  }

  async function runSourcePreflight() {
    if (order?.manifest.coding_loop_verification || order?.manifest.deploy_verification) return;
    if (inFlight.current || !sourceReceipt || sourceReceipt.state !== "stored") return;
    inFlight.current = true; setBusy(true); setError("");
    try {
      const result = await api(`${path}/source-preflight`, { method: "POST" });
      if (!result.ok) { setError(apiFailure(result, "checking the stored source contract")); return; }
      setPreflight(result.data as SourcePreflight);
    } catch {
      setError("Could not confirm the source check. Refresh to read its saved report before retrying.");
    } finally { inFlight.current = false; setBusy(false); }
  }

  async function staticQaAction(approve = false, reviewed = qaQuote) {
    if (order?.manifest.coding_loop_verification || order?.manifest.deploy_verification) return;
    if (inFlight.current || sourceReceipt?.state !== "stored" || approve && !reviewed) return;
    inFlight.current = true; setBusy(true); setError("");
    try {
      const result = await api(`${path}/${approve ? "static-qa" : "static-qa-quote"}`, {
        method: "POST", ...(approve && reviewed ? { body: JSON.stringify({ quote_id: reviewed.quote_id,
          quote_digest: reviewed.quote_digest, approve_qa_ceiling_micro: reviewed.qa_ceiling_micro }) } : {}),
      });
      if (!result.ok) { setError(apiFailure(result, approve ? "starting your independent check" : "preparing your independent check")); return; }
      const data = result.data as StaticQaQuote & { existing_run?: SourceQaRun; id?: string; app_id?: string };
      const run = approve && data.id && data.app_id ? { id: data.id, app_id: data.app_id } : data.existing_run;
      if (run) { setQaRun(run); setQaQuote(null); }
      else {
        setQaQuote(data);
        if (!approve) { inFlight.current = false; await staticQaAction(true, data); }
      }
    } catch {
      setError("Your check could not be verified. Try the check again to recover its saved run before starting another.");
    } finally { inFlight.current = false; setBusy(false); }
  }

  async function renew() {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError("");
    const generation = ++serial.current;
    try {
      if (order?.manifest.general_browser_verification?.execution_profile != null) {
        const wire = await browserProfileWireAvailable(parseBrowserExecutionProfile(order?.manifest.general_browser_verification?.execution_profile)?.version ?? "", browserReadOnlyAuthority(order?.manifest), order?.browser_qualification_grant != null || order?.browser_qualification_trial_binding != null);
        if (!alive.current || serial.current !== generation) return;
        if (!wire) { setGeneralAvailability("unavailable"); setError(BROWSER_PROFILE_WIRE_MESSAGE); return; }
      }
      const result = await api(`${path}/renew`, { method: "POST" });
      if (!alive.current || serial.current !== generation) return;
      const next = (result.data as { review_path?: unknown } | null)?.review_path;
      const websiteRetry = isLocalConsole() && order?.manifest.coding_loop_verification?.profile === "static_landing_v1"
        && next === `/v/${ventureId}/site`;
      if (!result.ok || typeof next !== "string" || !websiteRetry && !next.startsWith(`/v/${ventureId}/device-work-orders/`)) {
        setError(apiFailure(result, "preparing a fresh Mac task request"));
        return;
      }
      router.push(next);
    } catch {
      if (alive.current && serial.current === generation) setError("Could not prepare a fresh request. Ask again in chat.");
    } finally { if (alive.current && serial.current === generation) { inFlight.current = false; setBusy(false); } }
  }

  async function revealBuildFolder() {
    const workspace = order?.manifest.coding_loop_verification?.workspace_slug || order?.manifest.coding_verification?.workspace_slug;
    if (!workspace || inFlight.current || !isDesktopShell()) return;
    inFlight.current = true; setBusy(true); setError("");
    const generation = serial.current;
    try {
      const folder = await desktopInvokeOrThrow<string>("venture_workspace_path", {ventureId, folderName: workspace});
      if (!alive.current || generation !== serial.current) return;
      await desktopInvokeOrThrow("reveal_in_file_manager", {path: folder});
    } catch {
      if (alive.current && generation === serial.current) setError("Your result folder could not be opened. Choose Refresh build on this screen, then Open result folder again.");
    } finally {
      if (alive.current && generation === serial.current) { inFlight.current = false; setBusy(false); }
    }
  }

  async function freshAfterRejection() {
    // A refused claim leaves the approved review (and its credit hold) open; release it first.
    await act("cancel");
    await renew();
  }

  async function act(action: "approve" | "cancel" | "acknowledge") {
    if (!order || inFlight.current || (action === "approve" && (productCapsDirty || !acknowledged || desktopGrantBlocked || productGrantBlocked || (Boolean(order.manifest.general_browser_verification) && generalAvailability !== "available")))) return;
    inFlight.current = true; setBusy(true); setError("");
    const generation = ++serial.current;
    const digest = order.review_digest;
    try {
      if (action === "approve" && order.manifest.general_browser_verification) {
        const status = await generalBrowserOrderAvailability(await api(`${path}/capabilities`), order.manifest.general_browser_verification.execution_profile);
        if (!alive.current || serial.current !== generation) return;
        setGeneralAvailability(status);
        if (status !== "available") { setError(generalBrowserExecutionMessage(status, order.manifest.general_browser_verification.execution_profile)); return; }
        if (order.manifest.general_browser_verification.execution_profile != null) {
          const wire = await browserProfileWireAvailable(parseBrowserExecutionProfile(order?.manifest.general_browser_verification?.execution_profile)?.version ?? "", browserReadOnlyAuthority(order?.manifest), order?.browser_qualification_grant != null || order?.browser_qualification_trial_binding != null);
          if (!alive.current || serial.current !== generation) return;
          if (!wire) { setGeneralAvailability("unavailable"); setError(BROWSER_PROFILE_WIRE_MESSAGE); return; }
        }
      }
      if (action === "approve" && isNativeDesktopManifest(order.manifest)) {
        const status = desktopExecutionAvailability(await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-desktop-pause`));
        if (!alive.current || serial.current !== generation) return;
        setDesktopAvailability(status);
        if (status !== "available") { setError(desktopExecutionMessage(status)); return; }
      }
      const result = await api(`${path}/${action}`, { method: "POST", body: JSON.stringify({ review_digest: digest,
        ...(action === "approve" && isDesktopShell() && (order.manifest.coding_loop_verification || order.manifest.deploy_verification)
          ? {product_build_parser_supported: true} : {}),
        ...(action === "approve" && browserReadOnlyAuthority(order.manifest) ? {read_only_parser_supported: true} : {}),
        ...(action === "approve" && (order.browser_qualification_grant != null || order.browser_qualification_trial_binding != null)
          ? {browser_qualification_parser_supported: true} : {}) }) });
      if (!alive.current || generation !== serial.current) return;
      if (!result.ok) {
        setOrder(null); setAcknowledged(false); setError(apiFailure(result, `${action === "approve" ? "approving" : action === "acknowledge" ? "marking this Mac task reviewed" : "cancelling"} this Mac task`));
        return;
      }
      const refreshed = await load();
      if (action === "approve" && alive.current && refreshed === serial.current) {
        window.requestAnimationFrame(() => { if (alive.current && refreshed === serial.current) focusActions(); });
      }
    } catch {
      if (alive.current && generation === serial.current) {
        setOrder(null); setAcknowledged(false); setError("The result is uncertain. Refresh the saved request before attempting another action.");
      }
    } finally { if (alive.current && generation === serial.current) { inFlight.current = false; setBusy(false); } }
  }

  const expires = order ? Date.parse(order.expires_at) : NaN;
  const executionExists = Boolean(order?.claimed_at || order?.state === "claimed" || order?.state === "stopped"
    || nativeReceipt && (nativeReceipt.execution_id || nativeReceipt.evidence?.execution_id || nativeReceipt.state !== "not_started"));
  const expired = !executionExists && (!Number.isFinite(expires) || now >= expires);
  const reviewMinutes = Math.max(0, Math.ceil((expires - now) / 60000));
  const stoppedLabel = nativeReceipt?.tool === "terminal" ? "Terminal process stopped"
    : nativeReceipt?.tool === "coding" ? "Coding process stopped"
    : nativeReceipt?.tool === "inspect" ? "Read-only inspection stopped"
    : nativeReceipt?.tool === "app_navigation" ? "Supymem navigation stopped"
    : nativeReceipt?.tool === "desktop" ? "Mac app task stopped" : "Browser stopped";
  const desktopEffects = nativeReceipt?.tool === "desktop" && Array.isArray(nativeReceipt.evidence?.steps)
    ? nativeReceipt.evidence.steps.filter((step) => step.kind === "effect") : [];
  const desktopRejection = nativeReceipt?.tool === "desktop" && Array.isArray(nativeReceipt.evidence?.steps)
    ? nativeReceipt.evidence.steps.filter((step) => step.kind === "action_rejected").at(-1) : undefined;
  const reviewedWindowChanged = nativeReceipt?.state === "not_started" &&
    ["desktop_app_identity_changed", "desktop_window_identity_changed"].includes(nativeReceipt.failure_code || "");
  const claimRejected = nativeReceipt?.state === "not_started" && nativeReceipt.failure_code === "native_claim_rejected";
  const desktopGrantBlocked = Boolean(order && isNativeDesktopManifest(order.manifest)) && desktopAvailability !== "available";
  const generalGrantBlocked = Boolean(order?.manifest.general_browser_verification) && generalAvailability !== "available";
  const productGrantBlocked = Boolean(order?.manifest.coding_loop_verification || order?.manifest.deploy_verification) && (!productAvailable || !isDesktopShell() && !browserBuild);
  const savedProductLoop = parseProductBuildLoop(order?.manifest.coding_loop_verification);
  const editPlanLimits = Boolean(savedProductLoop?.phase === "plan" && !executionExists
    && (order?.state === "pending" || order?.state === "cancelled" || expired));
  const prepareCodingLimits = Boolean(savedProductLoop?.phase === "plan" && productPlan && nativeReceipt?.state === "stopped"
    && nativeReceipt.task_verified === true && nativeReceipt.credits_reconciled === true);
  const canStart = !desktopGrantBlocked && !generalGrantBlocked && !productGrantBlocked && !executionExists
    && (isDesktopShell() || browserBuild && nativeProduct?.state === "not_started" && /^[a-f0-9]{64}$/.test(order?.requested_build?.request_sha256 || ""))
    && order?.state === "approved" && !expired && !reviewedWindowChanged && !claimRejected &&
    (!nativeReceipt || nativeReceipt.state === "not_started") && !busy;
  useEffect(() => {
    if (!isLocalConsole() || !canStart || !order?.manifest.coding_loop_verification
      || !/^[a-f0-9]{64}$/.test(order.requested_build?.request_sha256 || "") || requestedStart.current) return;
    requestedStart.current = true;
    void nativeAction("start");
  }, [canStart, order?.requested_build?.request_sha256]);
  useEffect(() => {
    if (!canStart || startExposureReported.current) return;
    const button = startButtonRef.current;
    if (!button) return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting) && !document.hidden && !button.disabled) {
        startExposureReported.current = true;
        announceMacStartExposure(orderId);
        observer.disconnect();
      }
    });
    observer.observe(button);
    return () => observer.disconnect();
  }, [canStart, orderId]);
  const lastDesktopEffect = desktopEffects.at(-1)?.effect;
  const lastDesktopTarget = desktopEffects.at(-1)?.target_label;
  const statusReceipt = nativeReceipt?.tool === "general_browser" ? {...nativeReceipt, task_verified: false} : nativeReceipt;
  const reconciliationNeedsReview = Boolean(order?.claimed_at && nativeReceipt && !nativeReceipt.reconciliation_reviewed_at
    && (["reconciliation_required", "interrupted_reconciliation_required"].includes(nativeReceipt.state)
      || nativeReceipt.state === "stopped" && nativeReceipt.credits_reconciled === false));
  const browserAnswer = nativeReceipt?.tool === "general_browser" && order?.manifest.general_browser_verification
    && ["stopped", "reconciliation_required", "interrupted_reconciliation_required"].includes(nativeReceipt.state)
    && typeof nativeReceipt.result_summary === "string" && nativeReceipt.result_summary.trim()
    && [...nativeReceipt.result_summary].length <= 500 ? nativeReceipt.result_summary : null;
  if (isLocalConsole() && (order?.manifest.coding_loop_verification || order?.manifest.coding_verification)) {
    const loop = order.manifest.coding_loop_verification || { ...order.manifest.coding_verification!,
      profile: "static_html_v1", max_iterations: 1, max_wall_seconds: order.manifest.duration_seconds,
      per_call_seconds: order.manifest.duration_seconds };
    const desktop = isDesktopShell();
    const lastTry = loopIterations.at(-1);
    const browserFinished = browserBuild && order.state === "stopped" && !order.stop_reconciliation_required
      && order.native_stop?.processes_stopped === true && order.native_stop.task_verified === true;
    const watch = stoppingBuild ? {...nativeProduct, state: "stopping"}
      : browserBuild && order.state === "failed" && (!nativeProduct || nativeProduct.state === "not_started")
      ? {state: "failed", failure_code: "build_failed"} : nativeProduct
      || {state: order.stop_reconciliation_required ? "reconciliation_required" : order.state,
        task_verified: order.native_stop?.task_verified, evidence: {processes_stopped: order.native_stop?.processes_stopped,
          outcome: browserFinished ? loop.phase === "plan" ? "plan_ready" : "pass" : lastTry?.outcome} };
    const tests = lastTry?.outcome === "pass" ? lastTry.diagnosis?.tap_summary : undefined;
    const status = productBuildStatus(watch);
    return <section className={styles.activity} aria-label="Your build activity">
      <ProductBuildReview local loop={loop} watch={watch} busy={busy} scope={productScope} plan={productPlan}
        preview={productPreview}
        checksPassed={tests?.fail === 0 && Number.isSafeInteger(tests.pass) && tests.pass > 0 ? tests.pass : undefined}
        onStartBuild={canStart ? () => void nativeAction("start") : undefined}
        onStopBuild={desktop || browserBuild
          ? () => void nativeAction(status === "stop_unconfirmed" ? "reconcile" : "cancel") : undefined}
        onRetryBuild={(desktop || browserBuild) && ["stopped", "cancelled", "failed", "missing", "signed_out", "disconnected"].includes(status)
          ? () => void (order.state === "approved" && !order.claimed_at ? nativeAction("start") : renew()) : undefined}
        onRevealFolder={desktop && executionExists ? () => void revealBuildFolder() : undefined}
        onRetryPreview={previewSupported && cleanBuild ? () => void previewAction("start") : undefined}
        onStopPreview={previewSupported && localPreview?.stop_available ? () => void previewAction("cancel") : undefined} />
      {resultFolder && <p>Your result folder: <code>{resultFolder}</code>{" "}
        <button type="button" onClick={() => void copyResultFolder(resultFolder)}>Copy result folder</button>{" "}
        <span role="status">{copiedFolder === resultFolder ? "Folder path copied." : ""}</span></p>}
      {browserFinished && loop.profile === "web_app_vercel_v1" && loop.phase === "implement" && !cleanBuild
        && <p>Your saved result has no verified preview on this laptop. {resultFolder ? "Open the result folder shown here to inspect your files." : "Choose Refresh build to check its result again, or ask Charlie in your venture chat to rebuild it here."}</p>}
      {error && <ErrorNotice error={safeLoopText(error, 500)} source="builds.local_activity" />}
      {nativeProduct?.failure_code && nativeProduct.message && <ErrorNotice error={safeLoopText(nativeProduct.message, 500)} source="builds.local_failure" />}
      {nativeProduct?.continuation_message && <ErrorNotice error={safeLoopText(nativeProduct.continuation_message, 500)} source="builds.local_continuation" />}
      <LoopProgress collapsed maximum={loop.max_iterations}
        events={fullActivity?.length && fullActivity.length >= (order.events?.length || 0) ? fullActivity
          : order.events?.length ? order.events : nativeProduct?.events}
        onOpenActivity={order.manifest.coding_loop_verification ? () => void openActivity() : undefined} iterations={loopIterations}
        files={nativeProduct?.changed_files || nativeProduct?.evidence?.product_build?.changed_files} />
      <button type="button" disabled={busy} onClick={() => void load()}>Refresh build</button>{" "}
      <Link href={`/v/${encodeURIComponent(ventureId)}/builds`}>Return to Builds</Link>
    </section>;
  }
  if (!featureVisible("settings", "mac") && !isCodingBuildOrder(order)) return <section aria-label="Your coding tool">
    {error && <ErrorNotice error={error} source="device-work-orders.orderId.error" />}
    {order ? <p>Computer tasks are not in this version yet; connect your coding tool in <Link href="/settings/local#coding-tools">Keys and connections</Link> to build or fix your product.</p>
      : <p role="status">Opening your saved build. If it does not open, <Link href={`/v/${encodeURIComponent(ventureId)}/builds`}>return to your Builds screen</Link> and try again.</p>}
  </section>;
  const ReviewHeading = isLocalConsole() ? "h2" : "h1";
  return <section className={styles.panel} aria-busy={busy}>
    <div className={styles.heading}><ReviewHeading>{order?.manifest.title || "Saved Mac task"}</ReviewHeading><button type="button" disabled={busy} onClick={() => void load()}>Refresh</button></div>
    <ErrorNotice error={error} source="device-work-orders.orderId.error" className={styles.error} />
    {generalGrantBlocked && <p role="status">{generalBrowserExecutionMessage(generalAvailability, order?.manifest.general_browser_verification?.execution_profile)}</p>}
    {!order && !error && <p role="status">Loading the saved request…</p>}
    {order && checking && <p role="status">Checking task availability…</p>}
    {order && <>
      <p role="status">{deviceOrderStatus(order.state, {
        expired, claimedAt: order.claimed_at, receipt: statusReceipt,
      })}</p>
      {!executionExists && !expired && (order.state === "pending" || order.state === "approved") && <p role="status">
        {order.state === "pending" ? "Time left to review" : "Time left to start"}: {Math.floor(reviewMinutes / 60)}h {reviewMinutes % 60}m. Expires {new Date(expires).toLocaleString()}.
      </p>}
      {isNativeDesktopManifest(order.manifest) && desktopGrantBlocked && <p role="status">
        {desktopExecutionMessage(desktopAvailability)} Review saved results below.
      </p>}
      <p className={styles.instruction}>{order.manifest.instruction}</p>
      {productGrantBlocked && <p role="status">{!isDesktopShell()
        ? "Open this review in the Supymem Mac app to approve and run local coding."
        : "Local product builds are not enabled for this account yet. This review does not start a coding tool or a deployment."}</p>}
      <ProductBuildReview loop={order.manifest.coding_loop_verification} deploy={order.manifest.deploy_verification}
        scope={productScope} plan={productPlan} receipt={productDeployment} preview={productPreview}
        onRetryPreview={previewSupported && cleanBuild && !busy ? () => void previewAction("start") : undefined}
        onStopPreview={previewSupported && localPreview?.stop_available && !busy ? () => void previewAction("cancel") : undefined} />
      {savedProductLoop && editPlanLimits && <ProductBuildCapsEditor key={`${order.id}-plan`}
        caps={savedProductLoop} provider={savedProductLoop.provider} phase="plan"
        disabled={busy || productGrantBlocked} freshReview={order.state === "cancelled" || expired}
        onDirtyChange={capsChanged} onPrepare={caps => prepareLimits(caps, "plan")}
        className={styles.limits} primaryClassName={productCapsDirty || order.state === "cancelled" || expired ? styles.primary : undefined} />}
      {savedProductLoop && prepareCodingLimits && <ProductBuildCapsEditor key={`${order.id}-implement`}
        caps={{ max_iterations: 6, max_wall_seconds: 2700, per_call_seconds: 720,
          max_turns: savedProductLoop.provider === "claude" ? 40 : null }}
        provider={savedProductLoop.provider} phase="implement" disabled={busy || productGrantBlocked} freshReview
        onPrepare={caps => prepareLimits(caps, "implement")} className={styles.limits} primaryClassName={styles.primary} />}
      {order.manifest.coding_loop_verification && <LoopProgress
        maximum={order.manifest.coding_loop_verification.max_iterations}
        events={nativeReceipt?.events} iterations={loopIterations} />}
      {order.manifest.coding_loop_verification && nativeReceipt?.state === "stopped" && nativeReceipt.failure_code &&
        <ErrorNotice error={nativeReceipt.message || nativeErrors[nativeReceipt.failure_code] || errorCodeCopy(nativeReceipt.failure_code)
          || "This local build stopped without a verified result. Your saved files are retained; ask Supymem for a new coding review."}
          source="device-work-orders.orderId.coding_stopped" className={styles.error} />}
      {nativeReceipt?.continuation_message && <ErrorNotice error={nativeReceipt.continuation_message}
        source="device-work-orders.orderId.continuation" className={styles.error} />}
      {nativeReceipt && <p role="status">{nativeReceipt.state === "observing" && nativeReceipt.tool === "desktop"
        ? "Reading the reviewed Mac app window"
        : nativeReceipt.state === "needs_you"
        ? (nativeReceipt.founder_prompt || nativeReceipt.message || "Needs you to continue in the Supymem browser window.")
        : nativeReceipt.state === "stopped" && isVerifiedMacTask(statusReceipt)
        ? `${stoppedLabel}; the reviewed task was independently verified`
        : nativeReceipt.state === "closing" && nativeReceipt.tool === "terminal"
          ? "Closing the owned terminal process"
        : nativeLabels[nativeReceipt.state] || "Checking execution state"}
        {nativeReceipt.credits_reconciled === true ? ". Credits reconciled." : ""}</p>}
      {nativeReceipt?.reason_code && cuReasons[nativeReceipt.reason_code] &&
        <p>{cuReasons[nativeReceipt.reason_code]}</p>}
      {browserAnswer !== null && <section aria-label="Reported browser result">
        <h2>Reported browser result</h2>
        <p className={styles.instruction}>{browserAnswer}</p>
        <p>This is Supymem’s reported answer. Compare it with the website; the task remains unverified.</p>
      </section>}
      {nativeReceipt?.tool === "desktop" && nativeReceipt.state === "acting" && nativeReceipt.target_label &&
        <p role="status">Supymem is attempting {nativeReceipt.operation === "click" ? "a click on" : "an action at"} “{nativeReceipt.target_label}” in the reviewed window. The result is pending.</p>}
      {nativeReceipt?.source_receipt && <p role="status">Local coding source captured. {order.manifest.coding_loop_verification || order.manifest.deploy_verification
        ? "The snapshot is retained on this Mac. A source snapshot alone does not verify a working app. Deployment to your account needs its own review and approval."
        : nativeReceipt.source_storage_state === "stored"
        ? "The exact version is stored for independent QA. The product is not yet accepted or hosted."
        : "Server custody is not confirmed; send this saved version before QA."}</p>}
      {nativeReceipt?.tool === "desktop" && nativeReceipt.evidence && <section aria-label="Observed Mac result">
        {nativeReceipt.state === "stopped" && <div role="status"><strong>{macResultHeadline(nativeReceipt.evidence.visual_review, nativeReceipt)}</strong>
          <p>{nativeReceipt.evidence.visual_review?.visible_facts?.length
            ? isVerifiedMacTask(nativeReceipt)
              ? "Supymem independently checked the reviewed result. Does it match what you wanted?"
              : "Supymem read these details from the screen; the whole request remains unverified. Review the Mac app before continuing."
            : "Supymem has no observed result to show. Review the Mac app before preparing another task."}</p></div>}
        <details><summary>How Supymem checked</summary>
        {nativeReceipt.effect_outcome_unconfirmed && <ErrorNotice error="One or more action results remain uncertain. Review the saved task before retrying; do not treat its action as verified." source="device-work-orders.orderId.one_or_more_native" />}
        {desktopRejection && <p role="status">Supymem stopped before attempting {desktopRejection.operation || "an action"}{desktopRejection.target_label
          ? ` on “${desktopRejection.target_label}”` : ""}. {desktopRejection.reason === "consequential_control"
          ? "This control may have a consequential effect and needs a separate review."
          : "The proposed control was unavailable in the fresh observation."}</p>}
        {desktopEffects.length > 0 && <p>Native action receipts: {desktopEffects.length}. Last driver effect: {
          lastDesktopEffect === "confirmed" ? "confirmed" : lastDesktopEffect === "no_change" ? "confirmed no change" : "unverifiable"
        }{lastDesktopTarget ? ` on “${lastDesktopTarget}”` : ""}. Compare the separate visible result below; an action receipt alone does not prove the full task.</p>}
        {nativeReceipt.evidence.steps?.filter((step) => step.kind === "window_identity_after_capture_refusal"
          && step.same_process === true).slice(-1).map((step, index) =>
          <p key={index}>After the attempted action{step.after_effect ? ` on “${step.after_effect}”` : ""}, a read-only inventory of the same Mac app found the reviewed window {step.reviewed_window_present === false ? "closed" : "still present"}. Supymem did not inspect or act on another window, and this does not prove why the window changed or that the full request succeeded.</p>)}
        {nativeReceipt.evidence.driver === "embedded_cua" && <p>The Supymem-owned driver targeted this window in the background. Accessibility actions can occur without moving your pointer or showing an on-screen cursor when the target is covered or on another Space.</p>}
        {desktopEffects.at(-1)?.cursor_indicator === "overlay_requested" && <p>The driver requested an animated, click-through cursor marker at the target. This marker does not prove the click took effect.</p>}
        {nativeReceipt.evidence.steps?.some((step) => step.kind === "visual_review" && step.after_effect && step.visible_facts?.length) && <>
          <p>Visual checkpoints after dispatched actions (screen observations, not full-task verification):</p>
          <ul>{nativeReceipt.evidence.steps.filter((step) => step.kind === "visual_review" && step.after_effect && step.visible_facts?.length)
            .slice(0, 8).map((step, index) => <li key={index}>After “{step.after_effect}”: {step.visible_facts?.slice(0, 3).join("; ")}</li>)}</ul>
        </>}
        </details>
        {nativeReceipt.evidence.visual_review && <>
          <p>Visual end-state assessment: {nativeReceipt.evidence.visual_review.verdict}. This is a screenshot interpretation, not proof of prior actions or full task completion.</p>
          {nativeReceipt.evidence.visual_review.visible_facts.length > 0 && <ul>
            {nativeReceipt.evidence.visual_review.visible_facts.map((fact, index) => <li key={index}>{fact}</li>)}
          </ul>}
          {nativeReceipt.evidence.visual_review.next_step && <p>Suggested next step: {nativeReceipt.evidence.visual_review.next_step}</p>}
        </>}
        {attestedDesktopPreDispatch(nativeReceipt) &&
          <p>The Mac helper refused this action before dispatch. No result was verified; this task must not be replayed. {nativeReceipt.credits_reconciled === true
            ? "Actual model usage was reconciled."
            : "Actual model usage still needs reconciliation."}</p>}
        {nativeReceipt.failure_code === "computer_use_effect_outcome_uncertain" &&
          <p>Your Mac action could not be verified. Review its saved result before closing this task.</p>}
        {Array.isArray(nativeReceipt.evidence.observed_urls) && nativeReceipt.evidence.observed_urls.length > 0 && <>
          <p>URLs visible in the selected window:</p>
          <ol>{nativeReceipt.evidence.observed_urls.filter((url) => typeof url === "string" && /^https:\/\//.test(url)).map((url) =>
            <li key={url}><a href={url} target="_blank" rel="noopener noreferrer">{url}</a></li>)}</ol>
        </>}
      </section>}
      {reviewedWindowChanged && <ErrorNotice error="This Mac app or window changed after you reviewed it. No action started. Cancel this request and ask Supymem for a fresh review of the current window." source="device-work-orders.orderId.this_mac_app_or" />}
      {sourceReceipt && <section aria-label="Stored source check">
        <p>Stored source version: <code>{sourceReceipt.sha256}</code>. This proves custody only.</p>
        {preflight ? <>
          <p role="status">Contract check: {preflight.status === "blocked_runtime_contract"
            ? "Blocked before runtime QA" : preflight.runtime === "python_product_v1"
              ? "Python product files parse; independent runtime QA is still required"
              : "Basic contract present; independent runtime QA is still required"}.</p>
          {preflight.findings.length > 0 && <ul>{preflight.findings.map(code => <li key={code}>{sourceFindings[code] || code}</li>)}</ul>}
          <p>Saved report digest: <code>{preflight.report_sha256}</code>. No application was built, launched or hosted by this check.</p>
          {preflight.runtime === "static_landing_v1" && preflight.status === "requires_runtime_qa" && <div>
            <p>Run an independent browser check on your exact saved landing source. Nothing is published.</p>
            {!qaRun && <button type="button" disabled={busy} onClick={() => void staticQaAction(false)}>Run independent check</button>}
            {qaRun && <p>Saved QA run: <Link href={revisionBuildHref(ventureId, qaRun.app_id, qaRun.id)}>Open QA progress and founder review</Link>.</p>}
          </div>}
        </> : <p>No source contract report is saved yet. This read-only check will inspect the exact stored version; it cannot verify runtime behavior.</p>}
      </section>}
      {nativeReceipt?.state === "needs_you" && <p role="status" className={styles.instruction}>
        {browserReadOnlyAuthority(order?.manifest) ? "This read-only task reached protected input. No input access is permitted. Stop this task." : needsYouInstruction(nativeReceipt.pause_kind, nativeReceipt.handoff)}
      </p>}
      {isDesktopShell() && nativeReceipt && <BrowserWatchPanel watch={nativeReceipt.watch} state={nativeReceipt.state}
        busy={busy} onStop={() => void nativeAction("cancel")} />}
      <dl className={styles.scope}>
        {order.manifest.app_navigation_verification && <>
          <dt>Requested screen</dt><dd>{order.manifest.app_navigation_verification.route}</dd>
          <dt>Window control</dt><dd>Supymem’s own window only; no pointer or other app access</dd>
        </>}
        {order.manifest.general_browser_verification && <>
          <dt>Browser goal</dt><dd>{browserReadOnlyAuthority(order.manifest) ? "Owned Supymem Chrome only; stops at passwords, 2FA, CAPTCHAs, terms, payments, and sending" : "Owned Supymem Chrome only; pauses for passwords, 2FA, CAPTCHAs, terms, payments, and sending"}</dd>
          <dt>Start URL</dt><dd>{order.manifest.general_browser_verification.start_url}</dd>
          <dt>Browser access</dt><dd>{browserReadOnlyAuthority(order.manifest) ? (order.manifest.general_browser_verification.read_urls?.length
            ? "Read only: no clicks, typing, submissions, downloads or new tabs. Only scoped reads, supported same-origin links and the exact reviewed pages below are permitted."
            : "Read only: no clicks, typing, submissions, downloads or new tabs. Only scoped reads and supported same-origin navigation are permitted.")
            : generalBrowserTypingSummary(order.manifest.general_browser_verification.fill_values)}</dd>
          <BrowserReadUrlsReview urls={order.manifest.general_browser_verification.read_urls} />
        </>}
        {order.manifest.desktop_verification && <>
          {order.manifest.desktop_verification.reviewed_surfaces && <>
            <dt>Explicitly reviewed existing windows</dt><dd><ol>{order.manifest.desktop_verification.reviewed_surfaces.map(surface => <li key={`${surface.process_id}:${surface.window_id}`}>
              {surface.application_bundle}: {surface.window_title || "Untitled"} (PID {surface.process_id}, window {surface.window_id})
            </li>)}</ol>No new app, new window, external script, filesystem or separate browser profile is granted.</dd>
          </>}
          <dt>Selected Mac app</dt><dd>{order.manifest.desktop_verification.application_bundle}</dd>
          <dt>Exact window</dt><dd>{order.manifest.desktop_verification.window_title || "Untitled window"} (ID {order.manifest.desktop_verification.window_id})</dd>
          <dt>Control mode</dt><dd>{order.manifest.desktop_verification.delivery_mode === "foreground_fallback"
            ? "Starts in the background. If controls are unavailable, Supymem may bring this selected app to the front and interrupt your current screen. It remains bound to the reviewed window."
            : order.manifest.desktop_verification.operation === "desktop_goal_v2"
              ? "Background actions in explicitly selected existing windows only; unsupported runtimes refuse this contract"
              : "Background actions in one exact window; the task stops if the app or window changes"}</dd>
          <dt>Screen data</dt><dd>Supymem reads this window and sends bounded, filtered control labels to its decision model. Passwords and known credential shapes are excluded. {order.manifest.desktop_verification.visual_review
            ? "After an uncertain action or at a decision stop, a resized screenshot of this selected window may be sent to OpenAI for visual review. Check this window for private data before approving."
            : "No screenshot is sent to OpenAI for this task."}</dd>
          <dt>Step limit</dt><dd>{order.manifest.desktop_verification.max_steps} observed actions</dd>
          <dt>Text Supymem may type</dt><dd>{order.manifest.desktop_verification.reviewed_text.length
            ? order.manifest.desktop_verification.reviewed_text.join(" · ") : "None approved"}</dd>
        </>}
        {order.manifest.inspection_verification && <>
          <dt>Inspection</dt><dd>Read-only file inventory. Cursor is confirmed installed and is not given an editing session.</dd>
          <dt>Venture workspace</dt><dd>{order.manifest.inspection_verification.workspace_slug}</dd>
          <dt>Read access</dt><dd>This venture workspace only</dd>
          <dt>Write access</dt><dd>None</dd>
        </>}
        {order.manifest.coding_verification && <>
          <dt>Coding provider</dt><dd>{order.manifest.coding_verification.provider}</dd>
          <dt>Venture workspace</dt><dd>{order.manifest.coding_verification.workspace_slug}</dd>
          <dt>Provider read access</dt><dd>{order.manifest.coding_verification.read_scope === "device_filesystem"
            ? "Files on this Mac (provider sandbox limitation)" : order.manifest.coding_verification.read_scope}</dd>
          <dt>Write access</dt><dd>{order.manifest.coding_verification.write_scope.replaceAll("_", " ")}</dd>
          <dt>Network for model-run commands</dt><dd>{order.manifest.coding_verification.command_network}</dd>
        </>}
        <dt>Applications</dt><dd>{order.manifest.applications.join(", ")}</dd>
        <dt>Website scope</dt><dd>{order.manifest.desktop_verification
          ? "Desktop control is limited to the selected app and window, not to website origins. Any text Supymem may type is listed above."
          : order.manifest.origins.join(", ") || "No website origins granted"}</dd>
        <dt>Permitted actions</dt><dd>{order.manifest.actions.map(value => value.replaceAll(".", " ")).join(", ")}</dd>
        <dt>Execution time limit</dt><dd>{order.manifest.duration_seconds} seconds</dd>
        <dt>Review expires</dt><dd>{Number.isFinite(expires) ? new Date(expires).toLocaleString() : "Unavailable"}</dd>
      </dl>
      {order.manifest.general_browser_verification && <>
        <p>Browser step limit: {order.manifest.general_browser_verification.max_steps ?? "Not recorded"} steps. The execution time limit above applies to the whole saved task.</p>
        <BrowserExecutionProfileReview value={order.manifest.general_browser_verification.execution_profile} qualificationGrant={order.browser_qualification_grant} trialBinding={order.browser_qualification_trial_binding} className={styles.scope} />
      </>}
      <div ref={actionsRef} className={styles.actionBar} id="review-mac-actions">
      {order.state === "pending" && !executionExists && !expired && <label className={styles.confirm}><input type="checkbox" checked={acknowledged} disabled={busy || productCapsDirty || generalGrantBlocked || productGrantBlocked} onChange={event => {
        setAcknowledged(event.target.checked);
        if (event.target.checked) window.requestAnimationFrame(() => focusActions());
      }} />I reviewed this task and its permitted access.</label>}
      <div className={styles.actions}>
        {isDesktopShell() && !executionExists && order.state === "approved" && !reviewedWindowChanged && !claimRejected && (!nativeReceipt || nativeReceipt.state === "not_started") && <button ref={startButtonRef} className={!(editPlanLimits && expired) && (order.manifest.coding_loop_verification || order.manifest.deploy_verification) ? styles.primary : undefined} type="button" data-usage-control="mac_start" data-usage-order-id={orderId} disabled={!canStart} onClick={() => void nativeAction("start")}>{order.manifest.deploy_verification ? "Deploy approved version" : order.manifest.coding_loop_verification?.phase === "plan" ? "Create my build plan" : order.manifest.coding_loop_verification ? "Start approved coding run" : "Start approved task on this Mac"}</button>}
        {isDesktopShell() && order.state === "waiting_for_device" && <button type="button" disabled={busy || desktopGrantBlocked || generalGrantBlocked || productGrantBlocked || reviewedWindowChanged} onClick={() => void nativeAction("start")}>Resume on this Mac</button>}
        {isDesktopShell() && !browserReadOnlyAuthority(order.manifest) && order.browser_qualification_grant == null && nativeReceipt?.state === "needs_you" && nativeReceipt.handoff !== "visible" && <button type="button" disabled={busy || generalGrantBlocked} onClick={() => void nativeAction("handoff")}>Open Supymem browser</button>}
        {isDesktopShell() && !browserReadOnlyAuthority(order.manifest) && order.browser_qualification_grant == null && nativeReceipt?.state === "needs_you" && <button type="button" disabled={busy || generalGrantBlocked} onClick={() => void nativeAction("continue")}>{nativeReceipt.pause_kind === "sign_in" ? "Continue" : "Continue after I finished"}</button>}
        {isDesktopShell() && nativeReceipt && !["stopped", "not_started", "reconciliation_required", "interrupted_reconciliation_required", "needs_you"].includes(nativeReceipt.state) && <button type="button" data-usage-control="computer_use_stop" disabled={busy} onClick={() => void nativeAction("cancel")}>Stop background execution</button>}
        {isDesktopShell() && order.manifest.coding_loop_verification && nativeReceipt
          && !["stopped", "not_started", "reconciliation_required", "interrupted_reconciliation_required"].includes(nativeReceipt.state)
          && <button type="button" disabled={busy || nativeReceipt.stop_after_iteration} onClick={() => void nativeAction("soft-stop")}>{nativeReceipt.stop_after_iteration ? "Stopping after this try" : "Stop after this try"}</button>}
        {isDesktopShell() && (order.manifest.coding_loop_verification || order.manifest.deploy_verification)
          && nativeReceipt?.state === "stopped" && (nativeReceipt.credits_reconciled !== true
            || nativeReceipt.pending_plan && nativeReceipt.plan_posted !== true
            || nativeReceipt.iteration_receipts?.some(row => row.posted !== true)
            || order.manifest.deploy_verification && nativeReceipt.deploy_receipt_posted === false)
          && <button type="button" disabled={busy} onClick={() => void nativeAction("reconcile")}>Sync saved result without running again</button>}
        {isDesktopShell() && nativeReceipt?.state === "needs_you" && <button type="button" data-usage-control="computer_use_stop" disabled={busy} onClick={() => void nativeAction("cancel")}>Stop</button>}
        {isDesktopShell() && order.state === "cancelled" && nativeReceipt?.state === "stopped"
          && nativeReceipt.credits_reconciled === false
          && nativeReceipt.failure_code === "computer_use_scope_denied" && <div>
            <p>Supymem will check the saved stop without repeating any action. A saved native effect may have happened even if its receipt is uncertain. Do not retry it. If a model call's actual usage is still unknown after its deadline, Supymem may charge that call's already approved upper bound and release the rest of this task's reservation. The action and task outcomes remain unverified.</p>
            <button type="button" disabled={busy} onClick={() => void nativeAction("reconcile")}>Check final usage and close stopped task</button>
          </div>}
        {isDesktopShell() && order.state === "cancelled" && Boolean(order.claimed_at)
          && nativeReceipt?.failure_code !== "computer_use_effect_outcome_uncertain"
          && (!nativeReceipt || nativeReceipt.state === "reconciliation_required")
          && <button type="button" disabled={busy} onClick={() => void nativeAction("reconcile")}>Reconcile interrupted startup</button>}
        {isDesktopShell() && order.state === "cancelled" && nativeReceipt?.state === "stopped"
          && nativeReceipt.credits_reconciled === false
          && ["computer_use_effect_outcome_uncertain", "native_worker_upstream_rejected"].includes(nativeReceipt.failure_code || "")
          && attestedDesktopPreDispatch(nativeReceipt)
          && <button type="button" disabled={busy} onClick={() => void nativeAction("reconcile")}>Reconcile rejected Mac action</button>}
        {isDesktopShell() && order.state === "cancelled" && nativeReceipt?.state === "stopped"
          && nativeReceipt.credits_reconciled === false
          && nativeReceipt.tool === "desktop"
          && nativeReceipt.failure_code === "computer_use_effect_outcome_uncertain"
          && nativeReceipt.driver_failure_code === "desktop_effect_outcome_uncertain"
          && <button type="button" disabled={busy} onClick={() => void nativeAction("reconcile")}>Close stopped Mac text action as unverified</button>}
        {isDesktopShell() && order.state === "cancelled" && nativeReceipt?.state === "stopped"
          && nativeReceipt.credits_reconciled === false && nativeReceipt.tool === "desktop"
          && nativeReceipt.failure_code === "computer_use_effect_outcome_uncertain"
          && nativeReceipt.operation === "click" && <div>
            <p>Your click could not be verified. Review the saved action before continuing. Closing it does not repeat the click or claim your task succeeded.</p>
            <label className={styles.confirm}><input type="checkbox" checked={uncertainClickAcknowledged}
              disabled={busy} onChange={event => setUncertainClickAcknowledged(event.target.checked)} />I understand the click may have happened and will review its outcome before repeating this action.</label>
            <button type="button" disabled={busy || !uncertainClickAcknowledged}
              onClick={() => void closeUncertainClick()}>Close uncertain click as unverified</button>
          </div>}
        {isDesktopShell() && nativeReceipt?.state === "stopped" && nativeReceipt.credits_reconciled === false
          && nativeReceipt.failure_code === "browser_effect_outcome_uncertain"
          && <button type="button" disabled={busy} onClick={() => void nativeAction("reconcile")}>Reconcile stopped navigation</button>}
        {isDesktopShell() && nativeReceipt?.state === "reconciliation_required" && nativeReceipt.tool === "coding"
          && !(order.state === "cancelled" && nativeReceipt.failure_code === "native_worker_upstream_rejected")
          && <button type="button" disabled={busy} onClick={() => void nativeAction("reconcile")}>Verify stopped coding processes</button>}
        {isDesktopShell() && !order.manifest.coding_loop_verification && !order.manifest.deploy_verification
          && order.state === "stopped" && nativeReceipt?.state === "stopped"
          && nativeReceipt.source_receipt && nativeReceipt.source_storage_state !== "stored"
          && <button type="button" disabled={busy} onClick={() => void nativeAction("source-upload")}>Send saved source for QA</button>}
        {sourceReceipt?.state === "stored" && !preflight
          && <button type="button" disabled={busy} onClick={() => void runSourcePreflight()}>Check stored source contract</button>}
        {order.state === "pending" && !executionExists && <button className={!productCapsDirty && !(editPlanLimits && expired) && (order.manifest.coding_loop_verification || order.manifest.deploy_verification) ? styles.primary : undefined} type="button" data-usage-control="mac_approve" disabled={busy || productCapsDirty || expired || !acknowledged || desktopGrantBlocked || generalGrantBlocked || productGrantBlocked} onClick={() => void act("approve")}>{order.manifest.deploy_verification ? `Approve ${order.manifest.deploy_verification.target} deployment` : order.manifest.coding_loop_verification?.phase === "plan" ? "Approve build plan task" : order.manifest.coding_loop_verification ? "Approve coding limits" : "Confirm task permission"}</button>}
        {order.manifest.coding_loop_verification?.phase === "implement" && nativeReceipt?.state === "stopped"
          && nativeReceipt.task_verified === true && nativeReceipt.credits_reconciled === true && loopIterations.at(-1)?.outcome === "pass"
          && <button className={styles.primary} type="button" disabled={busy || productGrantBlocked}
            onClick={() => void prepareProductNext("preview")}>Review a Vercel preview deployment</button>}
        {order.manifest.deploy_verification?.target === "preview" && deployReceipt
          && nativeReceipt?.state === "stopped" && nativeReceipt.task_verified === true && nativeReceipt.credits_reconciled === true && deployReceipt.project_link_sha256
          && ["deployed", "protected"].includes(deployReceipt.status) && <button className={styles.primary} type="button" disabled={busy || productGrantBlocked}
            onClick={() => void prepareProductNext("production")}>Review a separate production deployment</button>}
        {isDesktopShell() && reconciliationNeedsReview && <div className={styles.confirm}>
          <p>Check the app and stop any remaining activity first. Clearing records your review, not verified execution; check the saved result before requesting another task.</p>
          <button type="button" data-usage-control="mac_result_looks_right" disabled={busy}
            onClick={() => void act("acknowledge")}>I've checked; clear this task</button>
        </div>}
        {!reconciliationNeedsReview && !nativeReceipt?.reconciliation_reviewed_at && !order.manifest.coding_loop_verification && !order.manifest.deploy_verification && !order.reviewed_at && (order.state === "stopped" || (Boolean(order.claimed_at) && (nativeReceipt?.state === "stopped" || order.state === "cancelled"))) && <>
          <button type="button" data-usage-control="mac_result_looks_right" disabled={busy} onClick={() => void act("acknowledge")}>Looks right, mark done</button>
          <button type="button" data-usage-control="mac_result_something_wrong" disabled={busy} onClick={() => prefillChat(
            macWrongPrefill(order.manifest.title, macResultHeadline(nativeReceipt?.evidence?.visual_review, nativeReceipt)))}>Something’s wrong</button>
        </>}
        {(["pending", "approved", "claimed", "waiting_for_device"].includes(order.state)) && nativeReceipt?.state !== "stopped" && <button type="button" data-usage-control="mac_cancel" disabled={busy} onClick={() => void act("cancel")}>{order.claimed_at ? "Request cancellation" : "Cancel request"}</button>}
        {!editPlanLimits && !desktopGrantBlocked && !generalGrantBlocked && !executionExists && (order.state === "cancelled" || (expired && (order.state === "pending" || order.state === "approved"))) && <button type="button" disabled={busy} onClick={() => void renew()}>Prepare a fresh request</button>}
        {!desktopGrantBlocked && !generalGrantBlocked && !executionExists && claimRejected && order.state === "approved" && !expired && <button type="button" disabled={busy} onClick={() => void freshAfterRejection()}>Prepare a fresh request</button>}
        <Link href={`/v/${ventureId}/plan`}>Return to the execution plan</Link>
      </div>
      </div>
    </>}
  </section>;
}
