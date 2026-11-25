"use client";

import { ChatAnchor } from "@/components/chat/ChatLink";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { captureFounderTimezone, estimatedDeadline, taskActionHref, taskCanEdit, taskClient, taskIsActive, taskStateLabel, type TaskAction, type TaskContract, type TaskResult, type TaskView } from "@/lib/taskApi";
import { renderLightMd, splitFences } from "@/lib/chatMarkdown";
import { RUN_STATUS_REFRESH } from "@/lib/runStatusPresentation";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "@/app/v/[ventureId]/tasks/tasks.module.css";

type Scope = { venture: string; subject: string; epoch: number; generation: number };
type Saved = Scope & { items: TaskView[]; timezone: string };
type Preview = { contract: TaskContract; timezone: string; key: string };
const failure = (error: unknown) => error instanceof Error ? error.message : "Connection interrupted. Refresh saved tasks before trying again.";

export function WorkingForYou({ ventureId, onActiveChange }: { ventureId: string; onActiveChange?: (count: number | null) => void }) {
  const [saved, setSaved] = useState<Saved | null>(null);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [goal, setGoal] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [editing, setEditing] = useState<TaskView | null>(null);
  const [opened, setOpened] = useState<(Scope & { result: TaskResult; task: TaskView }) | null>(null);
  const [researchRequest, setResearchRequest] = useState<(Scope & { taskId: string; key: string }) | null>(null);
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState("");
  const currentVenture = useRef(ventureId); currentVenture.current = ventureId;
  const generation = useRef(0);
  const timezone = useRef<(Scope & { value: string }) | null>(null);
  const resultDialog = useRef<HTMLDialogElement | null>(null);
  const readTicket = useRef(0), actionTicket = useRef(0), inFlight = useRef(false);
  const goalId = useId();
  const current = (scope: Scope) => scope.venture === currentVenture.current
    && scope.subject === verifiedSessionSubject() && scope.epoch === verifiedSessionEpoch() && scope.generation === generation.current;
  const visible = saved && current(saved) ? saved : null;
  const visibleResult = opened && current(opened) ? opened.result : null;
  const resultTask = opened && current(opened) ? visible?.items.find(task => task.id === opened.task.id) || opened.task : null;
  useEffect(() => { if (visibleResult && !resultDialog.current?.open) resultDialog.current?.showModal(); }, [visibleResult]);

  const load = useCallback(async () => {
    const ticket = ++readTicket.current;
    await ensureSession();
    const subject = verifiedSessionSubject();
    if (!subject || ticket !== readTicket.current || ventureId !== currentVenture.current) return;
    const scope = { venture: ventureId, subject, epoch: verifiedSessionEpoch(), generation: generation.current };
    try {
      const [items, preferences] = await Promise.all([taskClient.list(ventureId),
        timezone.current && current(timezone.current) ? Promise.resolve({ timezone: timezone.current.value }) : captureFounderTimezone(ventureId)]);
      if (ticket === readTicket.current && current(scope)) {
        timezone.current = { ...scope, value: preferences.timezone };
        setSaved({ ...scope, items, timezone: preferences.timezone }); setLoadError("");
      }
    } catch (error) { if (ticket === readTicket.current && current(scope)) { setOpened(null); setLoadError(failure(error)); } }
  }, [ventureId]);

  useEffect(() => {
    const reset = () => {
      ++generation.current;
      timezone.current = null;
      ++readTicket.current; ++actionTicket.current; inFlight.current = false;
      setSaved(null); setGoal(""); setPreview(null); setEditing(null); setOpened(null); setPending(false); setNote(""); setLoadError(""); setActionError("");
      void load();
    };
    reset();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 8000);
    const wake = () => { if (!document.hidden) void load(); };
    window.addEventListener("supymem-session-changed", reset);
    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      ++generation.current;
      ++readTicket.current; ++actionTicket.current;
      window.clearInterval(timer); window.removeEventListener("supymem-session-changed", reset);
      window.removeEventListener("online", wake); document.removeEventListener("visibilitychange", wake);
    };
  }, [load]);
  const activeCount = visible && !loadError ? visible.items.filter(taskIsActive).length : null;
  useEffect(() => { onActiveChange?.(activeCount); }, [activeCount, onActiveChange]);

  async function perform(work: (scope: Scope) => Promise<void>) {
    if (inFlight.current || !visible || loadError || !current(visible)) return;
    const scope = visible, ticket = ++actionTicket.current;
    inFlight.current = true; setPending(true); setActionError(""); setNote("");
    const admitted = () => ticket === actionTicket.current && current(scope);
    try {
      // Each operation rechecks its actor/venture before applying a response.
      await work(scope);
    } catch (error) { if (admitted()) { setActionError(failure(error)); void load(); } }
    finally { if (admitted()) { inFlight.current = false; setPending(false); } }
  }
  async function previewTask() {
    if (!goal.trim()) return;
    await perform(async scope => {
      const zone = visible!.timezone, contract = await taskClient.preview(scope.venture, goal.trim(), zone);
      if (current(scope)) setPreview({ contract, timezone: zone, key: crypto.randomUUID() });
    });
  }
  async function startTask() {
    if (!preview) return;
    const accepted = preview;
    const original = editing;
    await perform(async scope => {
      const task = original ? await taskClient.edit(scope.venture, original, accepted.contract, accepted.timezone)
        : await taskClient.create(scope.venture, accepted.contract, accepted.timezone, accepted.key);
      if (!current(scope)) return;
      setPreview(null); setEditing(null); setGoal(""); setNote(original ? "Future runs updated. The task stays paused until you resume it. Saved results remain available."
        : task.kind === "one_time" ? "Task started. Its saved progress will appear below." : "Standing task saved. Each run will report results or a change.");
      setSaved(previous => previous && current(previous) ? { ...previous, items: [task, ...previous.items.filter(item => item.id !== task.id)] } : previous);
      window.dispatchEvent(new Event(RUN_STATUS_REFRESH));
      void load();
    });
  }
  async function act(task: TaskView, action: TaskAction) {
    await perform(async scope => {
      const updated = await taskClient.act(scope.venture, task.id, action, task.updated_at,
        action === "continue" ? crypto.randomUUID() : undefined);
      if (!current(scope)) return;
      setSaved(previous => previous && current(previous) ? { ...previous, items: previous.items.map(item => item.id === updated.id ? updated : item) } : previous);
      setNote(action === "cancel" ? "Stopped. Saved results remain available. Calls already in progress may finish."
        : action === "pause" ? "Paused. Resume when you are ready; saved progress stays available."
          : action === "continue" ? "Another bounded attempt was approved. Check its saved progress below." : "Resumed. Check the saved progress below.");
      window.dispatchEvent(new Event(RUN_STATUS_REFRESH));
    });
  }
  async function openResult(task: TaskView) {
    await perform(async scope => {
      const result = await taskClient.result(scope.venture, task.id);
      if (current(scope)) setOpened({ ...scope, result, task });
    });
  }
  async function rerunResearch(task: TaskView) {
    if (!visible || !current(visible)) return;
    const request = researchRequest && current(researchRequest) && researchRequest.taskId === task.id
      ? researchRequest : { ...visible, taskId: task.id, key: crypto.randomUUID() };
    setResearchRequest(request);
    await perform(async scope => {
      await taskClient.rerunResearch(scope.venture, task, request.key);
      if (!current(scope)) return;
      if (taskIsActive(task)) await taskClient.act(scope.venture, task.id, "cancel");
      if (!current(scope)) return;
      setOpened(null); setResearchRequest(null);
      setNote("A new research run started. Your earlier sources are kept.");
      window.dispatchEvent(new Event(RUN_STATUS_REFRESH));
      void load();
    });
  }
  async function skipResearch(task: TaskView) {
    await perform(async scope => {
      if (taskIsActive(task)) await taskClient.act(scope.venture, task.id, "cancel");
      if (!current(scope)) return;
      setOpened(null); setResearchRequest(null);
      setNote("Research skipped for now. Your saved sources are kept. Choose your next step in chat.");
      window.dispatchEvent(new CustomEvent("supymem:ask", { detail: { action: "journey.skip_step",
        payload: { step: "research", why: "Research was interrupted; I want to skip it for now." } } }));
      window.dispatchEvent(new Event(RUN_STATUS_REFRESH));
      void load();
    });
  }
  function researchActions(task: TaskView, result = false) {
    if (!task.research_recovery) return null;
    return <div className={styles.actions}>
      <button type="button" data-usage-control={result ? "task_result_research_rerun" : "task_research_rerun"}
        disabled={pending || !!loadError} onClick={() => void rerunResearch(task)}>Run research again</button>
      <button type="button" data-usage-control={result ? "task_result_research_skip" : "task_research_skip"}
        disabled={pending || !!loadError} onClick={() => void skipResearch(task)}>Skip for now</button>
      {taskActionHref(task.research_recovery.saved_href, ventureId) && <ChatAnchor href={task.research_recovery.saved_href}>Read saved research</ChatAnchor>}
    </div>;
  }
  const date = (value: string | null | undefined) => estimatedDeadline(value, visible?.timezone).replace(/^by /, "");
  const check = preview?.contract.done_test?.spec;
  const predicate = check && typeof check === "object" ? check as Record<string, unknown> : null;
  const criteria = predicate?.predicate === "rubric" && Array.isArray(predicate.criteria)
    ? predicate.criteria.filter((criterion): criterion is string => typeof criterion === "string") : [];
  const metrics: Record<string, string> = { source_count: "verified sources", prospect_count: "saved prospects",
    draft_count: "saved drafts", variant_count: "saved website previews" };

  return <section aria-label="Working for you" style={{ marginBottom: 24 }}>
    <h2>Working for you</h2>
    <p className={styles.lede}>Hand off a task once, on a schedule, or when an event happens.</p>
    {!visible && !loadError && <p role="status">Checking saved tasks…</p>}
    <ErrorNotice error={loadError} source="handoffs.load" className={styles.fail} />
    {loadError && <button type="button" data-usage-control="task_handoff_refresh" onClick={() => void load()}>Refresh saved tasks</button>}
    <div className={styles.list}>
      {visible?.items.map(task => {
        const actionHref = taskActionHref(task.action?.href, ventureId);
        return <article key={task.id} id={`research-${task.id}`} className={styles.card}>
          <p className={styles.owner}>{taskStateLabel(task.state)} · {task.kind === "one_time" ? "One-time task" : task.kind === "scheduled" ? "Scheduled task" : "Runs when the event happens"}</p>
          <h3>{task.title}</h3>
          <p>{task.goal}</p>
          <p>Deadline estimate: {estimatedDeadline(task.deadline_at, visible?.timezone)}.</p>
          {task.next_fire_at && <p>Next scheduled run: {date(task.next_fire_at)}.</p>}
          {task.blocker && <div role="status"><p>{task.blocker.reason}</p><p>{task.blocker.unblock_action}</p></div>}
          <details>
            <summary data-usage-control="task_handoff_timeline">Plan and saved progress</summary>
            {task.steps.length ? <ol>{task.steps.map(step => <li key={step.id}>
              <strong>{step.title}</strong>: {taskStateLabel(step.state)}.
              <p>Deadline estimate: {estimatedDeadline(step.deadline_at, visible?.timezone)}.</p>
            </li>)}</ol> : <p>The steps will appear when the next run starts.</p>}
            {task.events.length > 0 && <ol aria-label="Saved task timeline">{task.events.map(event => <li key={event.id}>
              {event.message} <small>{date(event.created_at)}</small>
            </li>)}</ol>}
          </details>
          <div className={styles.actions} style={{ marginTop: 12 }}>
            {task.steps.length > 0 && <button type="button" data-usage-control="task_handoff_result_open" disabled={pending || !!loadError} onClick={() => void openResult(task)}>Open result</button>}
            {task.can_continue === true && <button type="button" data-usage-control="task_handoff_continue" disabled={pending || !!loadError} onClick={() => void act(task, "continue")}>Continue</button>}
            {task.can_continue !== true && actionHref && !task.research_recovery && <ChatAnchor data-usage-control="task_handoff_action" href={actionHref}>{task.action!.label}</ChatAnchor>}
            {task.can_continue !== true && task.blocker && !actionHref && <ChatAnchor data-usage-control="task_handoff_action" href={`/v/${encodeURIComponent(ventureId)}?chat=1`}>Open conversation</ChatAnchor>}
            {task.can_resume === true && <button type="button" data-usage-control="task_handoff_resume" disabled={pending || !!loadError} onClick={() => void act(task, "resume")}>{task.state === "auth_required" ? "Resume after account review" : "Resume task"}</button>}
            {["submitted", "working"].includes(task.state) && <button type="button" data-usage-control="task_handoff_pause" disabled={pending || !!loadError} onClick={() => void act(task, "pause")}>Pause task</button>}
            {taskIsActive(task) && <button type="button" data-usage-control="task_handoff_stop" disabled={pending || !!loadError} onClick={() => void act(task, "cancel")}>Stop task</button>}
            {task.kind !== "one_time" && <button type="button" data-usage-control="task_handoff_edit_future" disabled={pending || !!loadError || !taskCanEdit(task)} onClick={() => {
              setEditing(task); setGoal(task.goal); setPreview(null); setActionError(""); setNote("");
            }}>Edit future runs</button>}
          </div>
          {researchActions(task)}
          {task.kind !== "one_time" && !taskCanEdit(task) && <p className={styles.note}>Finish or stop the current run before editing future runs.</p>}
        </article>;
      })}
    </div>
    {visible?.items.length === 0 && <p>No saved handoff tasks yet.</p>}
    <details style={{ marginTop: 16 }} open={editing ? true : undefined}>
      <summary data-usage-control="task_handoff_open">{editing ? "Edit future runs" : "Hand off a task"}</summary>
      {editing && <p>Changes apply to future runs. Current and completed work keeps its saved results. Saving leaves the task paused until you resume it.</p>}
      {preview ? <div className={styles.card}>
        <h3>{preview.contract.title}</h3>
        <p>{preview.contract.goal}</p>
        <p>{preview.contract.kind === "one_time" ? "Runs once." : preview.contract.kind === "scheduled" ? "Runs on the schedule in your request." : "Runs when the event in your request happens."} Timezone: {preview.timezone}.</p>
        <p>Completion estimate: about {Math.ceil(preview.contract.estimate_seconds / 60)} minutes after each run starts.</p>
        {preview.contract.kind === "scheduled" && typeof preview.contract.trigger?.first_fire_at === "string" && <p>First run estimate: {estimatedDeadline(preview.contract.trigger.first_fire_at, preview.timezone)}.</p>}
        {typeof preview.contract.done_test?.spec === "string" && <p>Done when: {preview.contract.done_test.spec}</p>}
        {predicate?.predicate === "at_least" && typeof predicate.count === "number" && typeof predicate.metric === "string" && metrics[predicate.metric]
          && <p>Done when: at least {predicate.count} {metrics[predicate.metric]} pass the saved result checks.</p>}
        {predicate && ["workflow_receipt", "all_steps_verified"].includes(String(predicate.predicate))
          && <p>Done when: every planned step has a verified saved result.</p>}
        {criteria.length > 0 && <div><p>Done when: the saved result passes review against these criteria.</p>
          <ul>{criteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul>
          {typeof preview.contract.tolerance === "number" && <p>Required review score: {Math.round(preview.contract.tolerance * 100)}% or higher.</p>}
        </div>}
        <ol>{preview.contract.steps.map((step, index) => <li key={index}>{step.title}{step.runs_on === "device" && " (needs your Mac awake)"}</li>)}</ol>
        <p>Sending, posting, purchases and deletion will wait for your approval.</p>
        <div className={styles.actions}>
          <button type="button" className="primary" data-usage-control={editing ? "task_handoff_save_edit" : "task_handoff_start"} disabled={pending || !!loadError} onClick={() => void startTask()}>{pending ? "Saving task…" : editing ? "Save future runs" : "Start task"}</button>
          <button type="button" data-usage-control="task_handoff_edit" disabled={pending} onClick={() => {
            if (editing) setEditing(visible?.items.find(task => task.id === editing.id) || editing);
            setPreview(null); setActionError("");
          }}>Edit request</button>
        </div>
      </div> : <form onSubmit={event => { event.preventDefault(); void previewTask(); }}>
        <label htmlFor={goalId}>What should Supymem do?</label>
        <textarea id={goalId} value={goal} required maxLength={8000} rows={4} disabled={pending || !visible || !!loadError}
          placeholder="Every Friday, summarize waitlist sign-ups and replies."
          style={{ display: "block", width: "100%", margin: "8px 0" }} onChange={event => setGoal(event.target.value)} />
        <p className={styles.note}>Tasks run once unless you specify a frequency or event.</p>
        <button type="submit" className="primary" data-usage-control="task_handoff_preview" disabled={pending || !visible || !!loadError || !goal.trim()}>{pending ? "Preparing plan…" : editing ? "Preview future runs" : "Preview task"}</button>
      </form>}
      {editing && <button type="button" data-usage-control="task_handoff_cancel_edit" disabled={pending} onClick={() => { setEditing(null); setPreview(null); setGoal(""); setActionError(""); }}>Cancel edit</button>}
    </details>
    <ErrorNotice error={actionError} source="handoffs.action" className={styles.fail} />
    {note && <p role="status">{note}</p>}
    {visibleResult && <dialog ref={resultDialog} aria-label="Saved task result" onClose={() => setOpened(null)}
      onCancel={() => setOpened(null)} style={{ width: "min(680px, 90vw)", maxHeight: "80vh", overflowY: "auto" }}>
      <h2>{visibleResult.title}</h2>
      <p>{resultTask?.research_recovery ? "Research was interrupted. Your saved sources are kept."
        : { verified: "Verified saved result", partial: "Saved partial work", pending: "Result is not ready yet",
          unconfirmed: "Result could not be confirmed", withheld: "Result needs review" }[visibleResult.status]}</p>
      {!resultTask?.research_recovery && visibleResult.status === "pending" && resultTask && <p>{resultTask.blocker?.reason || `${resultTask.title} is ${taskStateLabel(resultTask.state).toLowerCase()}.`}</p>}
      {splitFences(resultTask?.research_recovery && visibleResult.status === "pending" ? "" : visibleResult.body_md).map((segment, index) => segment.code ? <pre key={index}>{segment.text}</pre>
        : <div key={index} style={{ whiteSpace: "pre-wrap" }} dangerouslySetInnerHTML={{ __html: renderLightMd(segment.text) }} />)}
      {visibleResult.artifacts.map(link => <p key={link.href}><ChatAnchor data-usage-control="task_handoff_result_artifact" href={link.href}>{link.label}</ChatAnchor></p>)}
      {resultTask && researchActions(resultTask, true)}
      {!resultTask?.research_recovery && resultTask && visibleResult.status !== "verified" && <div className={styles.actions}>
        {taskActionHref(resultTask.action?.href, ventureId) && <ChatAnchor href={resultTask.action!.href}>{resultTask.action!.label}</ChatAnchor>}
        {taskIsActive(resultTask) && <button type="button" disabled={pending || !!loadError} onClick={() => void act(resultTask, "cancel")}>Skip for now</button>}
      </div>}
      <ErrorNotice error={actionError} source="handoffs.result.action" className={styles.fail} />
      <button type="button" data-usage-control="task_handoff_result_close" onClick={() => setOpened(null)}>Close result</button>
    </dialog>}
  </section>;
}
