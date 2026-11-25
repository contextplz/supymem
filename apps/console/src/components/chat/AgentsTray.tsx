"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { useActivityRefresh, watchActivity } from "@/lib/activityRefresh";
import { isLocalConsole } from "@/lib/localMode";
import { delayedJobNotice, isDurableJob, jobFinished, jobSucceeded, jobWaitingLabel, unexpectedJobFailure, type DurableJob, type ResearchProgress } from "@/lib/durableJobs";
import { desktopInvoke, desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import { deviceOrderStatus, type SavedDeviceOrderState } from "@/lib/deviceOrderStatus";
import { isVerifiedMacTask } from "@/lib/macResult";
import { buildStageLabel, productBuildStatus, safeChangedFiles, safeLoopText, type LoopEvent, type LoopIteration, type ProductBuildWatch, type ChangedFile } from "@/lib/loopProgress";
import { LoopProgress } from "@/components/desktop/LoopProgress";
import { ProductBuildReview, type ProductLoopReview, type ProductPreviewReceipt } from "@/components/desktop/ProductBuildReview";
import { parseEmployeeStamp } from "@/components/office/avatars";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { ChatAnchor } from "./ChatLink";
import { PageResultLink } from "./PageResultLink";
import type { ChatMessage } from "@/lib/api-types";
import { validInboxBriefing } from "@/lib/inboxBriefing";
import styles from "./AgentsTray.module.css";
import dockStyles from "./ChatDock.module.css";

type BuildActivity = { orderId?: string; jobId?: string; loop?: ProductLoopReview; watch?: ProductBuildWatch; events?: LoopEvent[];
  iterations?: LoopIteration[];
  files?: ChangedFile[]; preview?: ProductPreviewReceipt; previewHref?: string; exportHref?: string };
export type AgentJob = DurableJob & { steps: string[]; task_id?: string; label?: string; reviewHref?: string; finished_at?: string | null; failure_reason?: string | null; build?: BuildActivity };
type DeviceOrderJob = Omit<AgentJob, "steps">;
const finishedOrderStates = new Set(["cancelled", "stopped", "failed", "expired"]);
const orderRetryMs = 30_000;
type DeviceOrder = { id: string; venture_id: string; state: SavedDeviceOrderState; claimed_at?: string;
  events?: LoopEvent[];
  expires_at: string; closed_at?: string; failure_code?: string; result_summary?: string; execution_id?: string; stop_reconciliation_required?: boolean; inference_reconciliation_required?: boolean;
  lease_reconciliation?: {process_cleanup_proven?: boolean};
  native_stop?: {execution_id?: string; task_verified?: boolean; processes_stopped?: boolean};
  iterations?: (LoopIteration & {execution_id?: string})[]; task_snapshot?: {kind?: string; pagebuilder_job_id?: string}; manifest: { task_id?: string; title: string; duration_seconds?: number; coding_loop_verification?: ProductLoopReview;
    coding_verification?: {provider: string; workspace_slug: string; operation: string} } };
type MacReceipt = { state: string; order_id?: string; message?: string; founder_prompt?: string;
  build_deadline_at?: number; loop_phase?: string;
  events?: LoopEvent[]; task_verified?: boolean; failure_code?: string; effect_outcome_unconfirmed?: boolean; credits_reconciled?: boolean;
  evidence?: ProductBuildWatch["evidence"]; changed_files?: ChangedFile[]; product_preview?: ProductPreviewReceipt };

const labels: Record<string, [string, string, string, string]> = {
  "browse.web": ["⌕", "Browsing public pages", "Browse notes ready", "web browse"],
  "research.deep": ["⌕", "Researching competitors", "Research ready", "research"],
  "idea.validate": ["⌕", "Researching your idea", "Research ready", "research"],
  "idea.clarify_turn": ["?", "Preparing your questions", "Questions ready", "questions"],
  "plan.compile": ["≡", "Preparing your plan", "Plan ready", "planning"],
  "gtm.journey.prepare": ["◎", "Finding customers", "Customer search ready", "customer search"],
  "pagebuilder.build": ["▤", "Building your page", "Page ready", "page build"],
  "pagebuilder.publish": ["▤", "Publishing your page", "Page published", "page publish"],
  "experiment.run": ["⚗", "Running your experiment", "Experiment ready", "experiment"],
  "experiment.exploration.prepare": ["?", "Exploring your idea", "Ideas ready", "idea exploration"],
  "device.work_order": ["▣", "Your coding tool is working", "Your coding task is ready", "coding task"],
  chat: ["•", "Preparing your reply", "Reply ready", "reply"],
  idea: ["?", "Exploring your idea", "Idea ready", "idea"],
  research: ["⌕", "Researching", "Research ready", "research"],
  gtm: ["◎", "Finding customers", "Customer search ready", "customer search"],
  content: ["≡", "Preparing your content", "Content ready", "content"],
  task: ["✓", "Completing your request", "Request ready", "request"],
  tasks: ["✓", "Completing your request", "Request ready", "request"],
  simulation: ["⌕", "Checking your message", "Message check ready", "message check"],
  build: ["▤", "Building your app", "App ready", "app build"],
  qa: ["✓", "Checking your app", "App checks ready", "app checks"],
  deploy: ["▤", "Updating your live app", "Live app updated", "app update"],
};
const jobLabel = (job: AgentJob) => labels[job.kind] || labels[job.kind.split(".")[0]] || ["•", "Working on your task", "Task ready", "task"];
const trayText = (value: unknown) => safeLoopText(value)
  .replace(/waiting for (?:a |an |the )?(?:worker|runner)\b|\bStarting soon\b/gi, "Queued")
  .replace(/\bcompany jobs?\b/gi, "work")
  .replace(/\b(?:jobs?|workers?|runners?)\b/gi, "task")
  .replace(/\bcall budget\b/gi, "search limit");
const searchStageLabels = new Map([
  ["planning", "Planning the search"], ["searching", "Searching public sources"],
  ["judging", "Checking fit"], ["contacting", "Finding contact routes"], ["drafting", "Writing drafts"],
]);
const pageAttemptId = (job?: AgentJob) => job?.kind === "pagebuilder.build" ? job.id : job?.build?.jobId;
const trayWaitStatus = (job: AgentJob, now: number) => {
  if (!jobWaitingLabel(job)) return null;
  if (job.wait?.reason === "paused") return "Paused";
  if (job.wait?.reason === "cancel_requested") return "Stopping";
  const due = Math.max(...[job.wait?.until, job.scheduled_for].map(value => Date.parse(value || "")).filter(Number.isFinite));
  if (due > now) return `Scheduled for ${new Date(due).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"})}`;
  return delayedJobNotice(job, now) ? "Delayed" : "Queued";
};
const trayLabel = (job: AgentJob, now: number) => {
  if (job.build || job.kind === "pagebuilder.build") return buildStageLabel(job.progress?.step_label || "", job.build?.loop?.provider, job.status);
  const [, running, ready, task] = jobLabel(job);
  if (jobSucceeded(job)) return ready;
  if (jobFinished(job)) return job.status === "cancelled" ? `${task[0].toUpperCase()}${task.slice(1)} stopped`
    : `Your ${task} ${task === "questions" || task === "app checks" ? "need" : "needs"} you`;
  return jobWaitingLabel(job) ? `${trayWaitStatus(job, now)}: ${task}` : running;
};

export function browseProgressFor(messages: ChatMessage[]): (ResearchProgress & { job_id: string })[] {
  const latest = new Map<string, ResearchProgress & { job_id: string }>();
  for (const message of messages) {
    if (message.role !== "assistant" || !["browse_progress", "browse_result"].includes(message.kind || "")) continue;
    const progress = message.citations?.find(citation => citation.browse_progress)?.browse_progress;
    if (!progress || typeof progress.job_id !== "string" || !progress.job_id
      || !["queued", "running", "ready", "partial"].includes(progress.state)
      || !Number.isFinite(progress.percent) || progress.percent < 0 || progress.percent > 100
      || typeof progress.step_label !== "string"
      || progress.started_at !== null && (typeof progress.started_at !== "string" || !Number.isFinite(Date.parse(progress.started_at)))) continue;
    latest.set(progress.job_id, { ...progress, step_label: safeLoopText(progress.step_label) });
  }
  return [...latest.values()];
}

const codingTaskKey = (job: AgentJob) => job.kind === "device.work_order" || job.build?.orderId
  ? JSON.stringify([job.venture_id, job.workspace_id, job.task_id || job.build?.orderId || job.id]) : null;

function latestCodingAttempts(jobs: AgentJob[]): AgentJob[] {
  const latest = new Map<string, AgentJob>();
  for (const job of jobs) {
    const key = codingTaskKey(job);
    if (!key) continue;
    const prior = latest.get(key);
    if (!prior || (Date.parse(job.created_at || job.started_at || "") || 0) > (Date.parse(prior.created_at || prior.started_at || "") || 0)) latest.set(key, job);
  }
  const kept = jobs.filter(job => !codingTaskKey(job) || latest.get(codingTaskKey(job)!) === job);
  return kept.length === jobs.length ? jobs : kept;
}

export function collapseAgentStatuses(messages: ChatMessage[], inboxBriefingEnabled = false, jobs: AgentJob[] = [], ventureId?: string): ChatMessage[] {
  const seen = new Set<string>();
  const browseLatest = new Map<string, string>();
  const codingLatest = new Map<string, string>();
  const attempts = new Map(latestCodingAttempts(jobs).filter(job => codingTaskKey(job))
    .map(job => [JSON.stringify([job.venture_id, job.task_id || job.build?.orderId || job.id]), job]));
  const codingReference = (message: ChatMessage) => {
    const action = message.actions?.find(item => typeof item.payload?.order_id === "string"
      || /\/device-work-orders\/[^/?#]+/.test(item.href || "") || item.task_id === "landing_page_build"
      || typeof item.payload?.job_id === "string" && message.kind === "mac_task_stopped");
    const orderId = action?.payload?.order_id || action?.payload?.job_id || action?.href?.match(/\/device-work-orders\/([^/?#]+)/)?.[1];
    const jobId = message.run_id || message.citations?.find(item => item.job_started)?.job_started?.job_id || action?.payload?.job_id;
    const job = jobs.find(item => typeof orderId === "string" && (item.id === orderId || item.build?.orderId === orderId)
      || typeof jobId === "string" && (item.id === jobId || item.build?.jobId === jobId));
    const task = job?.task_id || action?.task_id || action?.payload?.task_id;
    const venture = job?.venture_id || (typeof action?.payload?.venture_id === "string" ? action.payload.venture_id : undefined)
      || (action?.href || message.actions?.find(item => item.href)?.href)?.match(/\/v\/([^/?#]+)\//)?.[1] || ventureId || jobs[0]?.venture_id;
    if (legacyPageStart(message)) return JSON.stringify([venture, "landing_page_build"]);
    // shortcut: legacy notices without task IDs use identical copy, upgrade when all saved notices carry task IDs.
    return typeof task === "string" ? JSON.stringify([venture, task]) : typeof orderId === "string"
      ? JSON.stringify([venture, parseEmployeeStamp(message.body_md || message.text || "").text.trim()]) : null;
  };
  function legacyPageStart(message: ChatMessage) {
    return message.kind === "text" && /^Your coding tool\b.*\bpage\b/i.test(parseEmployeeStamp(message.body_md || "").text)
      && message.actions?.some(action => action.action === "status" || /\/v\/[^/]+\/builds$/.test(action.href || ""));
  }
  const codingNotice = (message: ChatMessage) => ["build_tool_started", "mac_task_stopped", "mac_task_finished"].includes(message.kind || "")
    || legacyPageStart(message)
    || message.actions?.some(action => action.task_id === "landing_page_build")
    || /^Your coding tool\b/.test(parseEmployeeStamp(message.body_md || "").text)
      && message.actions?.some(action => /\/device-work-orders\/[^/?#]+$/.test(action.href || ""))
    || jobs.some(job => (job.build || job.kind === "pagebuilder.build") && (job.id === message.run_id || job.build?.jobId === message.run_id && !!message.run_id
      || message.citations?.some(item => item.job_started?.job_id && [job.id, job.build?.jobId].includes(item.job_started.job_id))));
  messages.forEach(message => {
    const progress = message.role === "assistant" && ["browse_progress", "browse_result"].includes(message.kind || "")
      ? message.citations?.find(citation => citation.browse_progress)?.browse_progress : null;
    if (progress?.job_id) browseLatest.set(progress.job_id, message.id);
    if (message.role === "assistant" && codingNotice(message)) {
      const task = codingReference(message);
      if (task) codingLatest.set(task, message.id);
    }
  });
  const buildRecommendations = new Set(messages.filter(message => message.kind === "mac_task_stopped" && codingLatest.get(codingReference(message) || "") === message.id)
    .flatMap(message => message.actions?.flatMap(action => {
      if (typeof action.payload?.build_action === "string") return [action.payload.build_action];
      const order = action.payload?.order_id || action.payload?.job_id || action.href?.match(/\/device-work-orders\/([^/?#]+)/)?.[1];
      const job = jobs.find(item => item.id === order || item.build?.orderId === order);
      return job?.build?.loop ? [job.build.loop.profile === "static_landing_v1" ? "pagebuilder.build" : "build.tool.start"] : [];
    }) || []));
  return messages.flatMap((message, index) => {
    if (message.role !== "assistant") return [message];
    if (codingNotice(message)) {
      const task = codingReference(message);
      if (task && codingLatest.get(task) !== message.id) return [];
      if (message.kind === "text" && message.actions?.some(action => action.action === "status" && typeof action.payload?.job_id === "string")) return [message];
      const attempt = task ? attempts.get(task) : undefined;
      const sourceAction = message.actions?.find(action => /\/device-work-orders\/[^/?#]+$/.test(action.href || ""));
      const orderId = attempt?.build?.orderId || attempt?.id || sourceAction?.payload?.order_id
        || sourceAction?.href?.match(/\/device-work-orders\/([^/?#]+)/)?.[1];
      const reviewHref = attempt?.reviewHref || message.actions?.find(action => /\/device-work-orders\/[^/?#]+$/.test(action.href || ""))?.href;
      const review = reviewHref ? [{label: "Watch your tool", href: reviewHref, style: "primary" as const,
        task_id: attempt?.task_id || sourceAction?.task_id,
        payload: {order_id: orderId, task_id: attempt?.task_id || sourceAction?.task_id}}] : [];
      if (attempt && jobSucceeded(attempt)) message = {...message, kind: "mac_task_finished", text: undefined,
        body_md: "<!--employee:Charlie:Manager:manager-->\nYour coding tool finished this task. Choose Show status here to review your saved result, or Preview and Export below when your page is ready.",
        actions: [...review,
          ...(attempt.build?.previewHref ? [{label: "Preview", href: attempt.build.previewHref}] : []),
          ...(attempt.build?.exportHref ? [{label: "Export", href: attempt.build.exportHref}] : []),
          {label: "Show status here", action: "status", style: "primary", task_id: attempt.task_id,
            payload: {order_id: orderId, task_id: attempt.task_id}}]};
      else if (message.kind === "mac_task_stopped" || attempt && jobFinished(attempt) && !jobSucceeded(attempt)) {
        const page = attempt?.kind === "pagebuilder.build" || attempt?.task_id === "landing_page_build"
          || attempt?.build?.loop?.profile === "static_landing_v1"
          || message.actions?.some(action => action.payload?.build_action === "pagebuilder.build");
        const retryJobId = page ? pageAttemptId(attempt) : undefined;
        const reason = (safeLoopText(attempt?.failure_reason, 500) || parseEmployeeStamp(message.body_md || "").text.trim())
          .replace(/\bOpen Build activity\b/g, review.length ? "Choose Watch your tool below" : "Choose Show status here below")
          .replace(/\bchoose Retry on Website\b/g, retryJobId ? "choose Retry below" : "check the saved status");
        message = {...message, kind: "mac_task_stopped", text: undefined,
          body_md: `<!--employee:Charlie:Manager:manager-->\n${reason || "Your coding tool stopped before your result was ready."}\n\n${retryJobId ? "Choose Retry below to build your page again, or Show status here to review what happened." : review.length ? "Choose Watch your tool to review your saved task." : "Choose Show status here below to review what happened."}`,
          actions: [...review, ...(page ? (retryJobId ? [{label: "Retry", action: "pagebuilder.build", payload: {retry_job_id: retryJobId}, style: "primary" as const}] : []) :
            (message.actions || []).filter(action => !action.href?.endsWith("/site") && !action.href?.endsWith("/builds"))),
            {label: "Show status here", action: "status"}]};
      } else message = {...message, text: undefined,
        body_md: `<!--employee:Charlie:Manager:manager-->\n${review.length
          ? "Your private build uses your coding tool. Choose Watch your tool below to watch, stop it, or open your result folder."
          : "Your request is saved for your coding tool. Choose Show status here below to watch your tool when it starts."} Nothing is published.`,
        actions: [...review, {label: "Show status here", action: "status", style: "primary"}]};
    }
    const prior = messages[index - 1];
    if (message.kind === "next_moves" && prior?.kind === "mac_task_stopped"
      && message.actions?.some(action => buildRecommendations.has(action.action || action.name || ""))) return [];
    if (message.next_moves?.actions?.some(action => buildRecommendations.has(action.action || action.name || ""))) message = {...message, next_moves: undefined};
    if (message.kind === "browse_progress") {
      const progress = message.citations?.find(citation => citation.browse_progress)?.browse_progress;
      if (progress && browseLatest.get(progress.job_id) !== message.id) return [];
      return [message];
    }
    if (inboxBriefingEnabled && ["inbox_briefing", "inbox_urgent"].includes(message.kind || "")
      && validInboxBriefing(message.citations?.find(citation => citation.inbox_briefing)?.inbox_briefing)) return [message];
    let body = parseEmployeeStamp(message.body_md || "").text.trim();
    if (!body && message.text?.trim()) {
      message = { ...message, body_md: message.text };
      body = parseEmployeeStamp(message.body_md || "").text.trim();
    }
    if (!body && !message.parts?.some(part => part.type === "diagram" && typeof part.title === "string" && typeof part.svg === "string")
      && !message.founder_references?.startups.length && !message.founder_references?.yc_advice.length
      && !message.citations?.some(citation => citation.label?.trim())
      && !message.actions?.some(action => action.label?.trim() || action.confirmation || typeof action.payload?.question_id === "string")
      && !["startup_review", "market_beliefs", "idea_clarification_ready"].includes(message.kind || "")) return [];
    if (message.parts?.length || message.actions?.some(action => action.confirmation)) return [message];
    const text = body.replace(/^Your saved [^:\n]+:\s*\n/i, "");
    if (text !== body && text.slice(text.indexOf("\n") + 1).trim() && text.includes("\n")) return [message];
    const status = text.match(/^(?:your |saved )?(research|plan(?: generation| revision)?|customer search|page build|experiment|mac task)(?: status:| is|:)?\s+(?:queued|running|in progress)\b/i);
    if (message.kind !== "gtm_search_progress" && !status) {
      const result = text.match(/^(?:your )?(research|plan|customer search|page build|experiment|mac task)\b[^.\n]*\b(?:ready|finished|did not finish|stopped|failed)\b/i);
      if (result) seen.delete(result[1].toLowerCase());
      return [message];
    }
    const reference = message.gtm_search_progress;
    const task = message.kind === "gtm_search_progress" ? "customer search" : status![1].replace(/\s+(generation|revision)$/i, "").toLowerCase();
    const key = reference?.job_id || reference?.run_id || message.confirmed_action?.run_id || task;
    if (seen.has(key)) return [];
    seen.add(key);
    return [{ ...message, body_md: `Started ${task}. You can keep chatting while it runs.`, text: undefined }];
  });
}

export function updateAgentJobs(previous: AgentJob[], incoming: DurableJob[]): AgentJob[] {
  incoming = incoming.map(job => job.kind === "pagebuilder.build" && job.progress ? { ...job, progress: { ...job.progress,
    step_label: buildStageLabel(job.progress.step_label || "", (job as AgentJob).build?.loop?.provider, job.status) } } : job);
  const updates = new Map(incoming.map(job => [job.id, job]));
  return [...previous.map(job => {
    const next = updates.get(job.id);
    updates.delete(job.id);
    if (!next) return job;
    const step = safeLoopText(next.progress?.step_label);
    return { ...job, ...next, finished_at: jobFinished(next) ? (next as AgentJob).finished_at || job.finished_at || new Date().toISOString() : null,
      steps: step && !job.steps.includes(step) ? [...job.steps, step] : job.steps };
  }), ...[...updates.values()].filter(job => !jobFinished(job) || job.kind === "device.work_order").map(job => ({ ...job,
    steps: safeLoopText(job.progress?.step_label) ? [safeLoopText(job.progress?.step_label)] : [] }))];
}

export function useAgentJobs(ventureId: string, visible: boolean, onFinished?: () => void | Promise<void>) {
  const activity = useActivityRefresh(ventureId);
  const scope = JSON.stringify([verifiedSessionSubject(), verifiedSessionEpoch(), ventureId]);
  const storageKey = `supymem.agents:${encodeURIComponent(scope)}`;
  const dismissedStorageKey = `supymem.agents.dismissed:${encodeURIComponent(JSON.stringify([verifiedSessionSubject(), ventureId]))}`;
  const [saved, setSaved] = useState<{ scope: string; jobs: AgentJob[]; error: string; now: number }>({ scope, jobs: [], error: "", now: Date.now() });
  const tracked = useRef<{ scope: string; jobs: AgentJob[] }>({ scope, jobs: [] });
  const orderCache = useRef({ scope, finished: new Map<string, { state: string; job: DeviceOrderJob | null }>(),
    rechecks: new Map<string, { state: string; retryAt: number; job: DeviceOrderJob | null }>(), sourceArchives: new Map<string, string>() });
  const finish = useRef(onFinished);
  finish.current = onFinished;
  const acknowledged = useRef<{ scope: string; ids: Set<string> }>({ scope, ids: new Set() });
  const dismissed = useRef<{ scope: string; ids: Set<string> }>({ scope, ids: new Set() });
  const currentScope = useRef(scope);
  const refresh = useRef<() => void>(() => {});
  currentScope.current = scope;
  useEffect(() => {
    if (!visible || !ventureId || !verifiedSessionSubject()) return;
    let live = true, polling = false, workspace = "";
    const current = () => live && currentScope.current === scope
      && scope === JSON.stringify([verifiedSessionSubject(), verifiedSessionEpoch(), ventureId]);
    if (tracked.current.scope !== scope) tracked.current = { scope, jobs: [] };
    if (orderCache.current.scope !== scope) orderCache.current = { scope, finished: new Map(), rechecks: new Map(), sourceArchives: new Map() };
    const { finished, rechecks, sourceArchives } = orderCache.current;
    if (acknowledged.current.scope !== scope) acknowledged.current = { scope, ids: new Set() };
    if (dismissed.current.scope !== scope) dismissed.current = { scope, ids: new Set() };
    try {
      const ids = JSON.parse((window.localStorage || window.sessionStorage).getItem(dismissedStorageKey) || "[]");
      if (Array.isArray(ids) && ids.every(id => typeof id === "string")) ids.forEach(id => dismissed.current.ids.add(id));
    } catch {}
    if (!tracked.current.jobs.length) {
      try {
        const stored = JSON.parse(window.sessionStorage.getItem(storageKey) || "[]");
        if (Array.isArray(stored) && stored.every((job: AgentJob) => isDurableJob(job) && job.venture_id === ventureId && Array.isArray(job.steps)
          && job.steps.every((step: unknown) => typeof step === "string"))) {
          tracked.current.jobs = stored.map((job: AgentJob) => ({ ...job, label: safeLoopText(job.label),
            reviewHref: job.kind === "device.work_order" ? `/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(job.id)}` : undefined }));
        }
      } catch {}
    }
    const publish = (jobs: AgentJob[], error = "") => {
      if (!current()) return;
      const kept = jobs.filter(job => !dismissed.current.ids.has(job.id) && (!job.build?.orderId || !dismissed.current.ids.has(job.build.orderId)));
      tracked.current = { scope, jobs: kept };
      setSaved({ scope, jobs: kept, error, now: Date.now() });
      try { window.sessionStorage.setItem(storageKey, JSON.stringify(kept)); } catch {}
    };
    publish(tracked.current.jobs);
    const notify = async (jobs: AgentJob[]) => {
      const finished = jobs.filter(job => jobFinished(job) && !acknowledged.current.ids.has(job.id));
      if (finished.length && finish.current && current()) {
        await finish.current();
        if (current()) finished.forEach(job => acknowledged.current.ids.add(job.id));
      }
    };
    const poll = async () => {
      if (polling || document.hidden || !current()) return;
      polling = true;
      try {
        if (!workspace) {
          const venture = await api(`/api/ventures/${encodeURIComponent(ventureId)}`);
          const id = (venture.data as { workspace_id?: unknown } | null)?.workspace_id;
          if (!venture.ok || typeof id !== "string" || !id) throw new Error("Your running work could not be loaded. Checking again shortly.");
          workspace = id;
        }
        if (!current()) return;
        const query = new URLSearchParams({ venture_id: ventureId, active_only: "true", limit: "200" });
        const response = await api(`/api/workspaces/${encodeURIComponent(workspace)}/jobs?${query}`);
        const items = (response.data as { items?: unknown[] } | null)?.items;
        const resolvedItems = (response.data as { resolved_ids?: unknown } | null)?.resolved_ids;
        if (!response.ok || !Array.isArray(items) || items.some(job => !isDurableJob(job) || job.venture_id !== ventureId)) {
          throw new Error("Your running work could not be synchronized. Checking again shortly.");
        }
        if (resolvedItems !== undefined && (!Array.isArray(resolvedItems) || resolvedItems.some(id => typeof id !== "string" || !id))) {
          throw new Error("Your saved results could not be synchronized. Checking again shortly.");
        }
        const resolved = new Set<string>(Array.isArray(resolvedItems) ? resolvedItems : []);
        const jobs = items as DurableJob[];
        jobs.forEach(job => { dismissed.current.ids.delete(job.id); acknowledged.current.ids.delete(job.id); });
        resolved.forEach(id => dismissed.current.ids.add(id));
        const activeIds = new Set(jobs.map(job => job.id));
        const missing = tracked.current.jobs.filter(job => job.kind !== "device.work_order" && !jobFinished(job) && !activeIds.has(job.id));
        const ended = await Promise.all(missing.map(async job => {
          const result = await api(`/api/jobs/${encodeURIComponent(job.id)}`);
          if (!result.ok || !isDurableJob(result.data) || result.data.id !== job.id || result.data.venture_id !== ventureId) {
            throw new Error("A task's saved result could not be confirmed. Checking again shortly.");
          }
          return result.data;
        }));
        const detailed = await Promise.all([...jobs, ...ended].map(async job => {
          if (job.kind !== "pagebuilder.build") return job;
          const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder/jobs/${encodeURIComponent(job.id)}`);
          const detail = result.data as { job_id?: string; state?: string; stage?: string; progress?: number; order_id?: string; error_reason?: string } | null;
          if (!result.ok || detail?.job_id !== job.id || typeof detail.stage !== "string") return job;
          const status = detail.state === "ready" ? "done" : detail.state === "failed" ? "failed" : job.status;
          const prefix = `/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder/jobs/${encodeURIComponent(job.id)}`;
          return { ...job, status, failure_reason: safeLoopText(detail.error_reason, 500) || job.failure_reason, build: { orderId: detail.order_id,
            ...(detail.state === "ready" ? {previewHref: `${prefix}/preview`, exportHref: `${prefix}/source`} : {}) },
            progress: { state: status, step_label: buildStageLabel(detail.stage, "", status),
              percent: typeof detail.progress === "number" ? detail.progress * 100 : null, started_at: job.started_at || null } };
        }));
        if (!current()) return;
        let next = updateAgentJobs(tracked.current.jobs, detailed).filter(job => !resolved.has(job.id));
        const local = isLocalConsole();
        if (!local) {publish(next); await notify(next);}
        else {
          const research = next.filter(job => job.kind === "research.deep");
          if (research.length) {
            publish([...research, ...tracked.current.jobs.filter(job => job.kind !== "research.deep")]);
            await notify(research);
          }
        }
        if (!current() || document.hidden) return;
        const orders = await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders`);
        if (!current() || document.hidden) return;
        const devices = (orders.data as { orders?: { id: string; state: string; created_at?: string }[] } | null)?.orders;
        if (!orders.ok || !Array.isArray(devices) || devices.some(order => !order || typeof order.id !== "string" || typeof order.state !== "string")) {
          throw new Error("Your coding tool's status could not be loaded. Choose Show status here in chat to check again.");
        }
        const summaries = new Map(devices.map(order => [order.id, order]));
        const orderIds = new Set([...devices.filter(order => local || ["claimed", "waiting_for_device"].includes(order.state)).map(order => order.id),
          ...next.flatMap(job => job.build?.orderId ? [job.build.orderId] : job.kind === "device.work_order" && !jobFinished(job) ? [job.id] : [])]);
        const macJobs = await Promise.allSettled([...orderIds].map(async id => {
          if (local && dismissed.current.ids.has(id)) return null;
          const summary = summaries.get(id);
          const cached = finished.get(id);
          if (cached && (!summary || summary.state === cached.state)) return cached.job;
          finished.delete(id);
          const recheck = rechecks.get(id);
          if (recheck && (!summary || summary.state === recheck.state) && Date.now() < recheck.retryAt) return recheck.job;
          rechecks.delete(id);
          const prefix = `/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(id)}`;
          const detail = await api(prefix);
          if (!current() || document.hidden) return null;
          if (local && summary && finishedOrderStates.has(summary.state) && detail.status === 404) {
            rechecks.set(id, { state: summary.state, retryAt: Date.now() + orderRetryMs, job: null });
            return null;
          }
          const order = detail.data as DeviceOrder | null;
          if (!detail.ok || order?.id !== id || order.venture_id !== ventureId || typeof order.manifest?.title !== "string"
            || !["pending", "approved", "claimed", "waiting_for_device", "cancelled", "stopped", "failed"].includes(order.state)) {
            throw new Error("Your coding tool's saved progress could not be confirmed. Choose Show status here in chat to check again.");
          }
          const savedState = summary?.state === "expired" && ["pending", "approved"].includes(order.state) ? "expired" : order.state;
          const needsStop = savedState === "stopped" || (savedState === "cancelled" && !!(order.claimed_at || order.execution_id));
          const stopSettled = !order.stop_reconciliation_required && !order.inference_reconciliation_required
            && (!needsStop || !!order.closed_at && order.lease_reconciliation?.process_cleanup_proven !== false);
          const remember = (job: DeviceOrderJob | null, settled: boolean) => {
            if (local && finishedOrderStates.has(savedState) && current()) {
              if (settled) finished.set(id, { state: savedState, job });
              else rechecks.set(id, { state: savedState, retryAt: Date.now() + orderRetryMs, job });
            }
            return job;
          };
          const coding = order.manifest.coding_loop_verification || order.manifest.coding_verification;
          if (local && !coding) return remember(null, stopSettled);
          let receipt: MacReceipt | null = null;
          const desktop = isDesktopShell();
          if (desktop) {
            const value = await desktopInvoke("device_work_order_action", { ventureId, orderId: order.id, action: "status" }) as MacReceipt | null;
            if (!current() || document.hidden) return null;
            if (value?.order_id === order.id) receipt = value;
            else if (local && !finishedOrderStates.has(savedState)) throw new Error("Your tool's status could not be confirmed. Choose Watch your tool in chat, then Refresh build.");
          }
          const nativeSettled = !needsStop || !desktop || (receipt?.credits_reconciled !== false
            && receipt?.evidence?.processes_stopped !== false
            && (receipt?.state === "stopped" || (receipt?.state === "not_started" && order.native_stop?.processes_stopped === true)));
          if (local && order.state === "stopped" && order.execution_id && order.native_stop?.execution_id === order.execution_id) {
            const last = order.iterations?.at(-1);
            const verified = order.native_stop.task_verified === true && order.native_stop.processes_stopped === true
              && !order.stop_reconciliation_required && !order.inference_reconciliation_required;
            receipt = {...receipt, state: "stopped", order_id: order.id, task_verified: verified, failure_code: order.failure_code,
              effect_outcome_unconfirmed: !!order.stop_reconciliation_required || !!order.inference_reconciliation_required,
              evidence: {...receipt?.evidence, processes_stopped: order.native_stop.processes_stopped,
                product_build: {...receipt?.evidence?.product_build, outcome: last?.execution_id === order.execution_id ? last.outcome : undefined}}};
          }
          const stopped = receipt?.state === "stopped" || order.state === "stopped";
          const status = stopped ? isVerifiedMacTask(receipt) ? "done" : "failed" : order.state === "cancelled" ? "cancelled" : receipt?.failure_code ? "failed" : "running";
          if (local && coding) {
            const loop = order.manifest.coding_loop_verification || {...order.manifest.coding_verification!, profile: "static_html_v1",
              phase: "implement" as const, max_iterations: 1, max_wall_seconds: order.manifest.duration_seconds || 0, per_call_seconds: order.manifest.duration_seconds || 0};
            const expired = ["pending", "approved"].includes(order.state) && !order.claimed_at && Date.parse(order.expires_at) <= Date.now();
            const watch: ProductBuildWatch = {state: ["failed", "cancelled", "stopped"].includes(order.state) ? order.state : receipt?.state || order.state,
              task_verified: isVerifiedMacTask(receipt),
              build_deadline_at: receipt?.build_deadline_at,
              failure_code: order.failure_code || receipt?.failure_code || (order.state === "failed" ? "build_failed" : expired ? "build_expired" : undefined),
              evidence: {processes_stopped: receipt?.evidence?.processes_stopped,
                outcome: receipt?.evidence?.outcome, product_build: {outcome: receipt?.evidence?.product_build?.outcome}}};
            const buildStatus = productBuildStatus(watch);
            const state = buildStatus === "finished" ? "done" : ["stopped", "cancelled"].includes(buildStatus) ? "cancelled"
              : ["missing", "signed_out", "disconnected", "failed", "stop_unconfirmed"].includes(buildStatus) ? "failed"
              : buildStatus === "ready" ? "pending" : "running";
            const files = safeChangedFiles(receipt?.changed_files || receipt?.evidence?.product_build?.changed_files);
            const build: BuildActivity = {orderId: order.id, jobId: order.task_snapshot?.pagebuilder_job_id, loop, watch, files, preview: receipt?.product_preview,
              iterations: order.iterations?.map(row => ({index: row.index, phase: row.phase, outcome: row.outcome,
                summary: safeLoopText(row.summary, 280), diagnosis: {failures: row.diagnosis?.failures?.map(failure =>
                  safeLoopText(typeof failure === "string" ? failure
                    : failure && typeof failure === "object" && "message" in failure ? failure.message : "A check needs a fix. Choose Watch your tool below to review it, then retry."))}})),
              events: (order.events?.length ? order.events : receipt?.events || []).slice(-5)
                .map(event => ({at: event.at, text: safeLoopText(event.text, 280)}))};
            const needsArchive = state === "done" && loop.phase !== "plan";
            if (needsArchive) {
              if (!sourceArchives.has(id)) {
                const source = await api(`${prefix}/source-receipt`);
                const saved = source.data as {order_id?: string; venture_id?: string; state?: string} | null;
                if (!current()) return null;
                if (source.ok && saved?.order_id === id && saved.venture_id === ventureId && saved.state === "stored") {
                  sourceArchives.set(id, `${prefix}/source-archive`);
                }
              }
              build.exportHref = sourceArchives.get(id);
            }
            return remember({id: order.id, kind: "device.work_order", venture_id: ventureId, status: state, build,
              task_id: order.task_snapshot?.kind === "landing_page_build" ? order.task_snapshot.kind : order.manifest.task_id || order.id,
              created_at: summary?.created_at, workspace_id: workspace,
              failure_reason: state === "failed" ? safeLoopText(order.result_summary || order.iterations?.at(-1)?.summary || receipt?.message, 500) : undefined,
              label: safeLoopText(order.manifest.title), reviewHref: `/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(order.id)}`,
              started_at: order.claimed_at, progress: {state, percent: null, started_at: order.claimed_at || null,
                step_label: buildStageLabel(receipt?.loop_phase === "check" ? "checking" : watch.state, loop.provider, buildStatus === "stopping" ? "stopping" : state)}},
              stopSettled && nativeSettled && (!needsArchive || !!build.exportHref));
          }
          return { id: order.id, kind: "device.work_order", venture_id: ventureId, status, build: undefined,
            label: safeLoopText(order.manifest.title), reviewHref: `/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(order.id)}`, started_at: order.claimed_at,
            progress: { state: status, percent: null, started_at: order.claimed_at || null,
              step_label: safeLoopText(receipt?.founder_prompt || receipt?.events?.at(-1)?.text || receipt?.message) || deviceOrderStatus(order.state,
                { expired: Date.parse(order.expires_at) < Date.now(), claimedAt: order.claimed_at, receipt }) } };
        }));
        if (!current() || document.hidden) return;
        const confirmed = macJobs.flatMap(result => {
          if (result.status === "rejected") throw result.reason;
          return result.value ? [result.value] : [];
        });
        const linked = new Set(next.flatMap(job => job.kind === "pagebuilder.build" && job.build?.orderId ? [job.build.orderId] : []));
        next = updateAgentJobs(next, confirmed.filter(job => !linked.has(job.id))).map(job => {
          const mac = confirmed.find(item => item.id === (job.build?.orderId || job.id));
          if (job.build && mac?.build) return {...job, task_id: mac.task_id, created_at: mac.created_at || job.created_at,
            status: ["failed", "cancelled"].includes(mac.status) ? mac.status : job.status,
            reviewHref: mac.reviewHref, failure_reason: mac.failure_reason || job.failure_reason,
            build: {...mac.build, ...job.build, loop: mac.build.loop, watch: mac.build.watch,
            events: mac.build.events, files: mac.build.files, preview: mac.build.preview,
            previewHref: ["failed", "cancelled"].includes(mac.status) ? undefined : job.build.previewHref || mac.build.previewHref,
            exportHref: ["failed", "cancelled"].includes(mac.status) ? undefined : job.build.exportHref || mac.build.exportHref},
            progress: {...job.progress!, step_label: buildStageLabel(job.progress?.step_label || "", mac.build.loop?.provider,
              ["failed", "cancelled"].includes(mac.status) ? mac.status : jobFinished(job) ? job.status : "")}};
          return mac ? { ...job, label: mac.label, reviewHref: mac.reviewHref, build: mac.build } : job;
        });
        publish(next);
        await notify(next);
      } catch (cause) {
        if (current()) setSaved(previous => ({ scope, jobs: previous.scope === scope ? previous.jobs : [], now: Date.now(),
          error: safeLoopText(cause instanceof Error ? cause.message : "Your running work could not be loaded. Checking again shortly.") }));
      } finally { polling = false; }
    };
    refresh.current = () => void poll();
    if (isLocalConsole()) {
      const stop = watchActivity(activity, poll);
      return () => { live = false; stop(); };
    }
    let timer: number | undefined;
    const stopTimer = () => { if (timer !== undefined) window.clearInterval(timer); timer = undefined; };
    const onVisibility = () => {
      if (document.hidden) { stopTimer(); return; }
      timer ??= window.setInterval(poll, 2000);
      void poll();
    };
    onVisibility();
    window.addEventListener("focus", poll);
    document.addEventListener("visibilitychange", onVisibility);
    return () => { live = false; stopTimer(); window.removeEventListener("focus", poll); document.removeEventListener("visibilitychange", onVisibility); };
  }, [activity, ventureId, visible, scope, storageKey, dismissedStorageKey]);
  return { jobs: saved.scope === scope ? saved.jobs : [], error: saved.scope === scope ? saved.error : "", now: saved.now, refresh: () => refresh.current(),
    dismiss: (id: string) => {
      if (currentScope.current !== scope || !tracked.current.jobs.some(job => job.id === id && jobFinished(job))) return;
      const selected = tracked.current.jobs.find(job => job.id === id)!;
      const key = codingTaskKey(selected);
      tracked.current.jobs.filter(job => job.id === id || key && codingTaskKey(job) === key && jobFinished(job)
        && Date.parse(job.created_at || job.started_at || "") <= Date.parse(selected.created_at || selected.started_at || ""))
        .forEach(job => {dismissed.current.ids.add(job.id); if (job.build?.orderId) dismissed.current.ids.add(job.build.orderId);});
      tracked.current.jobs = tracked.current.jobs.filter(job => !dismissed.current.ids.has(job.id));
      setSaved(previous => ({ ...previous, jobs: previous.jobs.filter(job => !dismissed.current.ids.has(job.id)) }));
      try { (window.localStorage || window.sessionStorage).setItem(dismissedStorageKey, JSON.stringify([...dismissed.current.ids])); } catch {}
      try { window.sessionStorage.setItem(storageKey, JSON.stringify(tracked.current.jobs)); } catch {}
    } };
}

export function AgentsTray({ jobs, now, error, onDismiss, onAction, progress = [], showInline = false, refresh }: {
  jobs: AgentJob[]; now: number; error?: string; onDismiss: (id: string) => void;
  showInline?: boolean; refresh?: () => void;
  onAction?: (action: string, payload?: unknown) => void | boolean | Promise<void | boolean>;
  progress?: (ResearchProgress & { job_id: string })[];
}) {
  const attempts = latestCodingAttempts(jobs);
  const superseded = jobs.filter(job => jobFinished(job) && !jobSucceeded(job) && job.status !== "cancelled" && attempts.some(later =>
    later.venture_id === job.venture_id && later.workspace_id === job.workspace_id && later.kind === job.kind && jobSucceeded(later)
    && (!codingTaskKey(job) || codingTaskKey(job) === codingTaskKey(later))
    && Date.parse(later.created_at || "") > Date.parse(job.created_at || "")));
  const supersededIds = JSON.stringify(superseded.map(job => job.id));
  const attention = new Map(latestCodingAttempts(jobs.filter(job => jobFinished(job) && !jobSucceeded(job) && job.status !== "cancelled" && !superseded.includes(job)))
    .map(job => [codingTaskKey(job) || job.id, job.id]));
  jobs = superseded.length ? latestCodingAttempts(jobs.filter(job => !superseded.includes(job))) : attempts;
  const anchor = useRef<HTMLSpanElement>(null);
  const [header, setHeader] = useState<HTMLElement | null>(null);
  const [trayOpen, setTrayOpen] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);
  const retryFlight = useRef(false);
  const [retryError, setRetryError] = useState("");
  const [buildBusy, setBuildBusy] = useState<string | null>(null);
  const buildFlight = useRef(false);
  const [buildError, setBuildError] = useState("");
  const owner = JSON.stringify([verifiedSessionSubject(), verifiedSessionEpoch(), jobs[0]?.venture_id]);
  const buildOwner = useRef(owner);
  buildOwner.current = owner;
  const firstShown = useRef(new Map<string, number>());
  const populated = !!(jobs.length || error);
  useEffect(() => {
    const dock = anchor.current?.closest?.(`.${dockStyles.dock}`) || anchor.current?.parentElement;
    setHeader(dock?.querySelector<HTMLElement>(`:scope > .${dockStyles.header}`)
      || dock?.closest("#supymem-chat-panel")?.querySelector<HTMLElement>(":scope > div") || null);
  }, [populated]);
  useEffect(() => {
    for (const id of JSON.parse(supersededIds) as string[]) onDismiss(id);
  }, [supersededIds, onDismiss]);
  useEffect(() => {
    if (!header) return;
    const succeeded = jobs.filter(job => jobSucceeded(job) && !(showInline && (job.build || job.kind === "pagebuilder.build")));
    const ids = new Set(succeeded.map(job => job.id));
    for (const id of firstShown.current.keys()) if (!ids.has(id)) firstShown.current.delete(id);
    const timers = succeeded.map(job => {
      if (!firstShown.current.has(job.id)) firstShown.current.set(job.id, Date.now());
      return window.setTimeout(() => onDismiss(job.id), Math.max(0, 30_000 - (Date.now() - firstShown.current.get(job.id)!)));
    });
    return () => timers.forEach(timer => window.clearTimeout(timer));
  }, [jobs, header, onDismiss, showInline]);
  async function buildAction(job: AgentJob, action: "stop" | "folder") {
    if (!job.build?.orderId || buildFlight.current) return;
    const captured = owner;
    const current = () => buildOwner.current === captured && captured === JSON.stringify([verifiedSessionSubject(), verifiedSessionEpoch(), job.venture_id]);
    if (!current()) return;
    buildFlight.current = true; setBuildBusy(job.id); setBuildError("");
    try {
      if (action === "stop") {
        const receipt = await desktopInvokeOrThrow<MacReceipt>("device_work_order_action", {ventureId: job.venture_id, orderId: job.build.orderId,
          action: productBuildStatus(job.build.watch) === "stop_unconfirmed" ? "reconcile" : "cancel"});
        if (!current()) return;
        if (receipt?.order_id !== job.build.orderId || receipt.state !== "stopped" || receipt.evidence?.processes_stopped !== true)
          throw new Error("stop_unconfirmed");
      } else {
        const folder = await desktopInvokeOrThrow<string>("venture_workspace_path", {ventureId: job.venture_id, folderName: job.build.loop?.workspace_slug});
        if (!current()) return;
        if (typeof folder !== "string" || !folder) throw new Error("folder_unconfirmed");
        await desktopInvokeOrThrow("reveal_in_file_manager", {path: folder});
      }
    } catch {
      if (current()) setBuildError(action === "stop"
        ? "Your tool has not confirmed Stop. Choose Watch your tool below, then Refresh build and Check Stop or Stop again."
        : "Your folder could not be opened. Choose Watch your tool below, then Refresh build and Open result folder again.");
    } finally {
      buildFlight.current = false;
      if (current()) {setBuildBusy(null); refresh?.();}
    }
  }
  if (!jobs.length && !error && !showInline) return null;
  const groups = new Map<string, AgentJob[]>();
  jobs.forEach(job => {
    const label = trayLabel(job, now);
    const key = jobFinished(job) && !jobSucceeded(job) && job.status !== "cancelled"
      ? JSON.stringify([label, unexpectedJobFailure(job), trayText(job.failure_reason) || job.error_code]) : label;
    groups.set(key, [...(groups.get(key) || []), job]);
  });
  const statusJobs = jobs.filter(job => !attention.has(codingTaskKey(job) || job.id));
  const counts = [
    [statusJobs.filter(job => !jobFinished(job) && !jobWaitingLabel(job)).length, "running"],
    [statusJobs.filter(job => trayWaitStatus(job, now) === "Queued").length, "queued"],
    [statusJobs.filter(job => trayWaitStatus(job, now) === "Delayed").length, "delayed"],
    [statusJobs.filter(job => trayWaitStatus(job, now)?.startsWith("Scheduled for ")).length, "scheduled"],
    [statusJobs.filter(job => jobWaitingLabel(job) && job.wait?.reason === "cancel_requested").length, "stopping"],
    [statusJobs.filter(job => jobWaitingLabel(job) && job.wait?.reason === "paused").length, "paused"],
    [statusJobs.filter(jobSucceeded).length, "done"],
    [attention.size, "needs you"],
    [statusJobs.filter(job => job.status === "cancelled").length, "stopped"],
  ].filter(([count]) => count).map(([count, label]) => `${count} ${label}`).join(" · ");
  const tray = <aside className={styles.tray} data-agents-tray="" aria-label="Your tasks">
    <button type="button" className={styles.summary} aria-expanded={trayOpen} onClick={() => setTrayOpen(!trayOpen)}>
      <span className={styles.summaryText}>{counts || "Check your tasks"}{error ? " · Check status" : ""}</span><span aria-hidden="true">{trayOpen ? "▴" : "▾"}</span>
    </button>
    {trayOpen && <div className={styles.items} role="region" aria-label="Task progress" tabIndex={0}>
    <ErrorNotice error={trayText(error)} source="chat.agents.status" className={styles.error} />
    {[...groups.values()].map(group => {
      const job = group[0];
      const label = trayLabel(job, now);
      const [icon] = jobLabel(job);
      const done = jobSucceeded(job), stopped = job.status === "cancelled", failed = jobFinished(job) && !done && !stopped;
      const nextStep = job.kind === "idea.clarify_turn" ? "Continue in the chat to choose your next step." : "Review what stopped before trying again.";
      const reason = trayText(job.failure_reason) || "Your task did not finish.";
      const live = progress.find(item => item.job_id === job.id) || job.progress;
      const waiting = jobWaitingLabel(job);
      const start = waiting ? NaN : Date.parse(live?.started_at || job.started_at || job.created_at || "");
      const finishedAt = jobFinished(job) && job.finished_at ? Date.parse(job.finished_at) : now;
      const end = Number.isFinite(finishedAt) ? finishedAt : now;
      const seconds = Number.isFinite(start) ? Math.max(0, Math.floor((end - start) / 1000)) : null;
      const open = expanded === job.id;
      return <div key={job.id} className={`${styles.agent} ${done ? styles.done : unexpectedJobFailure(job) ? styles.failed : ""}`}>
        <button type="button" className={styles.chip} aria-expanded={open} onClick={() => setExpanded(open ? null : job.id)}>
          <span aria-hidden="true">{icon}</span>
          <span className={styles.copy}><strong>{label}</strong>
            <span>{job.build || job.kind === "pagebuilder.build" ? buildStageLabel(live?.step_label || "", job.build?.loop?.provider, job.status)
              : failed ? `${reason} ${nextStep}` : trayText(waiting ? delayedJobNotice(job, now) || waiting : live?.step_label) || (done ? "Ready to review" : stopped ? "Stopped safely" : "You can keep chatting while I work")}</span></span>
          {!jobFinished(job) && job.wait?.reason !== "paused" && <span className={styles.spinner} role="status" aria-label={waiting ? trayWaitStatus(job, now) || "Queued" : "Running"} />}
          {done && <span aria-label="Complete">✓</span>}
          {seconds !== null && <time aria-label="Elapsed time">{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}</time>}
        </button>
        {open && group.map(item => {
          const itemLive = progress.find(update => update.job_id === item.id) || item.progress;
          const stageCounts = (itemLive as ResearchProgress & { steps?: Record<string, { completed: number; planned: number }> } | null)?.steps;
          const stages = stageCounts && typeof stageCounts === "object" ? Object.entries(stageCounts)
            .filter(([, counts]) => counts && Number.isInteger(counts.completed) && Number.isInteger(counts.planned) && counts.planned > 0) : [];
          const stageLabel = (stage: string) => item.kind === "gtm.journey.prepare" ? searchStageLabels.get(stage) || "Search step" : trayText(stage);
          const nextStage = stages.find(([, counts]) => counts.completed < counts.planned);
          return <div key={item.id} className={styles.details}>
          {item.label && <strong>{trayText(item.label)}</strong>}
          <ol>{stages.length ? stages.map(([stage, counts]) => <li key={stage}>{stageLabel(stage)}: {counts.completed}/{counts.planned} steps processed</li>) :
            item.steps.map((step, index) => <li key={step}>{done ? "✓ " : jobFinished(item) || waiting || index < item.steps.length - 1 ? "Earlier: " : "Now: "}{item.build || item.kind === "pagebuilder.build" ? buildStageLabel(step, item.build?.loop?.provider) : trayText(step)}</li>)}</ol>
          {waiting && <p role="status">{trayText(delayedJobNotice(item, now) || jobWaitingLabel(item))}</p>}
          <p>{failed && `${reason} `}Next: {done ? "Review your result and choose your next move." : stopped ? "Review your saved progress. Nothing was restarted." : failed ? nextStep :
            (nextStage && stageLabel(nextStage[0])) || "I will post your result here when it is ready."}</p>
          {item.reviewHref && <ChatAnchor href={item.reviewHref}>Review your coding task</ChatAnchor>}
          {jobFinished(item) && <button type="button" onClick={() => onDismiss(item.id)}>Dismiss after review</button>}
          {!jobFinished(item) && attention.has(codingTaskKey(item) || item.id) && <button type="button"
            onClick={() => onDismiss(attention.get(codingTaskKey(item) || item.id)!)}>Dismiss earlier result</button>}
        </div>; })}
        {failed && !["idea.clarify_turn", "device.work_order"].includes(job.kind) && onAction && group.map(item => <button key={item.id} type="button" className={styles.retry} disabled={retrying === item.id} onClick={async () => {
          if (retryFlight.current) return;
          retryFlight.current = true;
          setRetrying(item.id); setRetryError("");
          try {
            const result = await onAction("job.retry", { kind: item.kind, job_id: item.id });
            if (result === false) throw new Error("Your retry was not confirmed. Check the chat and try again.");
          } catch { setRetryError("Your retry was not confirmed. Check the chat and try again."); }
          finally { retryFlight.current = false; setRetrying(null); }
        }}>{retrying === item.id ? "Checking retry…" : "Retry"}</button>)}
      </div>;
    })}
    <ErrorNotice error={retryError} source="chat.agents.retry" className={styles.error} />
    </div>}
  </aside>;
  return <><span ref={anchor} hidden />{header && createPortal(tray, header)}
    {showInline && <section aria-label="Your build activity" style={{background: "#fff", color: "#17202a", padding: 16, overflowWrap: "anywhere"}}>
      <ErrorNotice error={trayText(error || buildError)} source="chat.build_activity" />
      {jobs.filter(job => job.build || job.kind === "pagebuilder.build").map(job => {
        const retryJobId = pageAttemptId(job);
        return <div key={job.id}>
        {job.build?.loop ? <ProductBuildReview local loop={job.build.loop} watch={jobFinished(job)
          ? {...job.build.watch, state: jobSucceeded(job) ? "stopped" : job.status === "cancelled" ? "cancelled" : "failed",
            failure_code: jobSucceeded(job) ? job.build.watch?.failure_code : job.status === "cancelled" ? undefined : job.build.watch?.failure_code || "build_failed"} : job.build.watch} busy={buildBusy === job.id}
          inlineStage={buildStageLabel(job.progress?.step_label || "", job.build.loop.provider, job.status)}
          preview={job.build.preview} previewHref={job.build.previewHref} exportHref={job.build.exportHref} reviewHref={job.reviewHref}
          onStopBuild={isDesktopShell() && job.build.orderId ? () => buildAction(job, "stop") : undefined}
          onRevealFolder={isDesktopShell() && job.build.loop.workspace_slug ? () => buildAction(job, "folder") : undefined} />
          : <p role="status">{buildStageLabel(job.progress?.step_label || "", "", job.status)}. Keep this card open to watch your tool.</p>}
        {job.reviewHref && <ChatAnchor href={job.reviewHref}>Watch your tool</ChatAnchor>}
        {!job.build?.loop && job.build?.previewHref && jobSucceeded(job) && <PageResultLink href={job.build.previewHref} />}
        {!job.build?.loop && job.build?.exportHref && jobSucceeded(job) && <PageResultLink href={job.build.exportHref} />}
        {jobFinished(job) && !jobSucceeded(job) && onAction && retryJobId && <button type="button" disabled={retrying === job.id} onClick={async () => {
            if (retryFlight.current) return;
            retryFlight.current = true; setRetrying(job.id); setRetryError("");
            try {
              if (await onAction("pagebuilder.build", {retry_job_id: retryJobId}) === false) throw new Error("retry_unconfirmed");
            } catch { setRetryError(job.reviewHref ? "Your retry was not confirmed. Choose Watch your tool above to check its status before trying again."
              : "Your retry was not confirmed. Ask Charlie to show your landing page status before trying again."); }
            finally { retryFlight.current = false; setRetrying(null); }
          }}>Retry</button>}
        {!isDesktopShell() && job.reviewHref && <p>Choose Watch your tool above to review this build. To stop your tool or open its result folder, use this chat in your desktop app.</p>}
        {job.build?.loop && <LoopProgress maximum={job.build.loop.max_iterations} events={job.build.events?.slice(-5)} iterations={job.build.iterations} compact />}
        {jobFinished(job) && job.build?.loop?.phase !== "plan" && !job.build?.exportHref && <p>To export your saved work, choose Open folder and ask your coding tool to create a ZIP without keys or private settings.</p>}
      </div>;
      })}
      <ErrorNotice error={retryError} source="chat.agents.retry" />
      {!jobs.some(job => job.build || job.kind === "pagebuilder.build") && <p role="status">You have no build running here. Ask Charlie to build with your connected coding tool. <a href="/settings/local#coding-tools">Connect your coding tool in Keys and connections</a>.</p>}
    </section>}
  </>;
}
