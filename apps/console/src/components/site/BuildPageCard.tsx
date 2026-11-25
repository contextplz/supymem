"use client";

import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { routeInChat } from "@/lib/chatRouteAction";

import { ChatAnchor } from "@/components/chat/ChatLink";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { OneCard } from "@/components/primitives/OneCard";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { isLocalConsole } from "@/lib/localMode";
import { sameGodModeScope } from "@/lib/godModeRequestScope";
import { apiFailure } from "@/lib/apiFailure";
import { USAGE_LIMIT_MESSAGE } from "@/lib/credits";
import { canConfirmValidatedIdea, type ValidatedIdea } from "@/lib/validationCopy";
import { RUN_STATUS_REFRESH } from "@/lib/runStatusPresentation";
import { desktopInvoke, isDesktopShell } from "@/lib/desktop";
import { activeMacTaskReviewPath, macTaskPrepareFailure } from "@/lib/macTaskPrepareFailure";
import { buildInstruction, pageBuildView, pickTool, type PageBuildOrder, type PageBuildTool, type PageBuilderProfile, type PagePublication } from "@/lib/pageBuild";

import { NextStepButton, useNextStep } from "@/components/layout/NextStepStrip";
import { ServerCandidateQa } from "./ServerCandidateQa";
import { PageChoices } from "./PageChoices";
import { SiteAddress } from "./SiteAddress";
import addressStyles from "./SiteAddress.module.css";
import styles from "./BuildPageCard.module.css";

const NAME = { claude: "Claude Code", codex: "Codex" } as const;
const CODING_TOOLS_SETTINGS = "/settings/local#coding-tools";

/** Plain reasons for a build that could not start or stopped; never a cost review. */
export function pageBuildProblem(code?: string, message?: string): string {
  if (code === "pagebuilder_coding_tool_required")
    return message?.trim() || "Open Settings > Keys and connections > Coding tools, connect your tool and allow Charlie, then retry your page.";
  if (code === "capability_unavailable" && isLocalConsole())
    return typeof message === "string" && message.trim() ? message
      : "In Settings > Capabilities, switch on Draft landing pages. In Settings > Keys and connections > Your AI provider, save your model-provider key.";
  if (["pagebuilder_plan_rejected", "pagebuilder_plan_json_invalid", "pagebuilder_invalid_output", "pagebuilder_output_invalid", "pagebuilder_output_truncated"].includes(code || ""))
    return "The landing page builder could not produce a complete, usable landing page plan. Your idea and research are saved. Try building again.";
  if (code === "pagebuilder_outcome_uncertain") return "A landing page-building call stopped without a confirmed result. Your saved work is kept; wait for reconciliation before building again.";
  if (code === "pagebuilder_provider_rate_limited") return "The landing page builder's model is temporarily busy. Your saved work is kept. Try building again later.";
  if (code === "pagebuilder_candidate_policy_obsolete" || code === "pagebuilder_candidate_invalid") return "The saved landing page no longer meets current build requirements. Build a new version.";
  if (code === "pagebuilder_new_version_unavailable") return "A new landing page build could not start from this saved request. Refresh the saved landing page status and wait for any running request to finish before building again.";
  if (code === "pagebuilder_reference_unavailable") return "The landing page source could not be safely saved. Retry saving the existing source; no new generation is needed.";
  if (code === "pagebuilder_insufficient_credits") return USAGE_LIMIT_MESSAGE;
  if (code === "pagebuilder_provider_or_credits_unavailable" || code === "pagebuilder_pricing_unavailable")
    return isLocalConsole() ? "Check your API key and planning model in Settings > Keys and connections > Your AI provider, then build again."
      : "Your landing page connection is unavailable. Check Settings, then build again.";
  if (code === "pagebuilder_spend_limit" && isLocalConsole())
    return "Your monthly spending limit stopped this build. In Settings > Keys and connections > Your monthly spend cap, raise or remove Monthly limit (USD), then retry.";
  if (code?.startsWith("pagebuilder_waitlist")) return "Turn your waitlist back on, then build again.";
  if (code === "pagebuilder_confirmed_revision_required") return "Confirm your idea first, then build your landing page.";
  if (code === "pagebuilder_renderer_unavailable") return "The landing page preview could not start. Your idea is saved. Try building again.";
  if (code === "pagebuilder_build_limit") return "Three ready landing page versions have been delivered for this idea. Your saved landing pages are safe. Review them, update your idea for a new build, or continue to Find customers.";
  if (code === "pagebuilder_attempt_limit") return "Landing page building reached the eight-attempt safety limit for this idea version. Your idea, research and saved work are kept. Continue to Find customers; no further build is started or charged.";
  return "The landing page build stopped before finishing. Your idea and research are saved.";
}

/** Where the founder fixes a build that cannot start; null when building or re-reading again is the next step. */
export function pageBuildFix(code: string | undefined, ventureId: string, message?: string): { label: string; href: string; control: string } | null {
  const id = encodeURIComponent(ventureId);
  if (code === "pagebuilder_coding_tool_required")
    return { label: "Connect your coding tool", href: CODING_TOOLS_SETTINGS, control: "website_open_settings" };
  if (isLocalConsole()) {
    if (code === "pagebuilder_insufficient_credits") return null;
    if (code === "pagebuilder_spend_limit")
      return { label: "Open your monthly spend cap", href: "/settings/local#spend-title", control: "website_open_settings" };
    if (code?.startsWith("pagebuilder_") && message?.includes("Settings > Keys and connections > Your AI provider"))
      return { label: "Open your AI provider settings", href: "/settings/local#providers-title", control: "website_open_settings" };
    // shortcut: capability errors carry only copy, use a setting identifier when the API supplies one.
    if (code === "capability_unavailable" && !message?.startsWith("In Settings > Keys and connections > Your AI provider"))
      return { label: "Open Capabilities", href: "/settings/local/capabilities#landing_pages-title", control: "website_open_settings" };
    if (code === "capability_unavailable" || ["pagebuilder_provider_or_credits_unavailable", "pagebuilder_pricing_unavailable",
      "pagebuilder_provider_rejected", "pagebuilder_byok_unavailable", "pagebuilder_resources_unavailable",
      "pagebuilder_resources_changed", "pagebuilder_provider_offline"].includes(code || ""))
      return { label: "Open your AI provider settings", href: "/settings/local#providers-title", control: "website_open_settings" };
  }
  if (code === "pagebuilder_confirmed_revision_required")
    return { label: "Continue clarification", href: `/v/${id}/idea`, control: "website_continue_clarification" };
  if (code === "pagebuilder_build_limit")
    return { label: "Review your idea", href: `/v/${id}/idea`, control: "website_continue_clarification" };
  if (code === "pagebuilder_insufficient_credits" || code === "pagebuilder_provider_or_credits_unavailable" || code === "pagebuilder_pricing_unavailable")
    return { label: "Open Settings", href: `/v/${id}/settings`, control: "website_open_settings" };
  if (code?.startsWith("pagebuilder_waitlist")) return { label: "Turn on your waitlist", href: `/gtm?venture=${id}`, control: "website_open_waitlist" };
  return null;
}

