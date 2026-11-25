"use client";

import { useEffect } from "react";
import { api } from "@/lib/client";
import { isLocalConsole } from "@/lib/localMode";
import { desktopInvoke, desktopInvokeQuiet, desktopListen, isDesktopShell } from "@/lib/desktop";
import { closedNativeAuthTransition, retainNativeTelemetryOwner } from "@/lib/nativeAuthTransitionDiagnostic";
import { diagnosticUsageFeature, usageFeature } from "@/lib/usageFeature";
import { MAC_START_EXPOSURE_EVENT, USAGE_CONTROL_EVENT, macStartOrderId, usageControl } from "@/lib/usageControl";
import { enqueueTelemetry, flushQueuedTelemetry, retryQueuedOnReconnect, type QueuedTelemetry } from "@/lib/usageDelivery";
import { reportUsageCollectorState } from "@/lib/usageCollectorState";
import { emitVisibleActivation, qualifiesForExposure, syncExposureObservation, viewportVisibleRatio } from "@/lib/usageExposure";
import { interactionBackedScreenView, shouldCollectUsageEvent, type UsageEvent } from "@/lib/usageVisibility";
import { consumeChatNavigation } from "@/lib/chatNavigationUsage";
import { CATALOG } from "@/lib/featureCatalog";
import { CONTROL_OUTCOME_EVENT, armControlOutcome, type ControlOutcome } from "@/lib/controlOutcome";
import { nativeShellTelemetry } from "@/lib/nativeShellEvents";
import { UI_DIAGNOSTIC_EVENT, announceDiagnostic, closedApiDiagnosticOperation, closedApiDiagnosticTransport, closedNative503Origin, closedNativeDiagnosticCommand, diagnosticFingerprint, sanitizeDiagnostic,
  validDiagnosticReference, type DiagnosticCode, type DiagnosticContext } from "@/lib/uiDiagnostics";
import { startErrorObserver, type UserErrorDetail } from "@/lib/userError";
import { OBSERVABILITY_EVENT, consoleRoute, observeFounder, startDeadEndObserver, type Observation } from "@/lib/observability";

// Bounded offline queue. Persists only closed telemetry envelopes (event name,
// feature/source labels, platform, app version, session id, nonce). No chat text,
// screen content, URLs, or credentials. Mac Start alone may include an opaque
// work-order ID so its click can be joined to a saved, authenticated receipt.
const STORAGE_KEY = "supymem.usage.queue.v1";
const MAX_QUEUE = 256;

let owner: string | undefined;
let sessionId = "";
let lastView = "";
let lastActive = 0;
let lastTrustedInteractionAt = 0;
let queue: QueuedTelemetry[] = [];
let flushing = false;
let appVersion: string | undefined;
let versionRequested = false;
/** Desktop app version once the native shell has reported it (used by Report a problem). */
export const currentAppVersion = (): string | undefined => appVersion;
const recentDiagnostics = new Map<string, number>();
const suppressedTransients = new Map<string, number>();

function readStored(subject: string | undefined): QueuedTelemetry[] {
  if (!subject || typeof localStorage === "undefined") return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as { subject?: string; items?: QueuedTelemetry[] };
    if (parsed.subject !== subject || !Array.isArray(parsed.items)) return [];
    return parsed.items
      .filter((item) => item && typeof item === "object" && item.body && typeof item.body === "object")
      .slice(0, MAX_QUEUE)
      .map((item) => ({ body: item.body, attempts: Math.max(0, Number(item.attempts) || 0),
        nextAttemptAt: Number(item.nextAttemptAt) || 0 }));
  } catch {
    return [];
  }
}

function writeStored(subject: string | undefined, items: QueuedTelemetry[]) {
  if (typeof localStorage === "undefined") return;
  try {
    if (!subject || !items.length) {
      localStorage.removeItem(STORAGE_KEY);
      return;
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ subject, items: items.slice(0, MAX_QUEUE) }));
  } catch {
    /* Quota or private mode: fall back to the in-memory queue only. */
  }
}

function persist() {
  writeStored(owner, queue);
}

async function flush() {
  if (flushing || !navigator.onLine) return;
  flushing = true;
  try {
    await flushQueuedTelemetry({ queue: () => queue, owner: () => owner, persist,
      send: body => api("/api/telemetry", { method: "POST", body: JSON.stringify(body) }),
      now: Date.now, online: () => navigator.onLine });
  } finally { flushing = false; }
}

