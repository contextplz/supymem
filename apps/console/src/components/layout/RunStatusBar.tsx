"use client";

import { ChatAnchor } from "@/components/chat/ChatLink";

import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, apiSwr, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { dedupeStatusCta } from "@/lib/nextStep";
import { cacheGet } from "@/lib/apiCache";
import { RUN_STATUS_REFRESH, type IdeaPageStatus } from "@/lib/runStatusPresentation";
import { isPreparingFirstQuestion, runStatusPresentation, statusBarAction } from "@/lib/runStatusPresentation";
import { EmergencyStopButton } from "@/components/desktop/EmergencyStopButton";
import { estimatedDeadline, taskClient } from "@/lib/taskApi";
import styles from "./RunStatusBar.module.css";
import type { ResearchProgress as Progress } from "@/lib/durableJobs";
import { ResearchProgress } from "@/components/primitives/ResearchProgress";
import { gtmProgressOwner, shareGtmProgress, subscribeGtmProgress } from "@/lib/gtmProgress";

type Attention = { id: string; title: string; why?: string; href?: string; action_label?: string; steps?: { text?: string }[] };
type Snapshot = {
  venture: { id: string; name: string };
  run_status?: { state: string; stage: string; activity: string; research_progress?: Progress | null; next_step?: string; changed_at?: string; deadline_at?: string | null; continues_when_closed?: boolean | null; background_work?: { job_id: string; stage: string; state: string; href: string }[]; action?: { label: string; href: string } | null; secondary_action?: { label: string; href: string }; landing_page?: { live_url: string | null } };
  next_step?: { key?: string; cta_label?: string; href?: string; title?: string; stage?: string };
  needs_you?: Attention[];
  tiles?: { cost?: { credits?: { available: number; reserved: number; spent: number } } };
};

const dashboardPath = (id: string) => `/api/ventures/${encodeURIComponent(id)}/dashboard`;
const cachedDashboard = (id?: string) => (id ? (cacheGet(verifiedSessionSubject(), dashboardPath(id))?.data as Snapshot | undefined) ?? null : null);

function shortActivity(activity = "") {
  const sentence = activity.split(/(?<=[.!?])\s+/, 1)[0];
  return sentence.length <= 120 ? sentence : `${sentence.match(/^.{1,119}(?=\s)/u)?.[0] || ""}…`;
}

