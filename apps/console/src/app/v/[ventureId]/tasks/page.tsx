"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { NextStepStrip } from "@/components/layout/NextStepStrip";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { api, apiSwr, ensureSession } from "@/lib/client";
import { reportUserError } from "@/lib/userError";
import { displayName } from "@/lib/format";
import { prettyName, prettyRole } from "@/components/office/avatars";
import { taskRunFailure } from "@/lib/taskRunFailure";
import { TaskBrief } from "@/components/tasks/TaskBrief";
import { WorkingForYou } from "@/components/tasks/WorkingForYou";
import { estimatedDeadline } from "@/lib/taskApi";
import { buildPlanTaskId, buildWorkflowHref, planTaskId } from "@/lib/taskWorkflow";
import styles from "./tasks.module.css";

const QUESTION = "What work is next, who owns it, and why?";

type Task = {
  id: string;
  title?: string;
  why_now?: string;
  detail?: string | null;
  est_minutes?: number | null;
  due_at?: string | null;
  tags?: string[];
  runnable?: boolean;
  status?: string;
  rank?: number;
  owner_id?: string | null;
  owner_name?: string | null;
  owner_role?: string | null;
  source?: string;
  plan_task_state?: string;
  plan_scope?: string;
  plan_scope_reason?: string | null;
  build_review_ready?: boolean;
  public_site_evidence?: { source: string; id: string; url: string; observed_at?: string | null };
};

function errorMessage(data: unknown, fallback: string): string {
  if (!data || typeof data !== "object") return fallback;
  const root = data as {
    error?: { message?: string; code?: string } | string;
    detail?: { error?: { message?: string; code?: string }; message?: string } | string;
    message?: string;
  };
  if (typeof root.error === "object" && root.error?.message) return root.error.message;
  if (typeof root.error === "string" && root.error) return root.error;
  if (typeof root.detail === "object") {
    if (root.detail.error?.message) return root.detail.error.message;
    if (root.detail.message) return root.detail.message;
  }
  if (typeof root.detail === "string" && root.detail) return root.detail;
  if (root.message) return root.message;
  return fallback;
}

function errorCode(data: unknown): string {
  if (!data || typeof data !== "object") return "";
  const root = data as {
    error?: { code?: string };
    detail?: { error?: { code?: string }; code?: string };
  };
  return root.error?.code || root.detail?.error?.code || root.detail?.code || "";
}

