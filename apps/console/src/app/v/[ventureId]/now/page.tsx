"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { apiSwr, ensureSession, verifiedSessionSubject, verifiedSessionEpoch } from "@/lib/client";
import { displayName } from "@/lib/format";
import { prettyName, prettyRole } from "@/components/office/avatars";
import { TaskBrief } from "@/components/tasks/TaskBrief";
import { WorkingForYou } from "@/components/tasks/WorkingForYou";
import { currentBackgroundWork, stoppedRun, type BackgroundWork } from "@/lib/nowProgress";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "../tasks/tasks.module.css";

const QUESTION = "What is happening right now?";
type Task = { id: string; detail?: string | null; title?: string; why_now?: string; owner_name?: string | null; owner_role?: string | null };
type Progress = { venture: string; owner: string | null; epoch: number; jobs: BackgroundWork[]; running: Task[]; stopped?: ReturnType<typeof stoppedRun> };

export default function NowPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const [title, setTitle] = useState("Current progress");
  const [stage, setStage] = useState<string>();
  const [status, setStatus] = useState<{ code?: string; label?: string; detail?: string } | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [handoffCount, setHandoffCount] = useState<number | null>(null);
  const sequence = useRef(0);

  const load = useCallback(async () => {
    const request = ++sequence.current;
    await ensureSession();
    const owner = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const current = () => request === sequence.current && !!owner
      && owner === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
    const scope = { venture: ventureId, owner, epoch };
    await Promise.all([
      apiSwr(`/api/ventures/${ventureId}`, response => {
        if (!current() || !response.ok) return;
        const venture = response.data as { name?: string; raw_idea?: string; stage?: string; status?: typeof status };
        setTitle(displayName(venture.name, venture.raw_idea));
        setStage(venture.stage); setStatus(venture.status || null);
      }),
      apiSwr(`/api/ventures/${ventureId}/dashboard`, response => {
        if (!current()) return;
        const jobs = response.ok ? currentBackgroundWork(response.data, ventureId) : null;
        setBusy(false);
        if (jobs === null) { setError("Current progress could not be confirmed. Refresh to check the saved job."); return; }
        setError("");
        setProgress(previous => ({ ...scope, jobs, stopped: stoppedRun(response.data), running: previous?.venture === ventureId
          && previous.owner === owner && previous.epoch === epoch ? previous.running : [] }));
      }),
      apiSwr(`/api/ventures/${ventureId}/tasks`, response => {
        if (!current() || !response.ok) return;
        const payload = response.data as { running?: Task[] };
        setProgress(previous => ({ ...scope, stopped: previous?.stopped, jobs: previous?.venture === ventureId
          && previous.owner === owner && previous.epoch === epoch ? previous.jobs : [],
          running: Array.isArray(payload.running) ? payload.running : [] }));
      }),
    ]).catch(() => { if (current()) { setBusy(false); setError("Connection interrupted. Current progress is unverified."); } });
  }, [ventureId]);

  useEffect(() => {
    const reset = () => { ++sequence.current; setProgress(null); setBusy(true); setError(""); void load(); };
    reset();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 8000);
    window.addEventListener("supymem-session-changed", reset);
    return () => { ++sequence.current; window.clearInterval(timer); window.removeEventListener("supymem-session-changed", reset); };
  }, [load]);

  const saved = progress?.venture === ventureId && progress.owner === verifiedSessionSubject()
    && progress.epoch === verifiedSessionEpoch() ? progress : null;
  return <AppShell title={title} subtitle={QUESTION} ventureId={ventureId} stage={stage} status={status}>
    <div className={styles.page}>
      <h1 className={styles.question}>{QUESTION}</h1>
      <p className={styles.lede}>Current jobs and their saved status update here automatically.</p>
      <WorkingForYou ventureId={ventureId} onActiveChange={setHandoffCount} />
      {busy && !saved && <p role="status">Checking current progress…</p>}
      <ErrorNotice error={error} source="now.progress" className={styles.fail} />
      {error && <button type="button" onClick={() => void load()}>Refresh progress</button>}
      {!error && saved && saved.jobs.length > 0 && <div className={styles.list}>
        {saved.jobs.map(job => <article key={job.job_id} className={styles.card} aria-label={job.stage}>
          <p className={styles.owner}>{job.state === "working" ? "Running now" : "Waiting to start"}</p>
          <h2>{job.activity}</h2>
          <p>{job.owner_name ? `${prettyName(job.owner_name)}${job.owner_role ? ` · ${prettyRole(job.owner_role)}` : ""}` : "Supymem background service"}</p>
          <p role="status">{job.state === "working" ? "Please wait while this work finishes. The result will appear when it is ready."
            : "This job is queued. You do not need to start it again."}</p>
          {job.changed_at && <p className={styles.owner}>Status saved {new Date(job.changed_at).toLocaleString()}</p>}
        </article>)}
      </div>}
      {!error && saved && saved.jobs.length === 0 && saved.running.length > 0 && <div className={styles.list}>
        {saved.running.map(task => <article key={task.id} className={styles.card}>
          <p className={styles.owner}>Running now</p>
          <h2><TaskBrief ventureId={ventureId} taskId={task.id} title={task.title || "Current company task"} detail={task.detail} /></h2>
          <p>{task.owner_name ? `${prettyName(task.owner_name)}${task.owner_role ? ` · ${prettyRole(task.owner_role)}` : ""}` : "Supymem"}</p>
          <p>Please wait. The saved result will appear when this task finishes.</p>
          {task.why_now && <p>{task.why_now}</p>}
        </article>)}
      </div>}
      {!error && saved && saved.jobs.length === 0 && saved.stopped && <div className={styles.empty} role="status">
        <h2>{saved.stopped.state === "paused" ? "Paused" : saved.stopped.state === "failed" ? "Needs attention" : "Stopped"} · {saved.stopped.stage}</h2>
        <p>{saved.stopped.activity}</p>
        {saved.stopped.action && <Link data-usage-control="run_status_action" href={saved.stopped.action.href}>{saved.stopped.action.label}</Link>}
      </div>}
      {!busy && !error && saved && saved.jobs.length === 0 && saved.running.length === 0 && !saved.stopped && handoffCount === 0 && <div className={styles.empty}>
        <h2>No active job is recorded</h2>
        <p>Your saved conversation and work remain available. Open the conversation to review the next required action.</p>
        <Link href={`/v/${ventureId}?chat=1`}>Open conversation</Link>
      </div>}
      <p><Link href={`/v/${ventureId}/tasks`}>Open the work queue</Link></p>
    </div>
  </AppShell>;
}
