"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import type { ChatMessage } from "@/lib/api-types";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { taskActionHref, taskClient, taskIsActive, taskStateLabel, type TaskView } from "@/lib/taskApi";
import { RUN_STATUS_REFRESH } from "@/lib/runStatusPresentation";
import { ChatAnchor } from "./ChatLink";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { planChatActions } from "./chatActionPolicy";
import chatStyles from "./ChatDock.module.css";
import styles from "./GoalCard.module.css";

function taskReference(message: ChatMessage): string | undefined {
  if (message.role !== "assistant" && message.role !== "system") return;
  const plan = planChatActions(message.actions || []);
  const ids = new Set<string>();
  for (const action of [...plan.confirmations, ...plan.visible, ...plan.overflow]) {
    const binding = action.confirmation?.resulting_state?._goal_step as Record<string, unknown> | undefined;
    if (binding && [binding.task_id, binding.run_id, binding.step_id].every(value => typeof value === "string" && value.trim())) {
      ids.add(binding.task_id as string);
    } else if (["task_handoff", "task", "handoff"].includes(message.kind || "")) {
      const id = action.task_id ?? action.payload?.task_id;
      if (typeof id === "string" && id.trim()) ids.add(id);
    }
  }
  return ids.size === 1 ? [...ids][0] : undefined;
}

function stepLabel(state: string): string {
  if (state === "completed") return "Done";
  if (["admitting", "working", "verifying"].includes(state)) return "Running";
  if (["approval_required", "input_required", "auth_required", "paused", "reconciliation_required"].includes(state)) return "Waiting for you";
  if (["failed", "rejected"].includes(state)) return "Failed";
  if (state === "canceled") return "Stopped";
  return state === "submitted" || state === "queued" ? "Not started" : "Checking progress";
}