export default function TasksPage() {
  const params = useParams();
  const router = useRouter();
  const ventureId = String(params.ventureId || "");
  const [title, setTitle] = useState("Work");
  const [stage, setStage] = useState<string | undefined>();
  const [status, setStatus] = useState<{ code?: string; label?: string; detail?: string } | null>(
    null,
  );
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [running, setRunning] = useState<Task[]>([]);
  const [replanAvailable, setReplanAvailable] = useState(false);
  const [emptyAction, setEmptyAction] = useState<{ label: string; href: string } | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [actionNote, setActionNote] = useState("");
  const [actionFail, setActionFailState] = useState("");
  // The red inline text under the queue: reported with the action the founder took (Run, Dismiss, Replan).
  const setActionFail = (message: string, action = "") => {
    setActionFailState(message);
    if (message) reportUserError({ message, source: `tasks.${action}`, intent: `task_${action}` });
  };
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [replanning, setReplanning] = useState(false);

  const load = useCallback(async () => {
    await ensureSession();
    // Company and queue load in parallel; cached copies paint immediately and refresh in place.
    await Promise.all([
      apiSwr(`/api/ventures/${ventureId}`, (ven) => {
        if (!ven.ok) return;
        const v = ven.data as {
          name?: string;
          raw_idea?: string;
          stage?: string;
          status?: { code?: string; label?: string; detail?: string };
        };
        setTitle(displayName(v.name, v.raw_idea));
        setStage(v.stage);
        setStatus(v.status || null);
      }),
      apiSwr(`/api/ventures/${ventureId}/tasks`, (r) => {
        setBusy(false);
        if (!r.ok) {
          setError(errorMessage(r.data, "Couldn’t load the work queue."));
          return;
        }
        setError("");
        const payload = r.data as { items?: Task[]; running?: Task[]; replan_available?: boolean; empty_action?: { label: string; href: string } };
        setTasks(Array.isArray(payload.items) ? payload.items : []);
        setRunning(Array.isArray(payload.running) ? payload.running : []);
        setReplanAvailable(payload.replan_available === true);
        setEmptyAction(payload.empty_action || null);
      }),
    ]);
  }, [ventureId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Runs finish in the background; refresh until nothing is in flight.
  const runningIds = running.map((t) => t.id).join(",");
  useEffect(() => {
    if (!runningIds) return;
    const timer = window.setInterval(async () => {
      const r = await api(`/api/ventures/${ventureId}/tasks`);
      if (!r.ok) return;
      const payload = r.data as { items?: Task[]; running?: Task[]; replan_available?: boolean; empty_action?: { label: string; href: string } };
      const still = new Set((payload.running || []).map((t) => t.id));
      const finished = runningIds.split(",").filter((id) => !still.has(id));
      setTasks(Array.isArray(payload.items) ? payload.items : []);
      setRunning(Array.isArray(payload.running) ? payload.running : []);
      setReplanAvailable(payload.replan_available === true);
      setEmptyAction(payload.empty_action || null);
      if (finished.length) {
        setActionNote("A task finished. Its result is in chat; anything that failed is back in the queue below.");
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [runningIds, ventureId]);

  async function runTask(task: Task) {
    if (buildPlanTaskId(task)) {
      router.push(buildWorkflowHref(ventureId, task));
      return;
    }
    setPendingId(task.id);
    setActionFailState("");
    setActionNote("");
    const r = await api(`/api/ventures/${ventureId}/tasks/${task.id}/run`, {
      method: "POST",
      body: "{}",
    });
    setPendingId(null);
    if (!r.ok) {
      const code = errorCode(r.data);
      const failure = taskRunFailure(r.status, code, errorMessage(r.data, ""));
      console.error("task run failed", { ventureId, taskId: task.id, status: r.status, code });
      setActionFail(failure.message, "run");
      // An unconfirmed run may have finished server-side; show real status.
      if (failure.unconfirmed) await load();
      return;
    }
    const data = r.data as { status?: string; already_done?: boolean } | null;
    setActionNote(
      data?.status === "done"
        ? `Finished “${task.title || "the task"}”.`
        : `“${task.title || "The task"}” is running. This page updates when it finishes; the result is posted in chat.`,
    );
    await load();
  }

  async function dismissTask(task: Task) {
    setPendingId(task.id);
    setActionFailState("");
    setActionNote("");
    const r = await api(`/api/ventures/${ventureId}/tasks/${task.id}/dismiss`, {
      method: "POST",
      body: JSON.stringify({ reason: "not_relevant" }),
    });
    setPendingId(null);
    if (!r.ok) {
      setActionFail(errorMessage(r.data, "Couldn’t dismiss that task."), "dismiss");
      return;
    }
    setActionNote(`Dismissed “${task.title || "the task"}”.`);
    await load();
  }

  async function replan() {
    setReplanning(true);
    setActionFailState("");
    setActionNote("Re-cutting the work queue…");
    const r = await api(`/api/ventures/${ventureId}/tasks/replan`, {
      method: "POST",
      body: "{}",
    });
    setReplanning(false);
    if (!r.ok) {
      setActionNote("");
      setActionFail(errorMessage(r.data, "Couldn’t replan."), "replan");
      return;
    }
    setActionNote("Queue updated.");
    await load();
  }

  const headerRight = replanAvailable && ((tasks?.length || 0) + running.length > 0) ? (
    <button type="button" disabled={replanning} onClick={() => void replan()}>
      {replanning ? "Replanning…" : "Replan"}
    </button>
  ) : null;

  return (
    <AppShell
      title={title}
      subtitle={QUESTION}
      ventureId={ventureId}
      stage={stage}
      status={status}
      headerRight={headerRight}
    >
      <div className={styles.page}>
        <h1 className={styles.question}>{QUESTION}</h1>
        <p className={styles.lede}>
          This is the venture’s work queue, not a personal checklist. Each item names an owner
          and why it is next. Click a title for a fuller brief. Work already in flight is listed
          first.
        </p>
        {status?.code === "live" && <p className={styles.note}>
          The landing page is live. Open plan tasks remain here until their own review and acceptance is recorded.
        </p>}

        <WorkingForYou ventureId={ventureId} />

        {running.length > 0 && (
          <div className={styles.list} style={{ marginBottom: 24 }}>
            <p className={styles.lede}>Being done now</p>
            {running.map((task) => (
              <article key={task.id} className={styles.card}>
                <h2>
                  <TaskBrief
                    ventureId={ventureId}
                    taskId={task.id}
                    title={task.title || "Untitled task"}
                    detail={task.detail}
                  />
                </h2>
                <p className={styles.owner}>
                  {task.owner_name
                    ? `${prettyName(task.owner_name)}${
                        task.owner_role ? ` · ${prettyRole(task.owner_role)}` : ""
                      }`
                    : "Owner not assigned yet"}
                  {" · in progress"}
                </p>
                <p>Deadline estimate: {estimatedDeadline(task.due_at)}.</p>
                {task.public_site_evidence && <p className={styles.note}>
                  Public site published: <a href={task.public_site_evidence.url} target="_blank" rel="noopener noreferrer">{task.public_site_evidence.url}</a>.
                  {" "}This task still awaits its own review and acceptance.
                </p>}
              </article>
            ))}
          </div>
        )}

        {busy && tasks == null && (
          <div className={styles.loading}>
            <p>Loading the work queue…</p>
          </div>
        )}

        {error && (
          <div className={styles.error}>
            <h2>Couldn’t load work</h2>
            <ErrorNotice error={error} source="tasks.load" intent="tasks_load" className={styles.fail} />
            <button type="button" className="primary" onClick={() => void load()}>
              Retry
            </button>
          </div>
        )}

        {actionNote && <p className={styles.note}>{actionNote}</p>}
        <ErrorNotice error={actionFail} source="tasks.actionFail" className={styles.fail} />

        {!busy && !error && tasks && tasks.length === 0 && running.length === 0 && (
          <div className={styles.empty}>
            <h2>No work in the queue yet</h2>
            {emptyAction && <p>
              <button type="button" className="primary" onClick={() => router.push(emptyAction.href)}>
                {emptyAction.label}
              </button>
            </p>}
          </div>
        )}

        {tasks && tasks.length > 0 && (
          <div className={styles.list}>
            {tasks.map((task, i) => {
              const owner = task.owner_name
                ? `${prettyName(task.owner_name)}${
                    task.owner_role ? ` · ${prettyRole(task.owner_role)}` : ""
                  }`
                : task.owner_role
                  ? prettyRole(task.owner_role)
                  : null;
              const effort =
                typeof task.est_minutes === "number" && task.est_minutes > 0
                  ? ` · ~${task.est_minutes} min`
                  : "";
              const running = pendingId === task.id;
              const buildHandoff = buildPlanTaskId(task);
              return (
                <article key={task.id} className={styles.card}>
                  <h2>
                    <span className={styles.rank}>{task.rank ?? i + 1}</span>
                    <TaskBrief
                      ventureId={ventureId}
                      taskId={task.id}
                      title={task.title || "Untitled task"}
                    detail={task.detail}
                    />
                  </h2>
                  <p className={styles.why}>
                    {task.why_now ||
                      "No “why now” is attached to this item, it is in the queue without a recorded reason."}
                  </p>
                  <p>Deadline estimate: {estimatedDeadline(task.due_at)}.</p>
                  <p className={styles.owner}>
                    {owner
                      ? `Owner: ${owner}${effort}`
                      : `Owner not assigned yet, the orchestrator has not named a teammate.${effort}`}
                    {task.plan_task_state
                      ? ` · ${task.plan_task_state.replaceAll("_", " ")}`
                      : task.status ? ` · ${task.status}` : ""}
                  </p>
                  {task.public_site_evidence && <p className={styles.note}>
                    Public site published: <a href={task.public_site_evidence.url} target="_blank" rel="noopener noreferrer">{task.public_site_evidence.url}</a>.
                    {" "}This is publication evidence; this task still awaits its own review and acceptance.
                  </p>}
                  {task.plan_scope === "out" && <p className={styles.fail}>
                    This step cannot run as planned. {task.plan_scope_reason || "Review the plan before continuing."}
                  </p>}
                  <div className={styles.actions}>
                    <button
                      type="button"
                      className="primary"
                      disabled={running || replanning || (task.runnable === false && !buildHandoff && task.plan_scope !== "out")}
                      onClick={() => task.plan_scope === "out"
                        ? router.push(`/v/${ventureId}/plan${planTaskId(task) ? `#plan-task-${encodeURIComponent(planTaskId(task))}` : ""}`)
                        : void runTask(task)}
                    >
                      {running
                        ? "Running…"
                        : task.plan_scope === "out"
                          ? "Review blocked step"
                        : buildHandoff
                          ? task.build_review_ready ? "Review build" : "View build"
                          : task.runnable === false
                            ? "Not runnable yet"
                            : "Run"}
                    </button>
                    <button
                      type="button"
                      disabled={running || replanning}
                      onClick={() => void dismissTask(task)}
                    >
                      Dismiss
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
        {((tasks?.length || 0) + running.length > 0) && <NextStepStrip ventureId={ventureId} compact />}
      </div>
    </AppShell>
  );
}
