"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { redirect, useParams, useRouter, useSearchParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { NextStepStrip } from "@/components/layout/NextStepStrip";
import { InlineSpinner } from "@/components/ui/InlineSpinner";
import { BuildEvidence } from "@/components/builds/BuildEvidence";
import type { ReviewBuild } from "@/lib/buildReview";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { isLocalConsole } from "@/lib/localMode";
import { codingToolLabel, safeLoopText } from "@/lib/loopProgress";
import { apiErrorCode, apiFailure } from "@/lib/apiFailure";
import { routeInChat, savedBuildActivity } from "@/lib/chatRouteAction";
import { USAGE_LIMIT_MESSAGE } from "@/lib/credits";
import styles from "../plan/plan.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type AppRow = { id: string; name: string; slug: string; state: string };
type Task = { id?: string; key: string; title: string; description: string; implementation: string; capability_id: string; state: string; acceptance: string[] };
type Plan = { id: string; state: string; stale_dependencies: boolean; newer_plan_id?: string | null;
  milestones: { title: string; included: boolean; tasks: Task[] }[] };
type IncludedOverhead = { policy_id: string; basis: string; founder_price_micro: number;
  aws_actual_cost: number | null; source_zip_max_bytes: number; report_evidence_max_bytes: number;
  log_max_bytes_per_task: number; retention_days: number; internet_egress: boolean };
type Quote = { quote_id: string; input_digest: string; expires_at: string; ready: boolean;
  reasons: (string | { code: string; message?: string })[]; model: string;
  available_micro: number | null; ceiling_micro: number | null;
  model_ceiling_micro: number | null; compute_ceiling_micro: number | null; artifact_ceiling_micro: number | null;
  lifecycle_version?: number; builder_compute_ceiling_micro?: number; qa_compute_ceiling_micro?: number;
  included_overhead?: IncludedOverhead | null;
  expectations?: { product_name: string; required_text: string[]; visuals?: { id: string; name: string }[];
    interactions: { label: string; behavior: string }[]; criteria: { expectation: string; verification: string }[]; limitations: string[] } | null };
type Build = ReviewBuild & { app_id: string; phase?: string;
  revision_instruction?: string | null;
  plan_task_id?: string | null;
  verified_execution?: boolean;
  created_at?: string | null; error_code?: string | null; cleanup_state?: string; artifact_id?: string | null;
  cleanup_by_purpose?: { builder: string; qa: string } | null; included_overhead?: IncludedOverhead | null;
  qa_state?: string; usage?: { reserved_micro: number | null; consumed_micro: number | null; unresolved_micro: number | null;
    charge_lines?: { purpose: string; consumed_micro: number; unresolved_micro: number }[] } };
type BuildEvent = { seq: number; kind: string; summary?: string; ts?: string; payload?: { message?: string; summary?: string } };
type ProductRuntime = { id: string; build_run_id: string; generation: number; state: string; phase: string;
  preview_url: string | null };
const human = (value: unknown) => typeof value === "string" ? value.replace(/[._]/g, " ") : "Not recorded";
const buildEventLabels: Record<string, string> = {
  artifact: "Generated source captured securely",
  qa_source_prepared: "Generated source prepared for independent QA",
  qa_report_stored: "Independent QA evidence saved",
  founder_review: "Founder review recorded",
  provider_cost_absorbed: "Your provider response could not be verified. Review the saved result, then retry",
};
const buildEventLabel = (event: BuildEvent) => {
  const provided = event.summary || event.payload?.summary || event.payload?.message;
  const providedKind = provided?.trim().toLowerCase().replace(/[ .]/g, "_");
  if (providedKind && buildEventLabels[providedKind]) return buildEventLabels[providedKind];
  if (provided && provided !== event.kind) return human(provided);
  return buildEventLabels[event.kind] || human(event.kind);
};
const buildErrorLabels: Record<string, string> = {
  build_model_transport_timeout: "Your build stopped because the connection timed out. Check its saved status before retrying.",
  qa_report_case_invalid: "Your product check could not be verified. Review the saved evidence before retrying.",
  build_model_output_incomplete: "Your build stopped because the response was incomplete. Check its saved status before retrying.",
  build_model_boundary_blocked: "Your build stopped before contacting the AI provider because its output did not pass the data-safety check. Review its saved result, then retry.",
  build_provider_outcome_uncertain: "Your build response could not be verified. Check its saved status before retrying.",
  build_model_outcome_uncertain: "Your build response could not be verified. Check its saved status before retrying.",
  artifact_unapproved_contact_detail: "Your build stopped because the page included contact details you did not provide. Review the saved plan before retrying.",
  artifact_product_javascript_invalid: "Your build stopped because the generated code did not pass its checks. Review the saved evidence before retrying.",
  artifact_product_javascript_check_unavailable: "Supymem could not run the required product JavaScript parser, so it accepted and deployed nothing. Review cleanup before retrying.",
  qa_checks_failed: "Independent QA found product issues. Review the saved evidence below before retrying this milestone.",
};
const buildErrorLabel = (build: Build) => {
  if (build.error_code === "build_provider_outcome_uncertain" && build.completion_status === "closed" && !build.usage?.unresolved_micro) {
    return "Your AI provider response could not be verified. Your build stopped safely. Retry this milestone when ready.";
  }
  return buildErrorLabels[build.error_code || ""] || human(build.error_code || "");
};
const running = (build: Build | null) => !!build && build.runtime_version === 1 &&
  build.completion_status !== "closed" &&
  (!["succeeded", "failed", "cancelled", "rejected"].includes(build.state) || build.cleanup_state === "pending");