export function GoalCardView({ task, ventureId, loading, error, busy, disabled, children, onStop, onRefresh }: {
  task: TaskView | null; ventureId: string; loading: boolean; error: string; busy: boolean; disabled?: boolean;
  children?: ReactNode; onStop: () => void; onRefresh: () => void;
}) {
  const actionHref = taskActionHref(task?.action?.href, ventureId);
  // shortcut: ended goals cannot resume, replace the review link when an exact safe retry is available.
  const reviewHref = actionHref || `/v/${encodeURIComponent(ventureId)}/tasks`;
  const failed = task?.state === "failed" || task?.state === "rejected" || task?.steps.some(step => ["failed", "rejected"].includes(step.state));
  return <section className={styles.card} aria-label="Your goal progress" aria-busy={loading || busy}>
    <div className={styles.heading}>
      <strong>{task?.title || "Your goal"}</strong>
      {task && <span className="chip">{taskStateLabel(task.state)}</span>}
    </div>
    {loading && !task && <p>Loading your progress…</p>}
    {task && <>
      <p>{task.steps.filter(step => step.state === "completed").length} of {task.steps.length} steps done.</p>
      {task.steps.length ? <details open={task.steps.length <= 2 ? true : undefined}>
        <summary>Your steps</summary>
        <ol className={styles.steps}>
          {task.steps.map(step => <li key={step.id}>
            <span aria-hidden="true">{step.state === "completed" ? "✓" : ["failed", "rejected"].includes(step.state) ? "!" : "○"}</span>
            <span>{step.title}</span><span className="chip">{stepLabel(step.state)}</span>
          </li>)}
        </ol>
      </details> : <p>Your steps will appear when your plan is ready.</p>}
      {task.state === "approval_required" && <p>Review each action before you start it. Nothing is approved together.</p>}
      {task.state === "auth_required" && <p>Connect your account, then review your next step.</p>}
      {task.state === "input_required" && <p>Your next step needs your input. Review what it needs to continue.</p>}
      {task.state === "canceled" && <p>Your goal is stopped. Your saved results are kept. Work already in progress may finish.</p>}
    </>}
    {children && <div role="group" aria-label="Actions for you to review" className={styles.approvals}>{children}</div>}
    <ErrorNotice error={error} source="goals.progress" />
    <div className={styles.actions}>
      {failed && <ChatAnchor href={reviewHref} className={`${chatStyles.actionLink} ${chatStyles.actionGhost}`}>Try again</ChatAnchor>}
      {failed && <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}?chat=1`} className={`${chatStyles.actionLink} ${chatStyles.actionGhost}`}>Do it another way</ChatAnchor>}
      {task?.state === "auth_required" && <ChatAnchor href={reviewHref} className={`${chatStyles.actionLink} ${chatStyles.actionGhost}`}>Connect your account</ChatAnchor>}
      {task?.state === "input_required" && <ChatAnchor href={reviewHref} className={`${chatStyles.actionLink} ${chatStyles.actionGhost}`}>Review your next step</ChatAnchor>}
      {error && <button type="button" disabled={busy} onClick={onRefresh} className={`${chatStyles.actionLink} ${chatStyles.actionGhost}`}>Refresh your progress</button>}
      {task && taskIsActive(task) && <button type="button" data-usage-control="task_handoff_stop" disabled={disabled || busy || !!error || loading} onClick={onStop}
        className={`${chatStyles.actionLink} ${chatStyles.actionGhost}`}>{busy ? "Stopping…" : "Stop"}</button>}
    </div>
  </section>;
}

type Saved = { scope: string; task: TaskView | null; loading: boolean; error: string };

export function GoalCard({ ventureId, message, messages, disabled, children }: {
  ventureId?: string; message: ChatMessage; messages?: ChatMessage[]; disabled?: boolean; children?: ReactNode;
}) {
  const reference = taskReference(message);
  const taskId = reference && (!messages || messages.filter(item => taskReference(item) === reference).at(-1)?.id === message.id) ? reference : undefined;
  const scope = JSON.stringify([verifiedSessionSubject(), verifiedSessionEpoch(), ventureId, taskId]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const [saved, setSaved] = useState<Saved | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const flight = useRef<string | null>(null);
  const current = () => currentScope.current === scope && !!verifiedSessionSubject()
    && JSON.stringify([verifiedSessionSubject(), verifiedSessionEpoch(), ventureId, taskId]) === scope;
  useEffect(() => {
    if (!ventureId || !taskId) return;
    let cancelled = false, timer: ReturnType<typeof setTimeout> | undefined;
    const live = () => !cancelled && current();
    const changed = () => { cancelled = true; clearTimeout(timer); setSaved(null); setRefresh(value => value + 1); };
    window.addEventListener("supymem-session-changed", changed);
    async function load() {
      let enabled = false;
      try {
        await ensureSession();
        if (!live()) return;
        const config = await api("/api/ui-config");
        if (!live()) return;
        enabled = config.ok && (config.data as { goals_v1?: unknown } | null)?.goals_v1 === true;
        if (!enabled) {
          setSaved(null);
          return;
        }
        setSaved(previous => ({ scope, task: previous?.scope === scope ? previous.task : null, loading: true, error: "" }));
        const tasks = await taskClient.list(ventureId!);
        if (!live()) return;
        const task = tasks.find(item => item.id === taskId) || null;
        setSaved({ scope, task, loading: false, error: task ? "" : "Your saved goal could not be found. Refresh your progress." });
      } catch {
        if (live()) setSaved(previous => enabled && previous?.scope === scope ? { ...previous, loading: false, error: "Your progress could not load. Try again." } : null);
      } finally {
        if (live()) timer = setTimeout(() => void load(), 5000);
      }
    }
    void load();
    return () => { cancelled = true; clearTimeout(timer); window.removeEventListener("supymem-session-changed", changed); };
  }, [scope, refresh]);
  async function stop() {
    const task = saved?.scope === scope ? saved.task : null;
    if (!task || !current() || disabled || saved?.loading || saved?.error || flight.current || !taskIsActive(task)) return;
    flight.current = scope;
    setBusy(scope);
    try {
      const config = await api("/api/ui-config");
      if (!current()) return;
      if (!config.ok || (config.data as { goals_v1?: unknown } | null)?.goals_v1 !== true) { setSaved(null); return; }
      const updated = await taskClient.act(ventureId!, task.id, "cancel");
      if (!current()) return;
      setSaved({ scope, task: updated, loading: false, error: "" });
      window.dispatchEvent(new Event(RUN_STATUS_REFRESH));
    } catch {
      if (current()) setSaved(previous => previous?.scope === scope ? { ...previous, error: "Your stop could not be confirmed. Refresh your progress before trying again." } : previous);
    } finally {
      if (flight.current === scope) { flight.current = null; setBusy(null); }
    }
  }
  if (!ventureId || !taskId || saved?.scope !== scope) return <>{children}</>;
  return <GoalCardView task={saved.task} ventureId={ventureId} loading={saved.loading} error={saved.error} busy={busy === scope} disabled={disabled}
    onStop={() => void stop()} onRefresh={() => setRefresh(value => value + 1)}>{children}</GoalCardView>;
}