/** Save the chosen page address; returns a plain-words problem, or "" when it is saved. */
async function saveAddress(ventureId: string, slug: string): Promise<string> {
  const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/site-address`, { method: "PUT", body: JSON.stringify({ slug, target: "pagebuilder" }) });
  if (result.ok) return "";
  const detail = (result.data as { detail?: { message?: string; suggestion?: string } } | undefined)?.detail;
  const hint = detail?.suggestion ? ` Try ${detail.suggestion}.` : "";
  return typeof detail?.message === "string" ? detail.message + hint : "The landing page address could not be saved. Try again.";
}

const wantsBuild =() => /[?&]build=1(?:&|$)/.test(window.location.search || "");
const dropBuildParam = () => {
  try { window.history?.replaceState(null, "", window.location.pathname); } catch { /* the URL can keep the flag; the server dedupes */ }
};

const publishSelection = (profile: PageBuilderProfile | null) => {
  const job = profile?.current, candidate = job?.server_publish_ready ? job.server_candidates?.[0] : null;
  return job && candidate ? { job_id: job.job_id, variant: candidate.variant,
    plan_id: profile?.plan_id ?? null, plan_task_id: profile?.task_id ?? null } : null;
};

const previewSelection = (profile: PageBuilderProfile | null) => {
  const job = profile?.current;
  const candidate = job?.server_publish_ready ? job.server_candidates?.[0]
    : job?.native_ready ? job.candidates[0]
    : job?.state === "ready" && !job.original_creator_required ? { variant: 0 } : null;
  return job && candidate ? { job_id: job.job_id, variant: candidate.variant } : null;
};

function checkedPublication(value: unknown): PagePublication | null {
  const saved = value as PagePublication | null;
  return saved && typeof saved.run_id === "string" && /^[A-Za-z0-9_-]+$/.test(saved.run_id)
    && ["checking", "publishing", "live", "failed"].includes(saved.state)
    && (saved.live_url === null || typeof saved.live_url === "string" && /^https:\/\/[A-Za-z0-9.-]+(?:\/[^\s]*)?$/.test(saved.live_url))
    ? saved : null;
}

/** Card 1 on Website: the founder's own Claude Code / Codex builds the page (no template pages). */
export function BuildPageCard({ ventureId, jobId, onSelection }: { ventureId: string; jobId?: string; onSelection?: (selection: { job_id: string; variant: number }) => void }) {
  const local = useLocalMode();
  const requestedJob = useRef(jobId);
  const liveVenture = useRef(ventureId);
  liveVenture.current = ventureId;
  const pathname = typeof window === "undefined" ? "" : window.location.pathname;
  const scope = { venture: ventureId, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch(), pathname };
  const ownerRef = useRef(scope);
  if (!sameGodModeScope(ownerRef.current, scope) || ownerRef.current.pathname !== pathname) ownerRef.current = scope;
  const renderOwner = ownerRef.current;
  const pending = useRef<AbortController | null>(null);
  const current = () => Boolean(pending.current && !pending.current.signal.aborted)
    && ownerRef.current === renderOwner && window.location.pathname === renderOwner.pathname
    && sameGodModeScope(renderOwner, { venture: liveVenture.current, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch() });
  const [tool, setTool] = useState<"claude" | "codex" | null>(null);
  const [orders, setOrders] = useState<PageBuildOrder[]>([]);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const buildFlight = useRef<object | null>(null);
  const supersededBuild = useRef<string | null>(null);
  const [address, setAddress] = useState<{ slug: string | null; current: string | null }>({ slug: null, current: null });
  const addressRef = useRef(address);
  const [changing, setChanging] = useState(false);
  const onAddress = (slug: string | null, saved: string | null) => { addressRef.current = { slug, current: saved }; setAddress({ slug, current: saved }); };
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  /** The server code behind `error` when fixing it happens on another page. */
  const [problem, setProblem] = useState<string | undefined>();
  // Only the idea gate needs the founder's live next step (the same receipt the Next step strip shows).
  const ideaStep = useNextStep(problem === "pagebuilder_confirmed_revision_required" ? ventureId : undefined);
  const [builder, setBuilder] = useState<"loading" | "enabled" | "disabled">("loading");
  const [profile, setProfile] = useState<PageBuilderProfile | null>(null);
  const [publication, setPublication] = useState<{ owner: typeof renderOwner; key: string; value: PagePublication } | null>(null);
  const publishKey = useRef<string | null>(null);
  const publishFlight = useRef<object | null>(null);
  const publishedRun = useRef<{ owner: typeof renderOwner; key: string; value: PagePublication } | null>(null);
  const retrySourceKey = useRef<string | null>(null);
  const publicationRetries = useRef(new Set<string>());
  const unconfirmedRetries = useRef(new Set<string>());
  const [previewDocument, setPreviewDocument] = useState<{ owner: typeof renderOwner; key: string; html: string } | null>(null);
  const previewRequest = useRef<{ owner: typeof renderOwner; key: string; response: ReturnType<typeof api> } | null>(null);
  const previewSource = previewSelection(profile), previewSourceKey = previewSource ? JSON.stringify(previewSource) : null;
  const preview = previewDocument?.owner === renderOwner && previewDocument.key === previewSourceKey ? previewDocument.html : "";
  const fullPreview = useRef<HTMLDialogElement | null>(null);
  const [referenceCapable, setReferenceCapable] = useState(false);
  const refreshGeneration = useRef(0);
  const detecting = useRef<Promise<void> | null>(null);
  const autoStarted = useRef(false);
  const buildRequested = useSearchParams()?.get("build") === "1";
  const lastBuildRequested = useRef(buildRequested);
  const [validatedIdea, setValidatedIdea] = useState<ValidatedIdea | null>(null);
  const [buildChoice, setBuildChoice] = useState<typeof renderOwner | null>(null);

  function reconcileRetry(saved: PagePublication, key: string) {
    if (publishFlight.current) return;
    if (saved.state !== "failed") {
      for (const retryKey of unconfirmedRetries.current) {
        const [sourceKey, runId] = JSON.parse(retryKey);
        if (sourceKey === key && runId === saved.run_id) unconfirmedRetries.current.delete(retryKey);
      }
      return;
    }
    if (saved.retryable !== true || typeof saved.job_attempt !== "number"
      || !Number.isSafeInteger(saved.job_attempt) || saved.job_attempt < 0) return;
    const retryKey = JSON.stringify([key, saved.run_id, saved.job_attempt]);
    if (unconfirmedRetries.current.delete(retryKey)) publicationRetries.current.delete(retryKey);
  }

  const refresh = useCallback(async () => {
    if (!current() || publishFlight.current || buildFlight.current) return;
    const generation = ++refreshGeneration.current;
    const currentRefresh = () => current() && !publishFlight.current && !buildFlight.current && generation === refreshGeneration.current;
    const signal = pending.current!.signal;
    setValidatedIdea(null);
    // Local tool detection runs `claude`/`codex` version and sign-in probes on the Mac (up to ~12 s).
    // It only matters for the on-device builder, so it never holds back the server's answer;
    // one detection at a time, and its result lands whenever it finishes.
    const local = detecting.current ||= Promise.all([
      !isLocalConsole() && isDesktopShell() ? desktopInvoke<PageBuildTool[]>("list_local_tools").catch(() => null) : Promise.resolve(null),
      !isLocalConsole() && isDesktopShell() ? desktopInvoke<{ pagebuilder_reference_v1?: boolean }>("device_work_order_capabilities").catch(() => null) : Promise.resolve(null),
    ]).then(([tools, capabilities]) => {
      if (!current() || signal.aborted) return;
      setTool(Array.isArray(tools) ? pickTool(tools) : null);
      setReferenceCapable(capabilities?.pagebuilder_reference_v1 === true);
    }).finally(() => { if (detecting.current === local) detecting.current = null; });
    const [r, p] = await Promise.all([
      isLocalConsole() ? Promise.resolve({ ok: true, data: { orders: [] } }) : api(`/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders`, { signal }),
      api(`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder/profile${requestedJob.current ? `?job_id=${encodeURIComponent(requestedJob.current)}` : ""}`, { signal }),
    ]);
    // Without the server builder the card is entirely about the local tool: wait for it to avoid a "Connect" flash.
    if ((p.data as { detail?: { code?: string } } | null)?.detail?.code === "pagebuilder_disabled") await local;
    if (!currentRefresh() || signal.aborted) return;
    const list = r.ok ? (r.data as { orders?: PageBuildOrder[] } | null)?.orders : null;
    setOrders(Array.isArray(list) ? list : []);
    const detail = (p.data as { detail?: { code?: string; message?: string } } | null)?.detail;
    const code = detail?.code;
    if (!p.ok) {
      setBuilder(code === "pagebuilder_disabled" ? "disabled" : "enabled");
      setProfile(null); setPreviewDocument(null); previewRequest.current = null;
      publishKey.current = null; setPublication(null); publishedRun.current = null;
      if (code !== "pagebuilder_disabled") {
        setError(pageBuildProblem(code, detail?.message));
        setProblem(code);
      }
      if (code === "pagebuilder_confirmed_revision_required") {
        const sessions = await api(`/api/ventures/${encodeURIComponent(ventureId)}/idea/sessions`, { signal });
        if (!currentRefresh() || signal.aborted || !sessions.ok) return;
        const latest = (sessions.data as { items?: { id: string; state: string }[] })?.items?.find(item => item.state !== "abandoned");
        if (!latest) return;
        const saved = await api(`/api/ventures/${encodeURIComponent(ventureId)}/idea/sessions/${encodeURIComponent(latest.id)}`, { signal });
        if (!currentRefresh() || signal.aborted || !saved.ok) return;
        const idea = saved.data as ValidatedIdea | null;
        if (idea && canConfirmValidatedIdea(idea)) setValidatedIdea(idea);
      }
      return;
    }
    setProblem(undefined);
    const saved = p.data as PageBuilderProfile;
    if (supersededBuild.current && (!saved.current || saved.current.job_id === supersededBuild.current)) return;
    supersededBuild.current = null;
    const selection = publishSelection(saved), key = selection ? JSON.stringify(selection) : null;
    if (key && retrySourceKey.current !== key) {
      publicationRetries.current.clear(); unconfirmedRetries.current.clear(); retrySourceKey.current = key;
    }
    if (publishKey.current !== key) {
      setPublication(null); publishedRun.current = null;
      if (publishFlight.current) { publishFlight.current = null; busyRef.current = false; setBusy(false); }
    }
    publishKey.current = key;
    const savedPublication = checkedPublication(saved.current?.publication);
    if (key && savedPublication) {
      reconcileRetry(savedPublication, key);
      publishedRun.current = { owner: renderOwner, key, value: savedPublication };
      setPublication({ owner: renderOwner, key, value: savedPublication });
    }
    setBuilder("enabled"); setProfile(saved); setError("");
    window.dispatchEvent(new Event(RUN_STATUS_REFRESH));
    const source = previewSelection(saved), sourceKey = source ? JSON.stringify(source) : null;
    if (!autoStarted.current && wantsBuild()) {
      autoStarted.current = true; dropBuildParam();
      // The founder's "Build my page" click elsewhere is the approval: start once (the server dedupes per idea version).
      if (!saved.current || saved.current.state === "failed" || saved.current.state === "ready") { void startBuild(saved.current?.job_id); return; }
    }
    if (source && sourceKey) {
      if (previewRequest.current?.owner !== renderOwner || previewRequest.current.key !== sourceKey) {
        previewRequest.current = { owner: renderOwner, key: sourceKey,
          response: api(`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder/jobs/${encodeURIComponent(source.job_id)}/preview-document?variant=${source.variant}`, { signal }) };
      }
      const doc = await previewRequest.current.response;
      if (!currentRefresh() || signal.aborted) return;
      const html = (doc.data as { html?: unknown } | null)?.html;
      if (doc.ok && typeof html === "string") setPreviewDocument({ owner: renderOwner, key: sourceKey, html });
      else {
        previewRequest.current = null;
        const detail = (doc.data as { detail?: { code?: string; message?: string } } | null)?.detail;
        setError(detail?.code === "pagebuilder_preview_too_large" && typeof detail.message === "string"
          ? detail.message : "The draft preview is unavailable. Refresh Website before reviewing the build.");
      }
    } else { setPreviewDocument(null); previewRequest.current = null; }
  }, [ventureId, renderOwner, jobId]);

  const view = pageBuildView(tool, jobId || builder === "enabled" && !profile ? [] : orders, profile?.task_id ?? undefined);
  const candidateJob = profile?.current;
  const failedAttempts = candidateJob?.consecutive_failures || 1;
  const previewCandidate = candidateJob?.server_publish_ready ? candidateJob.server_candidates?.[0]
    : candidateJob?.native_ready ? candidateJob.candidates[0] : null;
  const candidate = candidateJob?.native_ready
    ? candidateJob.candidates.find(item => item.variant === previewCandidate?.variant) : null;
  const selection = publishSelection(profile), selectionKey = selection ? JSON.stringify(selection) : null;
  useEffect(() => {
    if (selection && preview && current()) onSelection?.({ job_id: selection.job_id, variant: selection.variant });
  }, [onSelection, selectionKey, preview, renderOwner]);
  const savedPublication = publication?.owner === renderOwner && publication.key === selectionKey ? publication.value
    : candidateJob?.publication?.state === "failed" ? checkedPublication(candidateJob.publication) : null;
  // The final checks could not run (not a page problem): the same Publish click starts one fresh check.
  const publishAgain = Boolean(selectionKey) && savedPublication?.state === "failed" && savedPublication.retryable === true
    && savedPublication.next_action === "publish_again";
  // Nothing can resume this exact page: the next step is a new version, never a refresh-only dead end.
  const newVersion = savedPublication?.state === "failed" && (savedPublication.retryable !== true || !selectionKey);
  const serverAlternative = profile?.server_admission_enabled && profile.plan_id && profile.task_id && candidateJob && candidate && preview
    ? <ServerCandidateQa key={JSON.stringify([renderOwner, candidateJob.job_id, candidate.variant, profile.plan_id, profile.task_id])} ventureId={ventureId}
        jobId={candidateJob.job_id} variant={candidate.variant} planId={profile.plan_id} taskId={profile.task_id} /> : null;
  const polling = !candidateJob?.original_creator_required && (view.kind === "approve" || view.kind === "working"
    || Boolean(candidateJob && candidateJob.state !== "failed" && !candidateJob.native_ready
      && ["pending", "scheduled", "retry_wait", "leased", "running"].includes(candidateJob.queue_state || "")));
  useEffect(() => {
    const controller = new AbortController();
    pending.current = controller;
    const retire = () => {
      controller.abort(); setTool(null); setOrders([]); setBusy(false); setError(""); setProblem(undefined); setValidatedIdea(null);
      setBuilder("loading"); setProfile(null); setPreviewDocument(null); previewRequest.current = null;
      setPublication(null); publishKey.current = null;
      publishFlight.current = null; buildFlight.current = null; supersededBuild.current = null; busyRef.current = false;
      publishedRun.current = null;
      publicationRetries.current.clear(); unconfirmedRetries.current.clear(); retrySourceKey.current = null;
      setReferenceCapable(false);
    };
    setTool(null); setOrders([]); setBusy(false); setError(""); setProblem(undefined); setValidatedIdea(null);
    setBuilder("loading"); setProfile(null); setPreviewDocument(null); previewRequest.current = null;
    setPublication(null); publishKey.current = null;
    publishFlight.current = null; buildFlight.current = null; supersededBuild.current = null; busyRef.current = false;
    publishedRun.current = null;
    publicationRetries.current.clear(); unconfirmedRetries.current.clear(); retrySourceKey.current = null;
    setReferenceCapable(false); detecting.current = null;
    requestedJob.current = jobId;
    void refresh();
    window.addEventListener("supymem-session-changed", retire);
    return () => {
      controller.abort(); window.removeEventListener("supymem-session-changed", retire);
    };
  }, [refresh]);
  useEffect(() => {
    const newlyRequested = buildRequested && !lastBuildRequested.current;
    lastBuildRequested.current = buildRequested;
    if (!buildRequested) autoStarted.current = false;
    if (newlyRequested) void refresh();
  }, [buildRequested, refresh]);
  useEffect(() => {
    if (!polling) return;
    const t = window.setInterval(() => { void refresh(); }, 5000);
    return () => window.clearInterval(t);
  }, [polling, refresh]);
  useEffect(() => {
    if (!savedPublication || !["checking", "publishing"].includes(savedPublication.state)) return;
    let stopped = false, loading = false;
    const poll = async () => {
      if (stopped || loading || !current() || publishKey.current !== selectionKey) return;
      loading = true;
      try {
        const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder-admission/publish/${encodeURIComponent(savedPublication.run_id)}`,
          { signal: pending.current!.signal });
        if (stopped || !current() || publishKey.current !== selectionKey) return;
        const next = checkedPublication(result.data);
        if (result.ok && next?.run_id === savedPublication.run_id && selectionKey) {
          reconcileRetry(next, selectionKey);
          publishedRun.current = { owner: renderOwner, key: selectionKey, value: next };
          setPublication({ owner: renderOwner, key: selectionKey, value: next }); setError("");
        } else setError("Publication status is unavailable. Your saved request will keep its place; refresh to check it.");
      } catch {
        if (!stopped && current() && publishKey.current === selectionKey) setError("Publication status is unavailable. Refresh to check your saved request.");
      } finally { loading = false; }
    };
    const timer = window.setInterval(() => { void poll(); }, 2500);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [savedPublication?.run_id, savedPublication?.state, selectionKey, renderOwner, ventureId]);

  async function build(using: "claude" | "codex") {
    if (!current()) return;
    if (builder === "enabled" && (!referenceCapable || !candidateJob || !candidate || !preview)) return;
    const signal = pending.current!.signal;
    setBusy(true); setError("");
    const r = await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders/from-chat`, {
      signal, method: "POST", body: JSON.stringify({ instruction: buildInstruction(using, profile?.task_id ?? undefined),
        ...(builder === "enabled" && candidateJob && candidate
          ? { pagebuilder_candidate: { job_id: candidateJob.job_id, variant: candidate.variant } } : {}) }),
    });
    if (!current() || signal.aborted) return;
    setBusy(false);
    const path = r.ok ? (r.data as { review_path?: unknown } | null)?.review_path : activeMacTaskReviewPath(r);
    if (typeof path === "string" && path.startsWith(`/v/${ventureId}/device-work-orders/`)) {
      if (!routeInChat(path)) window.location.assign(path);
      return;
    }
    const code = (r.data as { detail?: { code?: string } } | null)?.detail?.code;
    setError(r.ok ? "The build was saved, but its link was unavailable. Open Mac tasks to find it."
      : code === "pagebuilder_runner_upgrade_required" ? "Update the Supymem Mac app before reviewing this draft build. No new coding task was prepared."
      : macTaskPrepareFailure(r));
  }

  /** One click: the server quotes and approves within its internal cap in the same request. */
  async function startBuild(newVersionOf?: string) {
    if (!current()) return;
    if (busyRef.current) { setError("A landing page request is already running. Wait for its result, or refresh the saved status before trying again."); setBusy(true); return; }
    const flight = {}; buildFlight.current = flight; refreshGeneration.current++;
    busyRef.current = true; setBusy(true); setError(""); setProblem(undefined);
    let timer: number | undefined, accepted = false, confirming = Boolean(validatedIdea);
    try {
      if (validatedIdea) {
        const confirmation = await api(`/api/ventures/${encodeURIComponent(ventureId)}/idea/sessions/${encodeURIComponent(validatedIdea.id)}/confirm`, {
          signal: pending.current!.signal, method: "POST",
          body: JSON.stringify({ revision_id: validatedIdea.current_revision?.id, start_research: false, accept_validation: true }),
        });
        if (!current() || buildFlight.current !== flight) return;
        if (!confirmation.ok) {
          setError(apiFailure(confirmation, "confirming your idea"));
          setProblem("pagebuilder_confirmed_revision_required"); setBuildChoice(null); setValidatedIdea(null);
          return;
        }
        confirming = false; setValidatedIdea(null); autoStarted.current = true; dropBuildParam();
      }
      const request = new AbortController(), signal = AbortSignal.any([pending.current!.signal, request.signal]);
      const r = await Promise.race([
        api(`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder/build`, { signal, method: "POST", body: JSON.stringify(newVersionOf ? {new_version_of: newVersionOf} : {}) }),
        new Promise<never>((_resolve, reject) => { timer = window.setTimeout(() => { request.abort(); reject(new Error("build_request_unconfirmed")); }, 15000); }),
      ]);
      if (!current() || buildFlight.current !== flight) return;
      const saved = r.data as PageBuilderProfile["current"] & { requeued?: boolean };
      if (!r.ok || saved?.state === "failed") {
        const detail = (r.data as { detail?: { code?: string; message?: string } } | null)?.detail;
        const code = detail?.code || saved?.error;
        setError(typeof saved?.error_reason === "string" && saved.error_reason.trim()
          ? saved.error_reason : pageBuildProblem(code, detail?.message)); setProblem(code);
      } else if (!saved?.job_id || !["queued", "running", "critiquing", "ready"].includes(saved.state)
          || newVersionOf && saved.job_id === newVersionOf && !(saved.requeued === true && saved.state !== "ready")) {
        setError("No fresh landing page build was confirmed. Refresh the saved status; if this idea has reached its version limit, update your idea or continue to Find customers.");
      } else {
        accepted = true;
        requestedJob.current = jobId ? saved.job_id : undefined;
        supersededBuild.current = saved.requeued === true && saved.job_id === newVersionOf ? null : newVersionOf || null;
        setProfile(previous => previous ? {...previous, current: {...saved, queue_state: saved.state === "queued" ? "scheduled" : saved.state,
          native_ready: false, candidates: [], server_publish_ready: false, server_candidates: []}} : previous);
        window.dispatchEvent(new Event(RUN_STATUS_REFRESH));
        window.dispatchEvent(new CustomEvent("supymem-venture-changed", {
          detail: { ventureId, actorId: renderOwner.owner, epoch: renderOwner.epoch },
        }));
      }
    } catch {
      if (current() && buildFlight.current === flight) {
        setError(confirming ? "Your idea confirmation could not be verified. Refresh the current idea before building; no build request was sent."
          : "The build request could not be confirmed. Refresh the saved status before trying again; a request already sent may still finish.");
        if (confirming) { setProblem("pagebuilder_confirmed_revision_required"); setBuildChoice(null); setValidatedIdea(null); }
      }
    } finally {
      window.clearTimeout(timer);
      if (buildFlight.current === flight) { buildFlight.current = null; busyRef.current = false; if (current()) setBusy(false); }
    }
    if (!accepted && current()) window.dispatchEvent(new CustomEvent("supymem-venture-changed", {
      detail: { ventureId, actorId: renderOwner.owner, epoch: renderOwner.epoch },
    }));
    if (accepted && current()) void refresh();
  }

  /** The preview's Publish click approves only this saved source and its final checks. */
  async function publish(move = false) {
    const chosen = addressRef.current;
    if (!current() || busyRef.current || !selection || !selectionKey || !preview || publishKey.current !== selectionKey || !chosen.slug
      || move && chosen.slug === chosen.current
      || !move && (!publishAgain && publishedRun.current?.owner === renderOwner && publishedRun.current.key === selectionKey
      || savedPublication && savedPublication.state !== "failed")) return;
    const signal = pending.current!.signal, base = `/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder-admission`;
    const flight = {}; publishFlight.current = flight; refreshGeneration.current++;
    busyRef.current = true; setBusy(true); setError("");
    try {
      if (chosen.slug !== chosen.current) {  // an edited address is saved before anything is checked or published
        const problem = await saveAddress(ventureId, chosen.slug);
        if (!current() || signal.aborted || publishKey.current !== selectionKey) return;
        if (problem) { setError(problem); return; }
      }
      const result = await api(`${base}/publish`, { signal, method: "POST", body: JSON.stringify({ ...selection, confirmed: true }) });
      if (!current() || signal.aborted || publishKey.current !== selectionKey) return;
      const saved = checkedPublication(result.data);
      if (result.ok && saved) {
        if (move) setChanging(false);
        publishedRun.current = { owner: renderOwner, key: selectionKey, value: saved };
        setPublication({ owner: renderOwner, key: selectionKey, value: saved });
      }
      else setError("Publishing is unconfirmed. Refresh to check the saved request before trying again.");
    } catch {
      if (current() && publishKey.current === selectionKey) setError("Publishing is unconfirmed. Refresh to check the saved request before trying again.");
    } finally {
      if (publishFlight.current === flight) {
        publishFlight.current = null; busyRef.current = false;
        if (current() && publishKey.current === selectionKey) setBusy(false);
      }
    }
  }

  async function retryPublication() {
    const observed = savedPublication, receipt = publishedRun.current, attempt = observed?.job_attempt;
    if (!current() || busyRef.current || !selectionKey || publishKey.current !== selectionKey || !preview
      || !observed || observed.state !== "failed" || observed.retryable !== true
      || typeof attempt !== "number" || !Number.isSafeInteger(attempt) || attempt < 0
      || receipt?.owner !== renderOwner || receipt.key !== selectionKey || receipt.value.run_id !== observed.run_id
      || receipt.value !== observed
      || receipt.value.state !== "failed" || receipt.value.retryable !== true || receipt.value.job_attempt !== attempt) return;
    const retryKey = JSON.stringify([selectionKey, observed.run_id, attempt]);
    if (publicationRetries.current.has(retryKey)) return;
    publicationRetries.current.add(retryKey);
    const signal = pending.current!.signal, flight = {};
    publishFlight.current = flight; refreshGeneration.current++; busyRef.current = true; setBusy(true); setError("");
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder-admission/publish/${encodeURIComponent(observed.run_id)}/retry`,
        {signal, method: "POST", body: JSON.stringify({expected_attempt: attempt, confirmed: true})});
      if (!current() || signal.aborted || publishKey.current !== selectionKey || publishFlight.current !== flight) return;
      const saved = checkedPublication(result.data);
      if (result.ok && saved?.run_id === observed.run_id) {
        publishedRun.current = {owner: renderOwner, key: selectionKey, value: saved};
        setPublication({owner: renderOwner, key: selectionKey, value: saved});
      } else {
        unconfirmedRetries.current.add(retryKey);
        setError("Publication retry is unconfirmed. Refresh to check the saved request before trying again.");
      }
    } catch {
      if (current() && publishKey.current === selectionKey && publishFlight.current === flight) {
        unconfirmedRetries.current.add(retryKey);
        setError("Publication retry is unconfirmed. Refresh to check the saved request before trying again.");
      }
    } finally {
      if (publishFlight.current === flight) {
        publishFlight.current = null; busyRef.current = false;
        if (current() && publishKey.current === selectionKey) setBusy(false);
      }
    }
  }

  async function retrySource() {
    if (!current() || busy || !candidateJob || candidateJob.state !== "ready" || candidateJob.queue_state !== "failed_dead"
      || !Number.isInteger(candidateJob.queue_attempt)) return;
    const signal = pending.current!.signal;
    setBusy(true); setError("");
    const r = await api(`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder/jobs/${encodeURIComponent(candidateJob.job_id)}/retry-source`, {
      signal, method: "POST", body: JSON.stringify({ expected_attempt: candidateJob.queue_attempt }),
    });
    if (!current() || signal.aborted) return;
    setBusy(false);
    if (!r.ok) { setError("The saved source could not be retried. Refresh and check the current task before continuing."); return; }
    await refresh();
  }

  async function retryCheckedPage(savedJobId: string) {
    if (!current() || busyRef.current) return;
    const signal = pending.current!.signal;
    busyRef.current = true; setBusy(true); setError("");
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder/jobs/${encodeURIComponent(savedJobId)}/retry-tool-result`, {
        signal, method: "POST",
      });
      if (!current() || signal.aborted) return;
      if (!result.ok) { setError(apiFailure(result, "saving your checked page")); return; }
      await refresh();
    } catch {
      if (current() && !signal.aborted) setError("Your app did not respond. Restart it, open Website, and choose Retry saving.");
    } finally {
      if (current()) { busyRef.current = false; setBusy(false); }
    }
  }

  const open = (label: string, path?: string) => path
    ? <ChatAnchor href={path} className="primary" data-usage-control="website_build_open">{label}</ChatAnchor> : null;
  const retry = tool
    ? <button type="button" disabled={busy} data-usage-control="website_build_retry" onClick={() => void build(tool)}>{busy ? "Starting…" : "Retry"}</button>
    : null;
  /** The primary action when the fix lives on another page: the idea gate reuses the founder's live next step. */
  const fix = (code?: string, message = error) => {
    if (code === "pagebuilder_confirmed_revision_required" && validatedIdea)
      return <>{buildChoice === renderOwner
        ? <PageChoices ventureId={ventureId} disabled={busy} onBuild={() => void startBuild()} />
        : <button type="button" className="primary" disabled={busy} data-usage-control="website_confirm_idea" onClick={() => { if (current() && !busyRef.current) setBuildChoice(renderOwner); }}>Build my landing page now</button>}
        <p>Accept your validated idea without starting research. Choose your page build next.</p>
        <ErrorNotice error={error === pageBuildProblem(code) ? "" : error} source="site.confirmIdea" /></>;
    const where = pageBuildFix(code, ventureId, message);
    if (!where) return null;
    if (code === "pagebuilder_confirmed_revision_required" && ideaStep?.key.startsWith("idea."))
      return <NextStepButton step={ideaStep} className="primary" />;
    return <ChatAnchor href={where.href} className="primary" data-usage-control={where.control}>{where.label}</ChatAnchor>;
  };

  const nativeOrder = (!local || !profile?.current) && (view.kind === "approve" || view.kind === "working" || view.kind === "stopped" || view.kind === "failed");
  if (builder === "enabled" && profile && !candidateJob?.original_creator_required && !nativeOrder) {
    const gtm = <ChatAnchor className={candidateJob?.state === "failed" && failedAttempts >= 2 ? "primary" : "secondary"} href={`/gtm?venture=${encodeURIComponent(ventureId)}`} data-usage-control="website_find_customers">Find customers</ChatAnchor>;
    if (!candidateJob) return <OneCard id="site-build" step="Landing page" title="Build your landing page" status={error ? "error" : "needs_you"}
      body={profile.renderer_ready === false
        ? "Landing page previews are warming up. We'll start your build as soon as they're ready. You can queue it now."
        : local ? "Your connected coding tool builds a page from your saved idea, audience, offer, proof and call to action. Connect and allow your tool in Settings > Keys and connections > Coding tools, then choose Build."
          : "Supymem builds a landing page from your idea and research. You see a private preview before anything is public."}
      actions={fix(problem) || <PageChoices ventureId={ventureId} disabled={busy} onBuild={() => void startBuild()} />}>
      <ErrorNotice error={error} source="site.buildPage" />
    </OneCard>;
    if (candidateJob.state === "failed") {
      const elsewhere = fix(problem || candidateJob.error, error || candidateJob.error_reason || "");
      const correctingHeading = local && candidateJob.error_reason?.includes("ask Charlie to build a new version with the corrected heading");
      const retrySaving = local && candidateJob.error === "pagebuilder_result_pending";
      return <OneCard id="site-build" step="Landing page" title="The landing page build stopped" status="error"
        body={error || candidateJob.error_reason || pageBuildProblem(candidateJob.error)}
        actions={<>{elsewhere}{correctingHeading ? <ChatAnchor className="primary" href={`/new?venture=${encodeURIComponent(ventureId)}`}>Open venture chat</ChatAnchor>
          : retrySaving ? <button type="button" className="primary" disabled={busy} onClick={() => void retryCheckedPage(candidateJob.job_id)}>{busy ? "Saving…" : "Retry saving"}</button>
          : <button type="button" className={failedAttempts >= 2 || elsewhere ? "secondary" : "primary"} disabled={busy} onClick={() => void startBuild(candidateJob.job_id)}
            data-usage-control="website_build_retry">{busy ? "Starting…" : "Retry"}</button>}
          <button type="button" disabled={busy} onClick={() => void refresh()}>Refresh status</button>{gtm}</>}>
        {failedAttempts >= 2 && !correctingHeading && !retrySaving && <p>Your idea is saved. Check the reason above, then retry your page.</p>}
      </OneCard>;
    }
    if (candidateJob.state !== "ready") {
      const stage = String(candidateJob.stage || "queued").replace(/[_:]+/g, " ");
      const pct = Math.round(Math.min(1, Math.max(0, Number(candidateJob.progress) || 0)) * 100);
      return <OneCard id="site-build" step="Landing page" title="Building your landing page" status="working"
        body={profile.renderer_ready === false
          ? "Landing page previews are warming up. We'll start your build as soon as they're ready. You can leave this page."
          : `${stage.charAt(0).toUpperCase()}${stage.slice(1)} · ${pct}%. This takes a few minutes; you can leave this page.`}
        actions={<>{candidateJob.watch_url && <ChatAnchor href={candidateJob.watch_url} className="primary">Watch your page build</ChatAnchor>}
          <button type="button" onClick={() => void refresh()}>Refresh status</button></>}>
        <progress value={pct} max={100} style={{ width: "100%" }} aria-label="Landing page build progress" />
      </OneCard>;
    }
    const canPublish = Boolean(profile.server_admission_enabled && selection);
    const native = referenceCapable && candidate && preview
      ? <button type="button" disabled={busy} onClick={() => void build(view.kind === "ready" ? view.tool : "codex")}
        data-usage-control="website_build_candidate">Refine with {view.kind === "ready" ? NAME[view.tool] : "Codex"}</button> : null;
    const live = savedPublication?.state === "live" && savedPublication.live_url;
    const publishing = savedPublication && ["checking", "publishing"].includes(savedPublication.state);
    const canRetryPublication = Boolean(selectionKey) && savedPublication?.state === "failed" && savedPublication.retryable === true
      && typeof savedPublication.job_attempt === "number" && Number.isSafeInteger(savedPublication.job_attempt)
      && savedPublication.job_attempt >= 0 && !publicationRetries.current.has(JSON.stringify([
        selectionKey, savedPublication.run_id, savedPublication.job_attempt]));
    return <OneCard id="site-build" step="Landing page" title={live ? "Your landing page is live" : publishing ? "Publishing your landing page" : "Your landing page is ready"}
      actionsFirst
      status={live ? "done" : publishing ? "working" : "needs_you"}
      body={local ? "Publish your page to collect signups." : live ? "Share your live landing page and find your first customers."
        : publishing ? savedPublication?.state === "checking" ? "The final checks are running. Your landing page will go live when they pass."
          : "The final checks passed. Supymem is publishing your landing page."
        : savedPublication?.state === "failed" ? savedPublication.error_reason || (canRetryPublication
          ? "Publishing stopped. Retry publication of this saved landing page."
          : publishAgain ? "Our final checks couldn't run. Your preview stays private. Try publishing again."
          : !newVersion ? "Publishing stopped. Your preview stays private; refresh to review the saved result."
          : savedPublication.qa_report_available === false ? "Our final checks couldn't complete. Your preview stays private; no landing page-content failure was confirmed. Build a new version to try again."
          : "This landing page didn't pass the final checks, so it stays private. Build a new version of the landing page, then publish that one.")
        : canPublish ? "This is a private preview. Publish checks this landing page and makes it public when the checks pass."
        : "This is a private preview. Publishing becomes available when this exact landing page is safely saved and checked."}
      actions={local ? <>
        <button type="button" className="primary" disabled={!preview} data-usage-control="website_open_full_preview" onClick={() => fullPreview.current?.showModal()}>Open preview</button>
        {preview && previewSource && <ChatAnchor href={`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder/jobs/${encodeURIComponent(previewSource.job_id)}/source?variant=${previewSource.variant}`}
          download data-usage-control="website_export_html">Export</ChatAnchor>}
        <details><summary>More</summary>
          <ChatAnchor href={onSelection ? "#local-publishing" : `/v/${encodeURIComponent(ventureId)}/site#local-publishing`}>Publish your page</ChatAnchor>
          {gtm}
          <button type="button" disabled={busy} onClick={() => void startBuild(candidateJob.job_id)} data-usage-control="website_build_new_version">Rebuild your landing page</button>
          {candidateJob.queue_state === "failed_dead" && Number.isInteger(candidateJob.queue_attempt)
            && <button type="button" disabled={busy} onClick={() => void retrySource()} data-usage-control="website_retry_source">Retry saving source</button>}
          {candidateJob.image_note && <p>{candidateJob.image_note}</p>}
          {candidateJob.improvement_available && <p>Your page is ready with a simpler version. You can improve it again.</p>}
        </details>
      </> : <>{preview && previewSource && <ChatAnchor href={`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder/jobs/${encodeURIComponent(previewSource.job_id)}/source?variant=${previewSource.variant}`}
        download data-usage-control="website_export_html">Export</ChatAnchor>}{candidateJob.queue_state === "failed_dead" && Number.isInteger(candidateJob.queue_attempt)
        ? <button type="button" disabled={busy} onClick={() => void retrySource()} data-usage-control="website_retry_source">Retry saving source</button> : null}
        {live ? <><ChatAnchor href={live} className="primary" target="_blank" rel="noopener noreferrer" data-usage-control="website_open_url">Visit landing page</ChatAnchor>
          <button type="button" data-usage-control="website_copy_url" onClick={async () => {
            try { await navigator.clipboard.writeText(live); if (current()) setCopied(true); }
            catch { if (current()) setError("Copy did not complete. Select the landing page address to copy it."); }
          }}>{copied ? "Copied" : "Copy link"}</button>
          <button type="button" disabled={busy} onClick={() => void startBuild(candidateJob.job_id)} data-usage-control="website_build_new_version">Build a new version</button></>
          : canRetryPublication ? <button type="button" className="primary" disabled={busy || !preview} onClick={() => void retryPublication()}
            data-usage-control="website_retry">{busy ? "Retrying…" : "Retry publication"}</button>
          : publishAgain ? <button type="button" className="primary" disabled={busy || !preview || !address.slug} onClick={() => void publish()}
            data-usage-control="website_publish_again">{busy ? "Starting…" : "Publish again"}</button>
          : newVersion ? <button type="button" className="primary" disabled={busy} onClick={() => void startBuild(candidateJob.job_id)}
              data-usage-control="website_build_new_version">Build a new version</button>
          : publishing || savedPublication?.state === "failed" ? <button type="button" disabled={busy} onClick={() => void refresh()}>Refresh status</button>
          : canPublish ? <button type="button" className="primary" disabled={busy || !preview || !address.slug} onClick={() => void publish()}
            data-usage-control="website_publish_review">{busy ? "Starting…" : "Publish"}</button> : null}{gtm}
        {newVersion ? null : native}
        {candidateJob.improvement_available && !publishing && !live && !newVersion
          ? <button type="button" disabled={busy} onClick={() => void startBuild(candidateJob.job_id)}
            data-usage-control="website_build_new_version">Improve again</button> : null}
        {jobId && !candidateJob.improvement_available && !publishing && !newVersion
          ? <button type="button" disabled={busy} onClick={() => void startBuild(candidateJob.job_id)}
            data-usage-control="website_build_new_version">Rebuild your landing page</button> : null}
        {preview ? <button type="button" data-usage-control="website_open_full_preview" onClick={() => fullPreview.current?.showModal()}>Open full preview</button> : null}</>}>
      {!local && candidateJob.image_note && <p>{candidateJob.image_note}</p>}
      {!local && candidateJob.improvement_available && <p>Your page is ready with a simpler version. You can improve it again.</p>}
      {savedPublication?.state === "failed" && savedPublication.qa_report_available && <p>
        <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/site?run=${encodeURIComponent(savedPublication.run_id)}#build-review`}>Review the failed checks</ChatAnchor>
      </p>}
      {!local && !live && canPublish && !publishing && !canRetryPublication && !newVersion
        && <SiteAddress ventureId={ventureId} target="pagebuilder" onReady={onAddress} disabled={busy} />}
      {!local && live && <div><p>{live}</p>
        {!changing && <button type="button" data-usage-control="website_address_change" disabled={busy} onClick={() => setChanging(true)}>Change address</button>}
        {changing && <div role="group" aria-label="Change landing page address">
          <SiteAddress ventureId={ventureId} target="pagebuilder" onReady={onAddress} disabled={busy} />
          <p className={addressStyles.note}>Your old link keeps working.</p>
          <button type="button" className="primary" data-usage-control="website_address_save"
            disabled={busy || !preview || !address.slug || address.slug === address.current} onClick={() => void publish(true)}>{busy ? "Starting…" : "Use this address"}</button>{" "}
          <button type="button" disabled={busy} onClick={() => setChanging(false)}>Cancel</button>
        </div>}
      </div>}
      {preview ? <>
        <div className={styles.previewWrap}><iframe title="Landing page preview" srcDoc={preview} sandbox="allow-popups allow-popups-to-escape-sandbox" className={styles.preview} /></div>
        <dialog ref={fullPreview} className={styles.fullPreview} aria-label="Full landing page preview">
          <form method="dialog"><button type="submit" data-usage-control="website_close_full_preview">Close preview</button></form>
          <iframe title="Full landing page preview" srcDoc={preview} sandbox="allow-popups allow-popups-to-escape-sandbox" className={styles.preview} />
        </dialog>
      </> : null}
      <ErrorNotice error={error} source="site.buildPage" />
    </OneCard>;
  }

  if (builder === "enabled" && candidateJob?.original_creator_required) return <OneCard id="site-build" step="Landing page"
    title="The draft creator needs to continue" status="needs_you"
    body="The member who created this draft must review and build it from their account."
    actions={<><ChatAnchor href={`/v/${ventureId}/plan`} className="primary">Open plan</ChatAnchor>
      <button type="button" onClick={() => void refresh()}>Refresh status</button></>} />;

  if (view.kind === "connect" && builder === "disabled") {
    return (
      <OneCard id="site-build" step="Landing page" title="Connect a coding tool" status="needs_you"
        body={local ? "Open Settings > Keys and connections > Coding tools, connect your coding tool, then retry your page."
          : "Claude Code or Codex builds your landing page on your Mac. Open the Supymem Mac app and connect one."}
        actions={<ChatAnchor href={local ? CODING_TOOLS_SETTINGS : `/connections?venture=${encodeURIComponent(ventureId)}`} className="primary" data-usage-control="website_connect_tool">Connect</ChatAnchor>}>{local ? null : serverAlternative}</OneCard>
    );
  }
  if (view.kind === "approve") {
    return <OneCard id="site-build" step="Landing page" title="Approve the build" status="needs_you"
      body="Check what your coding tool will do, then approve it." actions={open("Review and approve", view.order.review_path)} />;
  }
  if (view.kind === "working") {
    return <OneCard id="site-build" step="Landing page" title="Building your landing page" status="working"
      body="Your coding tool is building it on your Mac." actions={open("Watch progress", view.order.review_path)} />;
  }
  if (view.kind === "stopped") {
    return <OneCard id="site-build" step="Landing page" title="Review the build" status="needs_you"
      body="Open it to see the result and check the landing page." actions={<>{open("Open the build", view.order.review_path)}{retry}</>} />;
  }
  if (view.kind === "failed") {
    return <OneCard id="site-build" step="Landing page" title="The build stopped" status="error"
      body="Open its details before trying again." actions={<>{retry}{open("Details", view.order.review_path)}</>} />;
  }
  if (builder === "loading") return <OneCard id="site-build" step="Landing page" title="Getting your landing page" status="working"
    body="Checking the current landing task and draft." />;
  if (builder === "enabled" || view.kind === "connect") return <OneCard id="site-build" step="Landing page" title="Your landing page needs attention" status="needs_you"
    body={error || "The landing page builder could not load. Refresh and try again."}
    actions={fix(problem) || <button type="button" className="primary" onClick={() => void refresh()}>Refresh</button>} />;
  return (
    <OneCard id="site-build" step="Landing page" title="Build your landing page" status={error ? "error" : "needs_you"}
      body={`${NAME[view.tool]} builds a landing page just for this idea. A few minutes.`}
      actions={<button type="button" className="primary" disabled={busy} data-usage-control="website_build_page" onClick={() => void build(view.tool)}>{busy ? "Starting…" : error ? "Retry" : "Build"}</button>}>
      <ErrorNotice error={error} source="site.buildPage" />
    </OneCard>
  );
}