function OverheadPolicy({ policy }: { policy: IncludedOverhead | null | undefined }) {
  if (!policy || policy.basis !== "platform_included") return null;
  const mib = (bytes: number) => Number.isSafeInteger(bytes) && bytes >= 0 ? `${bytes / 1048576} MiB` : "Not established";
  return <details><summary>Included storage and runtime limits</summary>
    <p>Source archive: {mib(policy.source_zip_max_bytes)} maximum. QA report and evidence: {mib(policy.report_evidence_max_bytes)} maximum.
      {" "}Runtime logs: {mib(policy.log_max_bytes_per_task)} maximum per task.</p>
    <p>Retention policy: {policy.retention_days} days. Internet access from the build and QA tasks: {policy.internet_egress ? "Enabled" : "Disabled"}.</p>
  </details>;
}

export default function BuildsPage() {
  return <Suspense fallback={<p>Loading build selection…</p>}><BuildRoute /></Suspense>;
}

function BuildRoute() {
  const ventureId = String(useParams().ventureId || "");
  const search = useSearchParams();
  // A result link can change only the query while this page remains mounted.
  // Remount the scoped workspace so old selection/approvals cannot win over it.
  const selection = JSON.stringify([ventureId, search.get("app"), search.get("run"), search.get("task")]);
  if (isLocalConsole()) {
    const activity = savedBuildActivity(ventureId, search.get("run"));
    if (activity) redirect(activity);
    return <LocalBuildWorkspace key={ventureId} ventureId={ventureId} />;
  }
  return <BuildWorkspace key={selection} ventureId={ventureId} />;
}

function buildReceiptMatches(data: unknown, id: string, ventureId: string): boolean {
  const order = data as { id?: unknown; venture_id?: unknown } | null;
  return order?.id === id && order.venture_id === ventureId;
}