export function UsageTracker({ subject, pathname, ventureId }: { subject?: string; pathname: string; ventureId?: string }) {
  useEffect(() => {
    if (isLocalConsole() || !subject) return;
    function observation(event: Event) {
      const detail = (event as CustomEvent<Observation>).detail;
      if (!detail || owner !== subject) return;
      enqueueTelemetry(queue, { attempts: 0, body: { event: detail.event, event_id: crypto.randomUUID(),
        context_subject: subject, venture_id: detail.event === "ui.dead_end" ? ventureId || null : detail.ventureId,
        ...(detail.event !== "ui.dead_end" && detail.workspaceId ? { workspace_id: detail.workspaceId } : {}),
        props: { feature: diagnosticUsageFeature(pathname), route: consoleRoute(pathname),
          ...(detail.event === "ui.dead_end" ? { code: detail.code } : { step: detail.step }) } } }, MAX_QUEUE);
      persist(); void flush();
    }
    const failedAction = (event: Event) => {
      const detail = (event as CustomEvent<ControlOutcome>).detail;
      if (detail && !detail.ok) observation(new CustomEvent(OBSERVABILITY_EVENT,
        { detail: { event: "ui.dead_end", code: "action_failed" } }));
    };
    window.addEventListener(OBSERVABILITY_EVENT, observation);
    window.addEventListener(CONTROL_OUTCOME_EVENT, failedAction);
    const stop = startDeadEndObserver();
    return () => { stop(); window.removeEventListener(OBSERVABILITY_EVENT, observation);
      window.removeEventListener(CONTROL_OUTCOME_EVENT, failedAction); };
  }, [subject, pathname, ventureId]);
  useEffect(() => {
    if (isLocalConsole()) return;
    const health = (state: Parameters<typeof reportUsageCollectorState>[0]) => {
      try { reportUsageCollectorState(state, window.localStorage); }
      catch { /* A denied WebView storage read cannot break usage collection. */ }
    };
    // Keep an actor-scoped diagnostic across a temporary native reauthorization.
    // An explicit signed_out event or a verified different actor still clears it.
    if (owner !== subject && !retainNativeTelemetryOwner(isDesktopShell(), owner, subject)) {
      owner = subject;
      queue = readStored(subject);
      sessionId = crypto.randomUUID();
      lastView = "";
      lastActive = 0;
      lastTrustedInteractionAt = 0;
      persist();
    }
    if (isDesktopShell() && !versionRequested) {
      versionRequested = true;
      void desktopInvoke<string>("app_version").then(value => {
        if (value && /^\d{1,4}\.\d{1,4}\.\d{1,4}$/.test(value)) appVersion = value;
      }).catch(() => { /* Missing version remains unknown, never inferred. */ });
    }
    const feature = usageFeature(pathname);
    if (!subject || !feature) {
      health(!subject ? "waiting_for_subject" : "unmapped_route");
      return;
    }
    health(document.hidden ? "hidden" : "collecting");
    let stopped = false;
    function record(event: UsageEvent,
                    control?: { controlId: string; feature: string }, orderId?: string | null,
                    interactionBacked = false, extra?: Record<string, unknown>) {
      // Deliberate clicks remain observable when macOS reports the WebView
      // hidden during a reviewed background interaction. A route view needs
      // either a visible document or a recent trusted native interaction.
      if (stopped) return;
      if (owner !== subject) { health("subject_mismatch"); return; }
      if (!shouldCollectUsageEvent(document.hidden, event, interactionBacked)) {
        health("hidden"); return;
      }
      health(document.hidden ? "hidden" : "collecting");
      enqueueTelemetry(queue, { attempts: 0, body: { event, event_id: crypto.randomUUID(), context_subject: subject,
        venture_id: ventureId || null,
        props: { feature: control?.feature || feature, platform: isDesktopShell() ? "macos" : "web",
          app_version: appVersion || null, session_id: sessionId,
          ...(event === "ui.navigation" ? { source: "chat" } : {}),
          ...(control && (event === "ui.control_activate" || event === "ui.control_exposure" || event === "ui.control_outcome") ? { control_id: control.controlId } : {}),
          ...(orderId ? { order_id: orderId } : {}), ...extra } } }, MAX_QUEUE);
      persist();
      void flush();
    }
    const viewKey = JSON.stringify([pathname, ventureId || null]);
    function view() {
      const interactionBacked = interactionBackedScreenView(document.hidden, isDesktopShell(), lastTrustedInteractionAt, Date.now());
      if ((!document.hidden || interactionBacked) && lastView !== viewKey) {
        lastView = viewKey;
        record("ui.screen_view", undefined, null, interactionBacked);
      }
    }
    function markTrustedInteraction(event: Event) {
      if (!event.isTrusted || !isDesktopShell()) return;
      lastTrustedInteractionAt = Date.now();
      // The same interaction may navigate to a newly mounted route.
      view();
    }
    function active(event: Event) {
      markTrustedInteraction(event);
      if (!document.hidden && Date.now() - lastActive >= 60000) { lastActive = Date.now(); record("session.active"); }
    }
    function interaction(event: MouseEvent) {
      markTrustedInteraction(event);
      // Count a deliberate control activation, without reading its text,
      // value, URL, or surrounding DOM. This measures usage, not success.
      if (!(event.target instanceof Element)) return;
      const control = event.target.closest("a[href], button, [role='button']");
      if (control && !control.matches(":disabled, [aria-disabled='true']")) {
        const tagged = control.closest("[data-usage-control]");
        const named = usageControl(tagged?.getAttribute("data-usage-control") || null);
        // The shared composer announces accepted button and Enter submits itself.
        if (named?.controlId === "chat_send" || named?.controlId === "computer_use_send") return;
        if (named && tagged) {
          emitVisibleActivation(named.controlId, exposed, event.isTrusted,
            viewportVisibleRatio(tagged.getBoundingClientRect(), { width: window.innerWidth, height: window.innerHeight }),
            () => {
              record("ui.control_exposure", named, null, true);
              if (exposureObserver) syncExposureObservation(tagged, observing, exposureObserver, false);
            },
            () => {
              armControlOutcome(named.controlId);
              record("feature.used", named);
              record("ui.control_activate", named,
                named.controlId === "mac_start" ? macStartOrderId(tagged.getAttribute("data-usage-order-id")) : null);
            });
        } else record("feature.used");
      }
    }
    function selection(event: Event) {
      if (!(event.target instanceof Element)) return;
      const control = event.target.closest("select[data-usage-control]");
      if (!control || control.matches(":disabled, [aria-disabled='true']")) return;
      const named = usageControl(control.getAttribute("data-usage-control"));
      if (named) emitVisibleActivation(named.controlId, exposed, event.isTrusted,
        viewportVisibleRatio(control.getBoundingClientRect(), { width: window.innerWidth, height: window.innerHeight }),
        () => {
          record("ui.control_exposure", named, null, true);
          if (exposureObserver) syncExposureObservation(control, observing, exposureObserver, false);
        },
        () => { armControlOutcome(named.controlId); record("feature.used", named); record("ui.control_activate", named); });
    }
    function explicitControl(event: Event) {
      const id = (event as CustomEvent<{ controlId?: string }>).detail?.controlId;
      const named = usageControl(id || null);
      if (named) { armControlOutcome(named.controlId); record("feature.used", named); record("ui.control_activate", named); }
    }
    function controlOutcome(event: Event) {
      const detail = (event as CustomEvent<ControlOutcome>).detail;
      const named = usageControl(detail?.controlId || null);
      if (!named) return;
      record("ui.control_outcome", named, null, true, detail.ok ? { outcome: "ok" }
        : { outcome: "failed", error_status: detail.status, ...(detail.serverCode ? { error_server_code: detail.serverCode } : {}) });
    }
    // Events the native shell recorded (tray, mascot, menus, updater, permissions, Mac task failures), including
    // any buffered while signed out. Drained only here, where the account is verified, then sent as usual.
    async function drainNative() {
      if (!isDesktopShell() || stopped || owner !== subject) return;
      const items = nativeShellTelemetry(await desktopInvokeQuiet<unknown[]>("drain_native_events"));
      for (const item of items) {
        if (owner !== subject) return;
        enqueueTelemetry(queue, { attempts: 0, body: { event: item.event, event_id: crypto.randomUUID(), context_subject: subject,
          venture_id: null, props: { feature: item.feature, platform: "macos", app_version: appVersion || null,
            session_id: sessionId, ...item.props } } }, MAX_QUEUE);
      }
      if (items.length) { persist(); void flush(); }
    }
    function macStartExposure(event: Event) {
      const orderId = macStartOrderId((event as CustomEvent<{ orderId?: string }>).detail?.orderId || null);
      if (orderId) record("ui.control_exposure", { controlId: "mac_start", feature: "computer_use" }, orderId);
    }
    const wake = () => {
      health(document.hidden ? "hidden" : "collecting");
      view();
      if (!document.hidden && exposureObserver) {
        // An intersection may have been delivered while the WebView was hidden.
        // Re-arm on return to the foreground, rather than on every DOM mutation.
        for (const element of document.querySelectorAll("[data-usage-control]")) {
          syncExposureObservation(element, observing, exposureObserver, false);
        }
        scanControls();
      }
      void flush();
    };
    // Exposure means an enabled, named control entered the visible viewport.
    // Record each ID once per route visit; do not inspect its label or value.
    const exposed = new Set<string>();
    const observing = new WeakSet<Element>();
    const exposureObserver = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!qualifiesForExposure(entry.isIntersecting, entry.intersectionRatio) || document.hidden || !(entry.target instanceof Element)) continue;
        const element = entry.target;
        if (element.matches(":disabled, [aria-disabled='true']")) continue;
        const named = usageControl(element.getAttribute("data-usage-control"));
        if (named && named.controlId !== "mac_start" && !exposed.has(named.controlId)) {
          exposed.add(named.controlId);
          record("ui.control_exposure", named);
          syncExposureObservation(element, observing, exposureObserver!, false);
        }
      }
    }, { threshold: 0.25 });
    function scanControls() {
      if (!exposureObserver) return;
      for (const element of document.querySelectorAll("[data-usage-control]")) {
        const named = usageControl(element.getAttribute("data-usage-control"));
        syncExposureObservation(element, observing, exposureObserver,
          Boolean(named && named.controlId !== "mac_start" && !exposed.has(named.controlId)
            && !element.matches(":disabled, [aria-disabled='true']")));
      }
    }
    const controlChanges = exposureObserver && typeof MutationObserver !== "undefined"
      ? new MutationObserver(scanControls) : null;
    const reconnected = () => {
      // A real network recovery should not wait out a previous 5-minute backoff.
      retryQueuedOnReconnect(queue, persist);
      wake();
    };
    const changed = (event: Event) => {
      const next = (event as CustomEvent<{ subject?: string }>).detail?.subject;
      if (next !== owner && !retainNativeTelemetryOwner(isDesktopShell(), owner, next)) {
        owner = next;
        queue = readStored(next);
        sessionId = crypto.randomUUID();
        lastView = "";
        lastActive = 0;
        lastTrustedInteractionAt = 0;
        persist();
      }
      if (next !== subject) health("subject_mismatch");
    };
    // The route changed after a saved chat navigation reply. This is a
    // navigation receipt, not a visible screen view or saved feature read.
    if (consumeChatNavigation(pathname)) record("ui.navigation");
    view();
    window.addEventListener("pointerdown", active, { passive: true });
    window.addEventListener("keydown", active);
    // Observe the activation attempt before React handlers synchronously disable
    // or unmount the control (for example Chat Send sets pending immediately).
    // A disabled control at click time is still excluded by interaction().
    document.addEventListener("click", interaction, { capture: true, passive: true });
    document.addEventListener("change", selection, { capture: true });
    scanControls();
    controlChanges?.observe(document.body, { childList: true, subtree: true, attributes: true,
      attributeFilter: ["disabled", "aria-disabled", "data-usage-control"] });
    window.addEventListener(USAGE_CONTROL_EVENT, explicitControl);
    window.addEventListener(CONTROL_OUTCOME_EVENT, controlOutcome);
    window.addEventListener(MAC_START_EXPOSURE_EVENT, macStartExposure);
    window.addEventListener("online", reconnected);
    window.addEventListener("supymem-session-changed", changed);
    document.addEventListener("visibilitychange", wake);
    void drainNative();
    const timer = setInterval(() => { void drainNative(); void flush(); }, 15000);
    return () => { stopped = true; clearInterval(timer);
      window.removeEventListener("pointerdown", active); window.removeEventListener("keydown", active);
      document.removeEventListener("click", interaction, true);
      document.removeEventListener("change", selection, true);
      controlChanges?.disconnect(); exposureObserver?.disconnect();
      window.removeEventListener(USAGE_CONTROL_EVENT, explicitControl);
      window.removeEventListener(CONTROL_OUTCOME_EVENT, controlOutcome);
      window.removeEventListener(MAC_START_EXPOSURE_EVENT, macStartExposure);
      window.removeEventListener("online", reconnected); window.removeEventListener("supymem-session-changed", changed);
      document.removeEventListener("visibilitychange", wake); };
  }, [subject, pathname, ventureId]);
  useEffect(() => {
    if (isLocalConsole() || !subject) return;
    const feature = diagnosticUsageFeature(pathname);
    let stopNativeAuth: (() => void) | undefined;
    let disposed = false;
    if (isDesktopShell()) {
      void desktopListen("supymem-native-auth", (value) => {
        if (value && typeof value === "object" && (value as { state?: unknown }).state === "signed_out") {
          owner = undefined;
          queue = [];
          lastTrustedInteractionAt = 0;
          persist();
          return;
        }
        const transition = closedNativeAuthTransition(value);
        if (!transition || owner !== subject) return;
        const fingerprint = `${subject}|auth_transition|${transition.reason}|${transition.httpClass}`;
        const now = Date.now();
        if (now - (recentDiagnostics.get(fingerprint) || 0) < 60_000) return;
        recentDiagnostics.set(fingerprint, now);
        enqueueTelemetry(queue, { attempts: 0, body: { event: "ui.error", event_id: crypto.randomUUID(),
          context_subject: subject, venture_id: null,
          props: { feature: "shell", platform: "macos", app_version: appVersion || null,
            session_id: sessionId, error_code: "native_error", error_context: "native",
            error_text: "Mac native_failure", error_auth_reason: transition.reason,
            error_auth_http_class: transition.httpClass } } }, MAX_QUEUE);
        // The auth gate may unmount this tracker before a request can complete.
        // The bounded, actor-scoped queue flushes only after that same account reconnects.
        persist();
        void flush();
      }).then((stop) => { if (disposed) stop(); else stopNativeAuth = stop; });
    }
    function diagnostic(event: Event) {
      const detail = (event as CustomEvent<{ code?: DiagnosticCode; context?: DiagnosticContext; text?: string; ref?: string; jobId?: string; operation?: string; origin?: string; transport?: string; orderId?: string; nativeCommand?: string;
        route?: string | null; status?: number; serverCode?: string | null; feature?: string | null; severity?: string }>).detail;
      if (detail?.context === "ui" || detail?.context === "voice") {
        // Errors the founder SAW (inline text, banner, voice): exact scrubbed message, source, intent, time. Deduped by the reporter.
        const u = detail as unknown as UserErrorDetail;
        if (owner !== subject || (u.code !== "ui_error" && u.code !== "voice_error")) return;
        enqueueTelemetry(queue, { attempts: 0, body: { event: "ui.error", event_id: crypto.randomUUID(),
          context_subject: subject, venture_id: ventureId || null,
          props: { feature: u.context === "voice" ? "voice_chat" : feature, platform: isDesktopShell() ? "macos" : "web",
            app_version: appVersion || null, session_id: sessionId, error_code: u.code, error_context: u.context,
            error_text: u.context === "voice" ? "Voice error shown" : "UI error shown", error_source: u.context, error_intent: "unknown",
            error_page: consoleRoute(pathname), error_occurred_at: u.occurredAt,
            ...(u.severity === "transient" ? { error_severity: "transient" } : {}),
            ...(u.serverCode ? { error_server_code: u.serverCode } : {}),
            ...(u.status !== undefined ? { error_status: u.status } : {}),
            ...(validDiagnosticReference(u.requestId) ? { error_request_id: u.requestId } : {}) } } }, MAX_QUEUE);
        persist();
        void flush();
        return;
      }
      if (!detail || !["network_error", "server_error", "request_error", "native_error", "render_error", "runtime_error", "integrity_error", "job_error"].includes(detail.code || "")) return;
      if (!["api", "native", "render", "runtime", "promise", "integrity", "job"].includes(detail.context || "")) return;
      if (owner !== subject) return;
      const text = sanitizeDiagnostic(detail.text);
      const jobId = detail.code === "job_error" && detail.context === "job" && /^job_[A-Za-z0-9_-]{1,64}$/.test(detail.jobId || "") ? detail.jobId : null;
      const operation = detail.context === "api" ? closedApiDiagnosticOperation(detail.operation) : null;
      const origin = detail.context === "api" ? closedNative503Origin(detail.origin) : null;
      const transport = detail.context === "api" ? closedApiDiagnosticTransport(detail.transport) : null;
      const nativeCommand = detail.context === "native" ? closedNativeDiagnosticCommand(detail.nativeCommand) : null;
      const orderId = detail.context === "api" && feature === "computer_use" && operation?.startsWith("mac_")
        && /^dwo_[A-Za-z0-9_-]{1,64}$/.test(detail.orderId || "") ? detail.orderId : null;
      // The calling feature comes from the failed route (server catalog); a page-level label is the fallback.
      const route = detail.context === "api" && typeof detail.route === "string" && /^\/[a-z0-9_{}./-]{1,80}$/.test(detail.route) ? detail.route : null;
      const status = route && Number.isInteger(detail.status) && detail.status! >= 0 && detail.status! <= 599 ? detail.status : null;
      const serverCode = route && typeof detail.serverCode === "string" && /^[a-z][a-z0-9_]{0,39}$/.test(detail.serverCode) ? detail.serverCode : null;
      const errorFeature = detail.context === "api" && detail.feature && CATALOG.features.includes(detail.feature) ? detail.feature : feature;
      if (detail.severity !== "transient") observeFounder({ event: "ui.dead_end", code: "action_failed" });
      const fingerprint = `${detail.severity === "transient" ? "transient|" : ""}${subject}|${diagnosticFingerprint(detail.code!, detail.context!, text, errorFeature!, detail.ref, jobId, operation, origin, transport, orderId, nativeCommand, route)}`;
      const now = Date.now();
      const transient = detail.severity === "transient";
      // A throttled transient is counted, and the next recorded one carries repeat_count.
      if (now - (recentDiagnostics.get(fingerprint) || 0) < 60_000) {
        if (transient) suppressedTransients.set(fingerprint, (suppressedTransients.get(fingerprint) || 0) + 1);
        return;
      }
      const repeatCount = transient ? (suppressedTransients.get(fingerprint) || 0) + 1 : 0;
      suppressedTransients.delete(fingerprint);
      recentDiagnostics.set(fingerprint, now);
      if (recentDiagnostics.size > 100) recentDiagnostics.delete(recentDiagnostics.keys().next().value!);
      enqueueTelemetry(queue, { attempts: 0, body: { event: "ui.error", event_id: crypto.randomUUID(),
        context_subject: subject, venture_id: ventureId || null,
        props: { feature: errorFeature, platform: isDesktopShell() ? "macos" : "web", app_version: appVersion || null,
          session_id: sessionId, error_code: detail.code, error_context: detail.context, error_text: text,
          ...(route ? { error_route: route } : {}),
          ...(status !== null ? { error_status: status } : {}),
          ...(serverCode ? { error_server_code: serverCode } : {}),
          ...(jobId ? { job_id: jobId } : {}),
          ...(operation ? { error_operation: operation } : {}),
          ...(origin ? { error_origin: origin } : {}),
          ...(transport ? { error_transport: transport } : {}),
          ...(nativeCommand ? { error_native_command: nativeCommand } : {}),
          ...(orderId ? { error_order_id: orderId } : {}),
          ...(transient ? { error_severity: "transient", error_repeat_count: Math.min(repeatCount, 100000) } : {}),
          ...(validDiagnosticReference(detail.ref) ? { error_ref: detail.ref } : {}) } } }, MAX_QUEUE);
      persist();
      void flush();
    }
    const runtime = (event: ErrorEvent) => announceDiagnostic("runtime_error", "runtime", event.error || event.message);
    const rejection = (event: PromiseRejectionEvent) => announceDiagnostic("runtime_error", "promise", event.reason);
    window.addEventListener(UI_DIAGNOSTIC_EVENT, diagnostic);
    window.addEventListener("error", runtime);
    window.addEventListener("unhandledrejection", rejection);
    const stopObserver = startErrorObserver();
    return () => { disposed = true; stopObserver(); stopNativeAuth?.(); window.removeEventListener(UI_DIAGNOSTIC_EVENT, diagnostic);
      window.removeEventListener("error", runtime); window.removeEventListener("unhandledrejection", rejection); };
  }, [subject, pathname, ventureId]);
  return null;
}