export function RunStatusBar({ ventureId, ideaStatus }: { ventureId?: string; ideaStatus?: IdeaPageStatus }) {
  const [data, setData] = useState<Snapshot | null>(() => cachedDashboard(ventureId));
  const [offline, setOffline] = useState(false);
  const [revision, setRevision] = useState(0);
  const [opened, setOpened] = useState<string | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [workflowHelp, setWorkflowHelp] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [copied, setCopied] = useState(false);
  const [slow, setSlow] = useState(false);
  const [god, setGod] = useState(false);
  const [timezone, setTimezone] = useState<{ venture: string; subject: string; epoch: number; value: string } | null>(null);
  useEffect(() => {
    if (!ventureId) { setGod(false); return; }
    let stopped = false;
    const read = async () => {
      const r = await api(`/api/ventures/${encodeURIComponent(ventureId)}/autopilot`);
      if (!stopped && r.ok) setGod(Boolean((r.data as { god_mode?: boolean }).god_mode));
    };
    const onChange = (e: Event) => setGod(Boolean((e as CustomEvent).detail));
    void read();
    const timer = setInterval(() => { if (!document.hidden) void read(); }, 30000);
    window.addEventListener("supymem-god-mode", onChange);
    return () => { stopped = true; clearInterval(timer); window.removeEventListener("supymem-god-mode", onChange); };
  }, [ventureId]);
  useEffect(() => {
    setSlow(false);
    const slowTimer = ventureId && !cachedDashboard(ventureId) ? setTimeout(() => setSlow(true), 10000) : undefined;
    setData(cachedDashboard(ventureId)); setTimezone(null); setOffline(false); setOpened(null); setDetailsOpen(false); setWorkflowHelp(false); setNote(""); setCopied(false);
    if (!ventureId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let pending = false;
    async function refresh() {
      if (stopped || pending) return;
      clearTimeout(timer);
      pending = true;
      try {
        await ensureSession();
        const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
        const currentOwner = () => !stopped && subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
        // Cached copy paints at once via onData; the refresh replaces it.
        await apiSwr(dashboardPath(ventureId!), (response) => {
          if (!currentOwner()) return;
          const value = response.data as Snapshot | null;
          if (!response.ok || value?.venture?.id !== ventureId) setOffline(true);
          else { setData(value); setOffline(false); }
        });
        if (subject && currentOwner()) {
          try {
            const preferences = await taskClient.preferences(ventureId!);
            if (currentOwner()) setTimezone({ venture: ventureId!, subject, epoch, value: preferences.timezone });
          } catch { if (currentOwner()) setTimezone(null); }
        }
      } catch { if (!stopped) setOffline(true); }
      finally { pending = false; if (!stopped) timer = setTimeout(refresh, 8000); }
    }
    void refresh();
    const wake = () => { if (!document.hidden) void refresh(); };
    const resetOwner = () => { setData(cachedDashboard(ventureId)); setTimezone(null); setOpened(null); setNote(""); void refresh(); };
    window.addEventListener("supymem-session-changed", resetOwner);
    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wake);
    window.addEventListener(RUN_STATUS_REFRESH, wake);
    return () => { stopped = true; window.removeEventListener("supymem-session-changed", resetOwner); window.removeEventListener(RUN_STATUS_REFRESH, wake); clearTimeout(timer); clearTimeout(slowTimer); window.removeEventListener("online", wake); document.removeEventListener("visibilitychange", wake); };
  }, [ventureId, revision]);
  const current = data?.venture.id === ventureId ? data : null;
  const pathname = usePathname();
  const [gtmProgress, setGtmProgress] = useState<Progress | null>(null);
  const progressOwner = gtmProgressOwner(verifiedSessionSubject(), verifiedSessionEpoch(), ventureId || "");
  useEffect(() => {
    setGtmProgress(null);
    return subscribeGtmProgress(progressOwner, setGtmProgress);
  }, [progressOwner]);
  useEffect(() => {
    const progress = data?.run_status?.research_progress;
    if (data?.venture.id === ventureId && progress?.run_id) shareGtmProgress(progressOwner, progress);
  }, [progressOwner, data, ventureId]);
  const pageStatus = pathname.endsWith("/idea") || pathname === "/new" ? ideaStatus : undefined;
  const run = pageStatus ? { ...current?.run_status, ...pageStatus, next_step: pageStatus.activity, action: null } : current?.run_status;
  const savedTimezone = timezone && timezone.venture === ventureId && timezone.subject === verifiedSessionSubject()
    && timezone.epoch === verifiedSessionEpoch() ? timezone.value : "UTC";
  const tasks = current?.needs_you || [];
  const selected = tasks.find(task => task.id === opened);
  const count = tasks.length;
  const step = pageStatus ? undefined : current?.next_step;
  const preparing = isPreparingFirstQuestion(run, count, step?.key, step?.title);
  const presentation = runStatusPresentation(run, count, step?.key, step?.title);
  const needsAction = !preparing && (run?.state === "needs_you" || tasks.length > 0);
  const state = offline ? "Connection interrupted" : presentation.state || (ventureId && !slow ? "Syncing" : "Ready");
  const cta = preparing ? null : dedupeStatusCta(statusBarAction(run, step, presentation.historical), step, run?.state, pathname);
  const working =!offline && !needsAction && (preparing || run?.state === "working" || run?.state === "queued" || (!!ventureId && !current && !pageStatus && !slow));
  const progress = pathname === "/gtm" || run?.research_progress?.run_id ? gtmProgress || run?.research_progress : run?.research_progress;
  if (!ventureId && !offline) return null;
  return <div className={styles.region}>
    <div className={styles.barViewport}><section className={styles.bar} data-state={offline ? "offline" : needsAction ? "needs_you" : presentation.historical ? "idle" : run?.state || "idle"} data-working={working} data-research-progress={Boolean(progress)} aria-label="Run status">
      <span className={`${styles.stateIcon} ${working ? styles.working : ""}`} aria-hidden="true">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          {working ? <path d="M8 2v12M2 8h12M4 4l8 8M12 4l-8 8" /> : offline || needsAction || run?.state === "failed" ? <><path d="M8 3v6" /><circle cx="8" cy="12" r="0.8" fill="currentColor" stroke="none" /></> : run?.state === "completed" ? <path d="m3 8 3 3 7-7" /> : <circle cx="8" cy="8" r="3" fill="currentColor" stroke="none" />}
        </svg>
      </span>
      <div className={styles.activity} role="status" aria-live="polite">
        <strong>{current?.venture.name || (ventureId ? "Your venture" : "Supymem")} <span>· {state}</span>{god && <> <ChatAnchor href={`/v/${encodeURIComponent(ventureId || "")}/settings#god-mode`} title="God mode (beta) is enabled; recorded jobs below show current work">· God mode on</ChatAnchor></>}</strong>
        <p>{offline ? "Reconnecting to saved work…" : shortActivity(presentation.activity) || (ventureId ? (slow ? "Status is slow to load. Your saved work is safe." : "Reading saved work…") : "Choose a venture to begin.")}</p>
        {!offline && <ResearchProgress progress={progress} />}
        {!pageStatus && run?.deadline_at && <p>Deadline estimate: {estimatedDeadline(run.deadline_at, savedTimezone)}.</p>}
        {!pageStatus && !offline && Boolean(run?.background_work?.length) && <p aria-label="Background work">{run!.background_work!.length} background task{run!.background_work!.length === 1 ? "" : "s"} in progress.</p>}
      </div>
      <div className={`${styles.actions} ${styles.barActions}`}>
        <EmergencyStopButton />
        {offline ? <button type="button" className={styles.primary} data-usage-control="run_status_reconnect" onClick={() => setRevision(n => n + 1)}>Reconnect</button>
          : pageStatus?.action ? <button type="button" className={styles.primary} data-usage-control="run_status_action" disabled={pageStatus.action.disabled} onClick={pageStatus.action.onClick}>{pageStatus.action.label}</button>
          : tasks[0] ? <button type="button" className={styles.primary} data-usage-control="run_status_attention" onClick={() => { setOpened(tasks[0].id); setNote(""); }}>Review {count} action{count === 1 ? "" : "s"}</button>
          : cta ? <ChatAnchor className={styles.primary} data-usage-control="run_status_action" href={cta.href}
            target={cta.label === "Visit landing page" ? "_blank" : undefined} rel={cta.label === "Visit landing page" ? "noopener noreferrer" : undefined}>{cta.label}</ChatAnchor> : null}
        {!offline && run?.landing_page?.live_url && <button type="button" data-usage-control="website_copy_url" onClick={async () => {
          const owner = verifiedSessionSubject(), epoch = verifiedSessionEpoch(), url = run.landing_page!.live_url!;
          try { await navigator.clipboard.writeText(url); if (owner === verifiedSessionSubject() && epoch === verifiedSessionEpoch()) setCopied(true); }
          catch { setCopied(false); }
        }}>{copied ? "Copied" : "Copy link"}</button>}
        {!offline && run?.state === "ready" && ventureId && <>
          <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/site?build=1`}>Build a new version</ChatAnchor>
          <ChatAnchor href={`/gtm?venture=${encodeURIComponent(ventureId)}`}>Find customers</ChatAnchor>
          <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/site#site-preview`}>Open full preview</ChatAnchor>
        </>}
        {!offline && run?.secondary_action && <ChatAnchor href={run.secondary_action.href}>{run.secondary_action.label}</ChatAnchor>}
        <button type="button" className={styles.detailButton} data-usage-control="run_status_details" aria-expanded={detailsOpen} aria-controls="run-status-details" onClick={() => setDetailsOpen(value => !value)}>{detailsOpen ? "Hide details" : "Details"}</button>
      </div>
    </section></div>
    {detailsOpen && <section id="run-status-details" className={styles.attention} aria-label="Full run status">
      <h2>{current?.venture.name || "Supymem"}: {state}</h2>
      <p><b>Now: </b>{offline ? "Connection interrupted. Saved status may be out of date." : presentation.activity || (ventureId ? (slow ? "Status is slow to load. Your saved work is safe." : "Reading saved work…") : "Choose a venture to inspect its work.")}</p>
      <p><b>Next: </b>{presentation.nextStep || "Review the saved work for an available action."}</p>
      {run?.deadline_at && <p><b>Deadline estimate: </b>{estimatedDeadline(run.deadline_at, savedTimezone)}.</p>}
      {presentation.historical && run && <p><b>Last result: </b>{run.activity}{run.action && (!pathname.endsWith("/now") || run.action.href.split(/[?#]/)[0] !== pathname) && <> <ChatAnchor data-usage-control="run_status_action" href={run.action.href}>{run.action.label}</ChatAnchor></>}</p>}
      <p>{offline ? "Action and approval status is unverified while disconnected." : needsAction ? "Your input or review is required. Open the action for its exact scope." : run?.state === "failed" ? "This attempt stopped. Use the next action to recover; no approval is waiting." : current ? "No action request is recorded in this snapshot." : ventureId ? "Checking action requests…" : "Choose a venture to check its action requests."}</p>
      {!offline && (needsAction || run?.state === "failed") && run?.action && <ChatAnchor data-usage-control="run_status_action" href={run.action.href}>{run.action.label}</ChatAnchor>}
      {run?.changed_at && <p>Updated {new Date(run.changed_at).toLocaleString()}{run.continues_when_closed === true && ". Work continues with the app closed."}</p>}
      {current && <button type="button" data-usage-control="run_status_alternative" disabled={offline} onClick={() => { setDetailsOpen(false); if (tasks[0]) { setOpened(tasks[0].id); setNote("Review the saved task below, then request an alternative."); } else { setWorkflowHelp(true); } }}>Explore an alternative</button>}
      <button type="button" onClick={() => setDetailsOpen(false)}>Close details</button>
    </section>}
    {workflowHelp && !selected && <section className={styles.attention} aria-label="Workflow alternatives">
      <h2>You can review or change the approach</h2>
      <p>Your saved work remains available. Choose what to reconsider before approving more work.</p>
      {ventureId && <div className={styles.actions}>
        <ChatAnchor className={styles.primary} href={`/v/${encodeURIComponent(ventureId)}/idea`}>Revisit the idea and scope</ChatAnchor>
        <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/research`}>Revisit the research evidence</ChatAnchor>
        <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/plan#plan-resources`}>Update available resources</ChatAnchor>
        {run?.action && (!pathname.endsWith("/now") || run.action.href.split(/[?#]/)[0] !== pathname) && <ChatAnchor href={run.action.href}>{run.action.label}</ChatAnchor>}
      </div>}
      <button type="button" onClick={() => setWorkflowHelp(false)}>Close</button>
    </section>}
    {selected && <section className={styles.attention} aria-label="Human attention">
      <div className={styles.actions}>{tasks.map(task => <button key={task.id} type="button" aria-pressed={task.id === selected.id} onClick={() => { setOpened(task.id); setNote(""); }}>{task.title}</button>)}</div>
      <h2>{selected.title}</h2><p>{selected.why}</p>
      <ol>{selected.steps?.map((step, index) => <li key={index}>{step.text}</li>)}</ol>
      <p>This dependency stays open until its required action is completed. Requesting an alternative does not approve spending or external communication.</p>
      <div className={styles.actions}>
        {selected.href && <ChatAnchor className={styles.primary} href={selected.href}>{selected.action_label || "Open required action"}</ChatAnchor>}
        <button type="button" disabled={busy || offline} onClick={async () => {
          setBusy(true); setNote("");
          const id = selected.id;
          try {
            const result = await api(`/api/ventures/${encodeURIComponent(ventureId!)}/human-tasks/${encodeURIComponent(id)}/alternative`, { method: "POST", body: JSON.stringify({ reason: "Requested from the persistent run-status bar" }) });
            if (!result.ok) { setNote("Could not save the alternative. The original request remains open."); return; }
            const changed = (result.data as { task: Attention }).task;
            setData(previous => previous && previous.venture.id === ventureId ? { ...previous, needs_you: previous.needs_you?.map(task => task.id === id ? changed : task) } : previous);
            setNote("Alternative saved. The original dependency has not been marked complete.");
          } catch { setNote("Connection interrupted. Review saved task state before trying again."); }
          finally { setBusy(false); }
        }}>{busy ? "Saving alternative…" : "Request an alternative"}</button>
        <button type="button" onClick={() => setOpened(null)}>Close</button>
      </div><p role="status">{note}</p>
    </section>}
  </div>;
}