function LocalBuildWorkspace({ ventureId }: { ventureId: string }) {
  const owner = useRef(ventureId), dispatch = useRef(false);
  const attempt = useRef<{ id: string; epoch: number; subject: string } | null>(null);
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false), [held, setHeld] = useState(false);
  const [orders, setOrders] = useState<Array<{id: string; state: string; title?: string; manifest?: {
    title: string; coding_loop_verification?: {provider: string; phase?: string};
    coding_verification?: {provider: string} } }> | null>(null);
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [retryReady, setRetryReady] = useState(false);
  const [recovered, setRecovered] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    owner.current = ventureId;
    return () => { owner.current = ""; };
  }, [ventureId]);
  function currentOwner(scope: { epoch: number; subject: string }) {
    return owner.current === ventureId && scope.subject === verifiedSessionSubject() && scope.epoch === verifiedSessionEpoch();
  }
  async function reconcileBuild(current: () => boolean) {
    const saved = attempt.current;
    if (!saved || dispatch.current || !currentOwner(saved)) return;
    const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders/${saved.id}`);
    if (!current() || !currentOwner(saved) || attempt.current !== saved || dispatch.current) return;
    if (result.ok && buildReceiptMatches(result.data, saved.id, ventureId)) {
      attempt.current = null; setRecovered(saved.id); setHeld(false); setRetryReady(false); setInstruction(""); setError("");
    } else if (result.status === 404) {
      setRetryReady(true); setError("");
    }
  }
  async function requestBuild() {
    if (dispatch.current || owner.current !== ventureId || !instruction.trim() || (attempt.current && !retryReady)) return;
    const epoch = verifiedSessionEpoch(), subject = verifiedSessionSubject();
    if (!subject) {setError("Restart your local app, then open your venture and retry your build."); return;}
    dispatch.current = true; setBusy(true); setError(""); setRecovered("");
    const retry = attempt.current;
    const current = () => currentOwner({ epoch, subject });
    const uncertain = "Your build request could not be confirmed. Choose Refresh builds to check it before retrying.";
    try {
      await ensureSession();
      if (!current() || (retry && !currentOwner(retry))) return;
      const id = retry?.id || `dwo_${crypto.randomUUID().replaceAll("-", "")}`;
      attempt.current = { id, epoch, subject }; setHeld(true); setRetryReady(false);
      const result = await api("/api/coding/dispatch", {method: "POST", body: JSON.stringify({venture_id: ventureId, prompt: instruction.trim(), kind: "product", order_id: id})});
      if (!current()) return;
      if (!result.ok) {
        const refused = [400, 401, 402, 403, 409, 422, 423, 429].includes(result.status);
        if (!retry && refused) { attempt.current = null; setHeld(false); }
        const setup = (result.data as {detail?: {message?: unknown}} | null)?.detail?.message;
        setError(!refused ? uncertain : apiErrorCode(result) === "build_tool_needs_setup"
          ? (typeof setup === "string" && setup) || "Connect your coding tool in Settings > Keys and connections > Coding tools, then retry your build."
          : apiFailure(result, "starting your coding tool"));
        return;
      }
      if (!buildReceiptMatches(result.data, id, ventureId)) {
        setError(uncertain); return;
      }
      attempt.current = null; setHeld(false); setInstruction("");
      const path = `/v/${encodeURIComponent(ventureId)}/device-work-orders/${id}`;
      if (!routeInChat(path)) window.location.assign(path);
    } catch {
      if (current()) setError(attempt.current ? uncertain : "Restart your local app, then open your venture and retry your build.");
    } finally { if (current()) { dispatch.current = false; setBusy(false); } }
  }
  useEffect(() => {
    let cancelled = false, loading = false;
    const load = async (checkAttempt = false) => {
      if (loading) return;
      loading = true;
      try {
        await ensureSession();
        const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
        const current = () => !cancelled && !!subject && currentOwner({ epoch, subject });
        if (!current()) return;
        if (checkAttempt) await reconcileBuild(current);
        const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders`);
        if (!current()) return;
        const rows = (result.data as {orders?: unknown[]} | null)?.orders;
        if (!result.ok || !Array.isArray(rows)) {
          setHistoryError("Your build history could not be read. Open this venture in your desktop app, then choose Refresh builds.");
          return;
        }
        setOrders(rows.filter((row): row is NonNullable<typeof orders>[number] => {
          const saved = row as {id?: string; venture_id?: string; manifest?: {coding_loop_verification?: unknown; coding_verification?: unknown}};
          return typeof saved?.id === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(saved.id)
            && (saved.venture_id == null || saved.venture_id === ventureId)
            && (!saved.manifest || !!(saved.manifest.coding_loop_verification || saved.manifest.coding_verification));
        }));
        setHistoryError("");
      } catch {
        if (!cancelled) setHistoryError("Your build history could not be read. Open this venture in your desktop app, then choose Refresh builds.");
      } finally { loading = false; }
    };
    void load(refresh > 0);
    const timer = window.setInterval(() => void load(), 2000);
    const changed = () => {attempt.current = null; dispatch.current = false; setHeld(false); setRetryReady(false); setRecovered(""); setError(""); setBusy(false); setInstruction(""); setOrders(null); setRefresh(value => value + 1);};
    window.addEventListener("supymem-session-changed", changed);
    return () => { cancelled = true; window.clearInterval(timer); window.removeEventListener("supymem-session-changed", changed); };
  }, [ventureId, refresh]);
  return <AppShell title="Your builds" ventureId={ventureId} subtitle="Watch your coding tool and open your saved work">
    <section style={{ background: "#fff", color: "#17202a", padding: 24, overflowWrap: "anywhere" }} aria-label="Your local builds">
      <p>Your coding tool runs on this laptop with your own login. Ask Charlie in your venture chat to build your app with the tool you connected.</p>
      <Link href="/settings/local#coding-tools">Connect your coding tool</Link>{" "}
      <Link href={`/v/${encodeURIComponent(ventureId)}`}>Open your venture chat</Link>{" "}
      <button type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>Refresh builds</button>
      <label>Your app build request<textarea maxLength={12000} value={instruction} disabled={busy || held} onChange={event => setInstruction(event.target.value)} /></label>
      <button type="button" disabled={busy || (held && !retryReady) || !instruction.trim()} onClick={() => void requestBuild()}>{retryReady ? "Retry your build request" : "Build with your coding tool"}</button>
      {held && !busy && <p role="status">{retryReady ? "No saved build was found for this request. You can retry the same request without starting a duplicate." : "Choose Refresh builds to check whether your request was saved."}</p>}
      {recovered && <p role="status">Your build was saved. <Link href={`/v/${encodeURIComponent(ventureId)}/device-work-orders/${recovered}`}>Open your build</Link></p>}
      <ErrorNotice error={error} source="builds.local_request" />
      <ErrorNotice error={historyError} source="builds.local_history" />
      {orders === null ? <p role="status">Loading your builds. If this takes longer, choose Refresh builds.</p>
        : orders.length === 0 ? <p>No builds are saved yet. Connect your coding tool, then ask Charlie in your venture chat to build your app.</p>
        : <ul>{orders.map(order => <li key={order.id}>
          <h2>{safeLoopText(order.title || order.manifest?.title || "Your saved build")}</h2>
          <p>{codingToolLabel((order.manifest?.coding_loop_verification || order.manifest?.coding_verification)?.provider || "")}: {
            order.state === "failed" ? "Build failed. Open your build to see what happened and how to retry."
              : order.state === "stopped" ? "Stopped; open your result to check it" : order.state === "cancelled" ? "Cancelled"
              : order.state === "claimed" ? "Build in progress" : order.state === "waiting_for_device" ? "Waiting for your laptop"
              : order.state === "expired" ? "Expired; request a new build when you are ready"
              : ["approved", "pending"].includes(order.state) ? "Ready to start" : "Open your build to check its status"}</p>
          <Link href={`/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(order.id)}`}>Watch build and open result</Link>
        </li>)}</ul>}
    </section>
  </AppShell>;
}

function BuildWorkspace({ ventureId }: { ventureId: string }) {
  const router = useRouter();
  const scope = useRef(ventureId); scope.current = ventureId;
  const mounted = useRef(false);
  const generation = useRef(0), listGeneration = useRef(0), detailGeneration = useRef(0), mutation = useRef(false);
  const [apps, setApps] = useState<AppRow[] | null>(null);
  const [appId, setAppId] = useState("");
  const currentApp = useRef(appId); currentApp.current = appId;
  const [plan, setPlan] = useState<Plan | null>(null);
  const [taskId, setTaskId] = useState("");
  const [builds, setBuilds] = useState<Build[] | null>(null);
  const [build, setBuild] = useState<Build | null>(null);
  const [events, setEvents] = useState<BuildEvent[]>([]);
  const [runtime, setRuntime] = useState<ProductRuntime | null>(null);
  const [runtimeError, setRuntimeError] = useState("");
  const [eventAfter, setEventAfter] = useState(0);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [approved, setApproved] = useState(false);
  const [clock, setClock] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [syncError, setSyncError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const draftRequest = useRef<{ body: string; key: string } | null>(null);
  const active = () => mounted.current && scope.current === ventureId;
  const tasks = (plan?.milestones || []).filter(milestone => milestone.included)
    .flatMap(milestone => milestone.tasks.map(task => ({ ...task, milestone: milestone.title })));
  const task = tasks.find(item => item.id === taskId);
  const approvedPlan = !!plan && ["approved", "executing"].includes(plan.state) && !plan.stale_dependencies && !plan.newer_plan_id;
  const closedFailedBinding = !!task?.id && build?.plan_task_id === task.id && build.completion_status === "closed" &&
    ["failed", "cancelled", "rejected"].includes(build.state);
  const selectedRun = !!task?.id && build?.plan_task_id === task.id;
  // Result links end in #build-review; the section only exists after the run loads.
  useEffect(() => {
    const scroll = () => {
      if (window.location.hash !== "#build-review") return;
      const go = () => document.getElementById("build-review")?.scrollIntoView({ block: "start" });
      go(); requestAnimationFrame(go); // the section can mount a frame after the run loads
    };
    if (build?.id) scroll();
    window.addEventListener("hashchange", scroll);
    return () => window.removeEventListener("hashchange", scroll);
  }, [build?.id]);
  const eligible = !!task?.id && task.capability_id === "build.run" && task.implementation !== "founder_action" &&
    (task.state === "pending" || task.state === "failed" || (closedFailedBinding && ["ready", "running", "qa"].includes(task.state)));
  const activeRun = builds?.some(item => running(item)) || running(build);
  const acceptedBuild = builds?.some(item => item.review?.status === "accepted") || build?.review?.status === "accepted";
  const quoteExpired = !quote || !Number.isFinite(Date.parse(quote.expires_at)) || Date.parse(quote.expires_at) <= clock;
  const budgetReady = !!quote && quote.ready && !quoteExpired && Number.isSafeInteger(quote.ceiling_micro) &&
    quote.ceiling_micro! > 0 && Number.isSafeInteger(quote.available_micro) && quote.available_micro! >= quote.ceiling_micro!;

  function selectResult(selectedApp: string, selectedRun?: string) {
    const query = new URLSearchParams(window.location.search);
    query.set("app", selectedApp);
    if (selectedRun) query.set("run", selectedRun); else query.delete("run");
    router.replace(`/v/${encodeURIComponent(ventureId)}/builds?${query.toString()}`, { scroll: false });
  }

  async function load() {
    const token = ++generation.current;
    const current = () => active() && generation.current === token;
    await ensureSession(); if (!current()) return;
    const [listed, plans] = await Promise.all([api(`/api/ventures/${ventureId}/apps`), api(`/api/ventures/${ventureId}/plans`)]);
    if (!current()) return;
    setError(""); setQuote(null); setApproved(false);
    const rows = (listed.data as { items?: AppRow[] })?.items;
    if (!listed.ok || !Array.isArray(rows)) { setApps(null); setError(apiFailure(listed, "loading your products")); return; }
    setApps(rows);
    const requestedApp = new URL(window.location.href).searchParams.get("app");
    if (requestedApp && !rows.some(row => row.id === requestedApp)) {
      setAppId(""); setError("The linked product is not available in this venture. Choose an available product below.");
    } else {
      setAppId(previous => rows.some(row => row.id === previous) ? previous
        : rows.find(row => row.id === requestedApp)?.id || rows[0]?.id || "");
    }
    const history = (plans.data as { items?: { id: string }[] })?.items;
    if (!plans.ok || !Array.isArray(history)) { setPlan(null); setError(apiFailure(plans, "loading the execution plan")); return; }
    if (!history[0]) { setPlan(null); return; }
    const result = await api(`/api/ventures/${ventureId}/plans/${history[0].id}`);
    if (!current()) return;
    if (result.ok && Array.isArray((result.data as Plan)?.milestones)) {
      const nextPlan = result.data as Plan;
      setPlan(nextPlan);
      const requested = new URL(window.location.href).searchParams.get("task") || "";
      const exists = nextPlan.milestones.some(milestone =>
        milestone.included && milestone.tasks.some(task => task.id === requested),
      );
      if (exists) setTaskId(requested);
    }
    else { setPlan(null); setError(apiFailure(result, "opening the execution plan")); }
  }
  async function loadBuilds(selectedApp: string) {
    const token = ++listGeneration.current;
    const [result, runtimeResult] = await Promise.all([
      api(`/api/apps/${selectedApp}/builds`), api(`/api/apps/${selectedApp}/runtime`),
    ]);
    if (!active() || currentApp.current !== selectedApp || listGeneration.current !== token) return;
    setRuntime(runtimeResult.ok ? ((runtimeResult.data as { runtime?: ProductRuntime | null })?.runtime || null) : null);
    setRuntimeError(runtimeResult.ok ? "" : apiFailure(runtimeResult, "reading application runtime status"));
    const rows = (result.data as { items?: Build[] })?.items;
    if (!result.ok || !Array.isArray(rows)) { setBuilds(null); setSyncError(apiFailure(result, "synchronizing builds")); return; }
    setBuilds(rows); setSyncError("");
    const query = new URL(window.location.href).searchParams;
    const requestedRun = query.get("app") === selectedApp ? query.get("run") : null;
    if (requestedRun && !rows.some(row => row.id === requestedRun)) {
      setBuild(null); setEvents([]);
      setSyncError("The linked build is not available for this product. Choose an available saved run below.");
      return;
    }
    const selected = rows.find(row => row.id === build?.id) || rows.find(row => row.id === requestedRun) || rows[0];
    if (selected) await loadBuild(selected.id, selectedApp, selected.id === build?.id ? eventAfter : 0);
    else { setBuild(null); setEvents([]); }
  }
  async function loadBuild(id: string, selectedApp: string, after = eventAfter): Promise<boolean> {
    const token = ++detailGeneration.current;
    const [detail, log] = await Promise.all([api(`/api/builds/${id}`), api(`/api/builds/${id}/events?after=${after}&limit=200`)]);
    if (!active() || currentApp.current !== selectedApp || detailGeneration.current !== token) return false;
    const receipt = detail.data as Build;
    if (!detail.ok || receipt?.id !== id || receipt.app_id !== selectedApp) {
      setSyncError(detail.ok ? "This build did not match the selected product. Refresh its status." : apiFailure(detail, "reading this build")); return false;
    }
    setBuild(receipt);
    // Detail polling is authoritative for the selected run. Keep the list row
    // synchronized as well so a queued snapshot cannot continue to count as an
    // active build after the run has closed, blocking a safe reviewed retry.
    setBuilds(previous => previous?.map(item => item.id === receipt.id ? receipt : item) || previous);
    setEventAfter(after);
    if (JSON.stringify(receipt.usage) !== JSON.stringify(build?.usage)) setRefresh(value => value + 1);
    const rows = (log.data as { items?: BuildEvent[] })?.items;
    if (!log.ok || !Array.isArray(rows)) { setEvents([]); setSyncError(apiFailure(log, "reading build activity")); return false; }
    setEvents(rows.slice(0, 200)); setSyncError(""); return true;
  }
  useEffect(() => {
    mounted.current = true; scope.current = ventureId;
    setApps(null); setAppId(""); setPlan(null); setBuilds(null); setBuild(null); setRuntime(null); setRuntimeError(""); setEvents([]); setEventAfter(0); setQuote(null);
    setTaskId(""); setApproved(false); setError(""); setStatus(""); setSyncError(""); setBusy(false); setName(""); setSlug("");
    mutation.current = false; draftRequest.current = null; void load();
    return () => { mounted.current = false; scope.current = ""; generation.current++; listGeneration.current++; detailGeneration.current++; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ventureId]);
  useEffect(() => {
    setBuilds(null); setBuild(null); setRuntime(null); setRuntimeError(""); setEvents([]); setEventAfter(0); setQuote(null); setApproved(false); setSyncError("");
    if (appId) void loadBuilds(appId);
    return () => { listGeneration.current++; detailGeneration.current++; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId, ventureId]);
  useEffect(() => {
    if (!quote) return;
    setClock(Date.now()); const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [quote]);
  useEffect(() => {
    if (!running(build) || syncError) return;
    let cancelled = false, timer: ReturnType<typeof setTimeout>;
    async function tick() {
      if (cancelled) return;
      if (document.visibilityState === "visible") {
        const ok = await loadBuild(build!.id, appId);
        if (!ok || cancelled) return;
      }
      timer = setTimeout(() => void tick(), 5000);
    }
    timer = setTimeout(() => void tick(), 5000);
    return () => { cancelled = true; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [build?.id, build?.state, build?.cleanup_state, appId, ventureId, syncError, eventAfter]);
  useEffect(() => {
    if (!appId || !runtime || !["pending", "provisioning", "reconciling", "staged"].includes(runtime.state)) return;
    const timer = setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void api(`/api/apps/${appId}/runtime`).then(result => {
        if (!active() || currentApp.current !== appId) return;
        if (result.ok) {
          setRuntime((result.data as { runtime?: ProductRuntime | null })?.runtime || null);
          setRuntimeError("");
        } else setRuntimeError(apiFailure(result, "reading application runtime status"));
      });
    }, 10000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId, runtime?.id, runtime?.state]);

  async function action(work: () => Promise<void>) {
    if (mutation.current) return;
    mutation.current = true; setBusy(true); setStatus("");
    try { await work(); }
    finally { if (active()) { mutation.current = false; setBusy(false); } }
  }
  async function createProduct() {
    await action(async () => {
      const body = JSON.stringify({ name: name.trim(), slug: slug.trim(), kind: "static_site", build_mode: "supymem", deploy_target: "supymem" });
      if (draftRequest.current?.body !== body) draftRequest.current = { body, key: crypto.randomUUID() };
      const result = await api(`/api/ventures/${ventureId}/apps`, { method: "POST", body, headers: { "Idempotency-Key": draftRequest.current.key } });
      if (!active()) return;
      if (!result.ok) { setStatus(apiFailure(result, "creating the product draft")); return; }
      const created = result.data as AppRow;
      if (!created?.id) { setStatus("The response did not identify the product. Refresh before trying again."); return; }
      setName(""); setSlug(""); draftRequest.current = null;
      setStatus("Product draft saved. Choose an approved build milestone to review its budget."); selectResult(created.id);
    });
  }
  async function estimate() {
    if (!appId || !approvedPlan || !eligible) return;
    const selectedApp = appId;
    let prepared: Quote | null = null;
    await action(async () => {
      setQuote(null); setApproved(false);
      const result = await api(`/api/apps/${selectedApp}/builds/estimate`, { method: "POST",
        body: JSON.stringify({ plan_id: plan!.id, plan_task_id: taskId, requested_mode: "supymem" }) });
      if (!active() || currentApp.current !== selectedApp) return;
      if (!result.ok) { setStatus(apiFailure(result, "estimating this build")); return; }
      const next = result.data as Quote;
      if (!next?.quote_id || !Array.isArray(next.reasons)) { setStatus("The build estimate could not be read. No execution started."); return; }
      setClock(Date.now()); setQuote(next);
      if (next.reasons.some(reason => (typeof reason === "string" ? reason : reason.code)?.endsWith("insufficient_credits"))) {
        setStatus(USAGE_LIMIT_MESSAGE); return;
      }
      prepared = next;
    });
    if (prepared) await start(prepared);
  }
  async function start(reviewed = quote) {
    if (!active() || currentApp.current !== appId || !appId || !reviewed || !reviewed.ready || !approvedPlan || !eligible || activeRun
        || !Number.isSafeInteger(reviewed.ceiling_micro) || reviewed.ceiling_micro! <= 0
        || !Number.isSafeInteger(reviewed.available_micro) || reviewed.available_micro! < reviewed.ceiling_micro!
        || !Number.isFinite(Date.parse(reviewed.expires_at)) || Date.parse(reviewed.expires_at) <= Date.now()) return;
    const selectedApp = appId;
    await action(async () => {
      const result = await api(`/api/apps/${selectedApp}/builds`, { method: "POST", headers: { "Idempotency-Key": `build-${reviewed.quote_id}` },
        body: JSON.stringify({ quote_id: reviewed.quote_id, input_digest: reviewed.input_digest, approve_budget_micro: reviewed.ceiling_micro }) });
      if (!active() || currentApp.current !== selectedApp) return;
      if (!result.ok) { setStatus(apiFailure(result, "starting the reviewed build")); await loadBuilds(selectedApp); return; }
      const id = (result.data as { build_run_id?: string })?.build_run_id;
      setQuote(null); setApproved(false);
      if (!id) { setStatus("Refresh the build list to confirm this request before starting another run."); await loadBuilds(selectedApp); return; }
      setStatus("Build request saved. Hosted work continues when this Mac closes."); selectResult(selectedApp, id);
    });
  }
  async function cancel() {
    if (!build || !running(build)) return;
    const id = build.id, selectedApp = appId;
    await action(async () => {
      const result = await api(`/api/builds/${id}/cancel`, { method: "POST", body: "{}" });
      if (!active() || currentApp.current !== selectedApp) return;
      setStatus(result.ok ? "Cancellation requested. Check the saved run until work stops and its usage is reconciled." : apiFailure(result, "cancelling this build"));
      await loadBuild(id, selectedApp);
    });
  }

  return <AppShell title="Builds" ventureId={ventureId} subtitle="Preview · review saved product build work">
    <div className={styles.page}>
      <h1>Product builds · Preview</h1><p className={styles.lede}>Review saved attempts and their evidence. New builds require an approved plan and budget; a completed build or landing page is not proof that a hosted product works for users.</p>
      <div className={styles.actions}><Link href={`/v/${ventureId}/plan`}>Plan and resources</Link>
        <button type="button" disabled={busy} onClick={() => void action(async () => { await load(); if (appId) await loadBuilds(appId); })}>Refresh saved state</button></div>
      <ErrorNotice error={error} source="builds.error" />{syncError && <ErrorNotice error={syncError} source="builds.syncError">{syncError} Automatic checking is paused.</ErrorNotice>}
      {status && <p role="status">{status}</p>}
      {apps === null && !error && <p role="status"><InlineSpinner />Loading your products…</p>}
      {busy && <p role="status"><InlineSpinner />Updating the build workspace…</p>}
      {apps && <section className={styles.card}><h2>Your product</h2>
        {apps.length > 0 ? <label className={styles.field}>Product<select value={appId} disabled={busy} onChange={event => selectResult(event.target.value)}>
          <option value="" disabled>Choose a product</option>
          {apps.map(item => <option key={item.id} value={item.id}>{item.name} · {human(item.state)}</option>)}</select></label> : <p>Create a product draft for this company.</p>}
        <details open={!apps.length}><summary>Create a product draft</summary>
          <label className={styles.field}>Product name<input value={name} maxLength={120} disabled={busy} onChange={event => setName(event.target.value)} /></label>
          <label className={styles.field}>Product address name<input value={slug} maxLength={63} disabled={busy} placeholder="studio-booking" onChange={event => setSlug(event.target.value.toLowerCase())} /></label>
          <p>Use 3 to 63 lowercase letters, numbers or hyphens. A draft saves the product identity; hosting starts with a separate deployment.</p>
          <button type="button" disabled={busy || !name.trim() || !/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/.test(slug.trim())} onClick={() => void createProduct()}>Save product draft</button>
        </details>
      </section>}
      <section className={styles.card}><h2>Choose the next milestone</h2>
        {!approvedPlan && <p>{acceptedBuild
          ? <>This milestone is complete. Review and approve the latest execution plan before starting another build. <Link href={`/v/${ventureId}/plan`}>Open latest plan</Link></>
          : <>Review and approve the current execution plan before starting a build. <Link href={`/v/${ventureId}/plan`}>Open plan</Link></>}</p>}
        <label className={styles.field}>Plan task<select value={taskId} disabled={busy || !approvedPlan || !appId} onChange={event => { setTaskId(event.target.value); setQuote(null); setApproved(false); }}>
          <option value="">Choose a task</option>{tasks.filter(item => item.id).map(item => <option key={item.id} value={item.id}>{item.milestone} · {item.title}{item.implementation === "founder_action" ? " · Founder action" : ""}{item.state !== "pending" ? ` · ${human(item.state)}` : ""}</option>)}</select></label>
        {task && <><p>{task.description}</p><ul>{task.acceptance.map((item, index) => <li key={index}>{item}</li>)}</ul>
          {!eligible && selectedRun && running(build) && <p>This approved milestone is running in the hosted build and QA workflow. Follow its saved activity below.</p>}
          {!eligible && selectedRun && !running(build) && <p>This milestone has a saved build result. Review its evidence and QA status below.</p>}
          {!eligible && !selectedRun && <p>This task requires its planned workflow or a founder action. Review its instructions in the execution plan.</p>}
          {closedFailedBinding && <p>The previous run is closed. Review a new estimate to retry this milestone safely.</p>}</>}
        <button type="button" data-usage-control="build_estimate" disabled={busy || !appId || !approvedPlan || !eligible || activeRun} onClick={() => void estimate()}>Build this milestone</button>
      </section>
      {quote && <section className={styles.card}><h2>Build review</h2><p>{quote.model}</p>
        <OverheadPolicy policy={quote.included_overhead} />
        {quote.expectations && <><h3>What this milestone will deliver</h3><p>{quote.expectations.product_name}</p>
          {quote.expectations.required_text.length > 0 && <><h4>Proposed offer copy</h4><ul>{quote.expectations.required_text.map((text, index) => <li key={index}>{text}</li>)}</ul></>}
          {!!quote.expectations.visuals?.length && <><h4>Images to verify on desktop and mobile</h4><ul>{quote.expectations.visuals.map((visual) => <li key={visual.id}>{visual.name}, at least 160 × 90 pixels</li>)}</ul></>}
          <ul>{quote.expectations.criteria.map((item, index) => <li key={index}><strong>{item.expectation}</strong>, {item.verification}</li>)}</ul>
          {quote.expectations.interactions.length > 0 && <><h4>Interactions to test</h4><ul>{quote.expectations.interactions.map((item, index) => <li key={index}>{item.label}: {item.behavior}</li>)}</ul></>}
          <h4>Limits of this milestone</h4><ul>{quote.expectations.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></>}
        {quote.reasons.length > 0 && status !== USAGE_LIMIT_MESSAGE && <ul>{quote.reasons.map((reason, index) => <li key={index}>{apiFailure({ok: false, status: 409, data: {detail: {code: typeof reason === "string" ? reason : reason.code}}}, "starting your build")}</li>)}</ul>}
        {quoteExpired && <ErrorNotice error="This estimate has expired. Review a new estimate before starting." source="builds.this_estimate_has_expired" />}
        {quote.ready && !budgetReady && !quoteExpired && <ErrorNotice error={USAGE_LIMIT_MESSAGE} source="builds.limit" />}
        <div className={styles.actions}><button type="button" data-usage-control="build_start" disabled={busy || !budgetReady || activeRun || !!syncError} onClick={() => void start()}>Retry build</button>
          <button type="button" disabled={busy} onClick={() => { setQuote(null); setApproved(false); }}>Dismiss estimate</button></div>
      </section>}
      {appId && <section className={styles.card}><h2>Saved build runs</h2>
        <ErrorNotice error={runtimeError} source="builds.runtimeError" />
        {runtime && <div role="status"><h3>Application runtime</h3>
          <p>Generation {runtime.generation}: {human(runtime.phase)} · {human(runtime.state)}.</p>
          {runtime.preview_url && <p>Private functional review is ready at <a href={runtime.preview_url} target="_blank" rel="noopener noreferrer">Open application preview</a>. Sign-in, saved data, exports and account isolation still need live verification before launch.</p>}
          {!runtime.preview_url && <p>{["failed", "cancelled"].includes(runtime.state)
            ? "Application provisioning stopped. Review the saved build and request a repair; no usable product is established."
            : "Supymem is preparing the application runtime. The source QA result alone does not prove the product works."}</p>}
        </div>}
        {builds === null ? <p>Build history has not synchronized.</p> : builds.length === 0 ? <p>No builds have been recorded for this product.</p> :
          <label className={styles.field}>Run<select disabled={busy} value={build?.id || ""} onChange={event => selectResult(appId, event.target.value)}>
            <option value="" disabled>Choose a saved run</option>
            {builds.map(item => <option key={item.id} value={item.id}>{item.created_at ? new Date(item.created_at).toLocaleString() : item.id} · {item.runtime_version === 1 ? human(item.state) : "Legacy record, execution unverified"}</option>)}</select></label>}
        {build && <>
          <p>{build.runtime_version === 1
            ? `Run: ${human(build.state)}${build.review?.status === "accepted" ? (build.revision_instruction ? " · site revision accepted" : " · milestone accepted") : build.review?.status === "rejected" ? " · changes requested" : build.phase ? ` · ${human(build.phase)}` : ""}`
            : "This legacy record has no verified execution receipt."}</p>
          {build.revision_instruction && <p><strong>Requested site change:</strong> {build.revision_instruction}</p>}
          {build.landing_page?.available && <p role="status">Your landing page is ready. {build.landing_page.qa_note}
            {" "}<a href={`/v/${encodeURIComponent(ventureId)}/site?app=${encodeURIComponent(appId)}&run=${encodeURIComponent(build.id)}`}>Review page</a></p>}
          {build.error_code && !build.landing_page?.available && <ErrorNotice error={buildErrorLabel(build)} source="builds.build_error" />}
          <OverheadPolicy policy={build.included_overhead} />
          <p>QA: {build.runtime_version === 1 && build.qa_state && build.qa_state !== "not_run" ? human(build.qa_state) : "No verified result"}
            {!build.verified_execution && !build.verified_source_import && build.qa_state === "passed" && " · Complete execution not yet verified"}
            {build.verified_source_import && " · Saved source and independent QA verified"}</p>
          {build.cleanup_by_purpose ? <p>Runtime cleanup, builder: {human(build.cleanup_by_purpose.builder)} · QA: {human(build.cleanup_by_purpose.qa)}</p>
            : build.cleanup_state && <p>Cleanup: {human(build.cleanup_state)}</p>}
          {running(build) && <button type="button" disabled={busy || build.state === "cancelling"} onClick={() => void cancel()}>Cancel this run</button>}
          <div id="build-review"><BuildEvidence key={build.id} build={build} onReviewed={() => void action(async () => { await load(); if (active()) await loadBuilds(appId); })} /></div>
          <h3>Activity</h3>{events.length ? <ol>{events.map(event => <li key={event.seq}>{buildEventLabel(event)}</li>)}</ol> : <p>No activity has synchronized yet.</p>}
          <div className={styles.actions}>
            {eventAfter > 0 && <button type="button" disabled={busy} onClick={() => void loadBuild(build.id, appId, 0)}>First activity</button>}
            {events.length === 200 && <button type="button" disabled={busy} onClick={() => void loadBuild(build.id, appId, events[events.length - 1].seq)}>Next activity</button>}
          </div>
        </>}
      </section>}
      <NextStepStrip ventureId={ventureId} compact />
    </div>
  </AppShell>;
}
