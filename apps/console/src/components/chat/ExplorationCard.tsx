"use client";

import { useEffect, useRef, useState } from "react";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { safeLoopText } from "@/lib/loopProgress";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import type { ChatMessage } from "@/lib/api-types";
import { ExperimentFeed, type ExperimentAutopilot } from "./ExperimentFeed";
import styles from "./ExplorationCard.module.css";

type Activity = { id: number; option_id: string; text: string; action: string; outcome: string;
  evidence_level?: string; evidence_summary?: string; source_url?: string | null;
  started_at: string; ended_at?: string | null };
type Option = { id: string; statement: string; state: string; experiment_ids?: string[] };
export type ExplorationRound = { id: string; state: string; options: Option[]; selected_option_id?: string | null;
  actions?: { action?: string }[];
  comparison?: { summary?: string; reasons?: Record<string, string>; selected_reason?: string; next_step?: string } };
type Action = (name: string, payload: Record<string, unknown>) => boolean | void | Promise<boolean | void>;
const words: Record<string, string> = { empty: "Idea", pending: "Idea", running: "Testing", worked: "Worked",
  did_not_work: "Didn't work", insufficient_evidence: "Too early to tell", too_early: "Too early to tell",
  failed: "Too early to tell", blocked_by_permission: "Too early to tell" };

function plain(value: unknown) {
  return safeLoopText(value, 600).replace(/\bevidence level\b/gi, "what happened")
    .replace(/\b(?:hypothesis|assumption)\b/gi, "idea")
    .replace(/\b(?:jobs?|runners?|workers?|leases?|receipts?|holds?|credits)\b/gi, "task");
}

function timestamp(value?: string | null) {
  if (!value || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) return null;
  const result = Date.parse(value);
  return Number.isFinite(result) ? result : null;
}

function intervals(items: Activity[], now: number) {
  return items.flatMap(item => {
    const start = timestamp(item.started_at), recordedEnd = item.ended_at ? timestamp(item.ended_at) : now;
    const end = recordedEnd === null ? null : Math.min(recordedEnd, now);
    return start !== null && end !== null && end > start && start <= now ? [{ start, end, item }] : [];
  });
}

function people(items: Activity[]) {
  return items.some(item => item.outcome === "succeeded" &&
    ["email", "reply"].includes(item.action) && /^\d+$/.test(item.evidence_summary || "") && Number(item.evidence_summary) > 0);
}

function count(items: Activity[], action: string) {
  return items.filter(item => item.action === action && item.outcome === "succeeded" && /^\d+$/.test(item.evidence_summary || ""))
    .reduce((total, item) => total + Number(item.evidence_summary), 0);
}

function validRound(value: unknown): value is ExplorationRound {
  if (!value || typeof value !== "object") return false;
  const round = value as ExplorationRound;
  return typeof round.id === "string" && typeof round.state === "string" && Array.isArray(round.options) &&
    round.options.length === 3 && new Set(round.options.map(option => option?.id)).size === 3 &&
    round.options.every(option => option && typeof option.id === "string" && !!option.id.trim() &&
      typeof option.statement === "string" && !!option.statement.trim() && typeof option.state === "string") &&
    (!round.selected_option_id || round.options.some(option => option.id === round.selected_option_id));
}

export function ExplorationCardView({ round, activity = [], loading = false, error = "", busy = false, completed = [],
  now = Date.now(), onAction, onRefresh, autopilot, ventureId }: { round: ExplorationRound | null; activity?: Activity[]; loading?: boolean;
  autopilot?: ExperimentAutopilot | null; ventureId?: string;
  error?: string; busy?: boolean; completed?: string[]; now?: number; onAction?: Action; onRefresh: () => void }) {
  const groups = (round?.options || []).map(option => activity.filter(item => item.option_id === option.id));
  const needsSearch = Array.isArray(round?.actions) && round.actions.some(action => action?.action === "connections.status");
  const spans = groups.map(items => intervals(items, now));
  const overlapping = spans.some((group, index) => group.some(first => spans.slice(index + 1).some(other =>
    other.some(second => Math.max(first.start, second.start) < Math.min(first.end, second.end)))));
  const togetherNow = spans.filter(group => group.some(span => !span.item.ended_at && span.start < now)).length > 1;
  const actionButton = (label: string, action: string, optionId?: string) => <button type="button" className="secondary sm"
    disabled={busy || !onAction || completed.includes(`${action}:${optionId || ""}`)}
    onClick={() => void onAction?.(action, { round_id: round?.id, ...(optionId ? { option_id: optionId } : {}) })}>{label}</button>;
  if (loading && !round) return <section className={styles.card} aria-label="Your three ideas" aria-busy="true"><p role="status">Loading your ideas...</p></section>;
  if (!round) return <section className={styles.card} aria-label="Your three ideas">
    <ErrorNotice error={error} source="chat.exploration.load" />
    <p>{error ? "You can try again or do it another way." : "Your ideas will appear here when they are ready."}</p>
    {error && <button type="button" className="secondary sm" onClick={onRefresh}>Try again</button>}
    {actionButton("Explore three ideas", "experiment.exploration.next")}
  </section>;
  const finished = ["worked", "did_not_work", "too_early", "insufficient_evidence"].includes(round.state);
  return <section className={styles.card} aria-label="Your three ideas" aria-busy={loading || busy}>
    <strong>Here's what I think could work</strong>
    <ol className={styles.options}>{round.options.map((option, index) => {
      const items = groups[index], tested = people(items);
      const researched = items.some(item => ["succeeded", "empty"].includes(item.outcome) &&
        ["search", "fetch", "discover", "draft"].includes(item.action));
      const status = words[option.state] || "Too early to tell";
      const starts = items.map(item => timestamp(item.started_at)).filter((start): start is number => start !== null && start <= now);
      const start = starts.length ? Math.min(...starts) : null;
      const latest = items.at(-1);
      return <li key={option.id}>
        <div className={styles.optionLine}><span className={styles.statement}>{plain(option.statement)}</span>
          <span className="tag">{["Worked", "Didn't work"].includes(status) && !tested ? "Too early to tell" : status}</span></div>
        <div className={styles.progress} role="status">
          {tested ? <span className="tag">Tested with people</span> : researched ? <span className="tag">Researched</span> : null}
          {start !== null && <>Started <time dateTime={new Date(start).toISOString()}>{new Date(start).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time></>}
          {latest ? <span>{plain(latest.text)}</span> : <span>{option.state === "running" ? "Your activity has not arrived yet." : "No actions recorded yet."}</span>}
        </div>
        {option.state === "blocked_by_permission" && needsSearch ? <div className={styles.actions}>
          <span>You need a configured search connection before your research can start.</span>
          {actionButton("Review your connections", "connections.status")}
          {actionButton("Try your test", "experiment.exploration.retry", option.id)}</div>
          : option.state === "blocked_by_permission" && <div className={styles.actions}><span>You need to connect your email before contacting anyone.</span>
          <button type="button" className="secondary sm" disabled={busy || !onAction}
            onClick={() => void onAction?.("connections.connect", { query: "connect gmail" })}>Connect your email</button></div>}
        {option.state === "failed" && <div className={styles.actions}><span>Your test did not finish.</span>
          {actionButton("Try again", "experiment.exploration.retry", option.id)}
          {actionButton("Do it another way", "experiment.exploration.next", option.id)}</div>}
      </li>;
    })}</ol>
    <p className={styles.progress} role="status">{togetherNow && overlapping ? "Your ideas are running at the same time." : overlapping ? "Your recorded actions overlapped." : "No overlapping actions recorded yet."}</p>
    {round.state === "pending" && actionButton("Start all three", "experiment.exploration.start")}
    {round.state === "failed" && !round.options.some(option => option.state === "failed") && <div className={styles.actions}>
      <span>Your tests did not finish.</span>{actionButton("Try again", "experiment.exploration.retry")}
      {actionButton("Do it another way", "experiment.exploration.next")}</div>}
    {round.state === "blocked_by_permission" && !round.options.some(option => option.state === "blocked_by_permission") && <div className={styles.actions}>
      <span>You need to connect your email before contacting anyone.</span><button type="button" className="secondary sm" disabled={busy || !onAction}
        onClick={() => void onAction?.("connections.connect", { query: "connect gmail" })}>Connect your email</button></div>}
    <div className={styles.logs}>{round.options.map((option, index) => <details key={option.id} className={styles.log}>
      <summary>What happened: {plain(option.statement)}</summary>
      {groups[index].length ? <ol>{groups[index].map(item => {
        let source: URL | null = null;
        try { if (item.source_url) { const url = new URL(item.source_url); if (["https:", "http:"].includes(url.protocol) && !url.username && !url.password) source = url; } } catch {}
        return <li key={item.id}><p>{plain(item.text)}</p>
          {timestamp(item.started_at) !== null && <time dateTime={item.started_at}>{new Date(item.started_at).toLocaleTimeString()}</time>}
          {source && <a href={source.href} target="_blank" rel="noopener noreferrer">Read source: {source.hostname}</a>}
        </li>;
      })}</ol> : <p>No actions recorded yet.</p>}
    </details>)}</div>
    {finished && <section className={styles.comparison} aria-label="Your results compared">
      <strong>{round.selected_option_id ? "Your next idea to pursue" : "Your results so far"}</strong>
      {round.comparison?.summary && <p>{plain(round.comparison.summary)}</p>}
      <ul>{round.options.map((option, index) => <li key={option.id}>
        <strong>{plain(option.statement)}{round.selected_option_id === option.id ? " (Selected)" : ""}</strong>
        <p>{plain(round.comparison?.reasons?.[option.id]) || (people(groups[index])
          ? `${count(groups[index], "email")} people emailed, ${count(groups[index], "reply")} replies recorded.`
          : "Your research does not yet show whether people will choose this.")}</p>
      </li>)}</ul>
      {round.selected_option_id && <><p>{plain(round.comparison?.selected_reason) || "Your detailed comparison is not ready yet."}</p>
        {actionButton("Continue with this idea", "experiment.exploration.continue", round.selected_option_id)}</>}
      {!round.selected_option_id && <><p>{round.state === "did_not_work" ? (groups.some(people)
        ? "Your tests didn't work. You can try another three ideas." : "Your research has not shown a clear next step. You can try another three ideas.")
        : "Your test is too small to conclude. You can explore another three ideas."}</p>
        {actionButton("Explore another three", "experiment.exploration.next")}</>}
      {!round.selected_option_id && round.options.map(option => <span key={option.id}>
        {actionButton(`Choose: ${plain(option.statement)}`, "experiment.exploration.select", option.id)}
      </span>)}
      {typeof round.comparison?.next_step === "string" && <p>{plain(round.comparison.next_step)}</p>}
    </section>}
    {autopilot && ventureId && <ExperimentFeed data={autopilot} ventureId={ventureId} onAction={onAction} disabled={busy} completed={completed} />}
    <ErrorNotice error={error} source="chat.exploration" />
    {error && <div className={styles.actions}><button type="button" className="secondary sm" disabled={busy} onClick={onRefresh}>Refresh your results</button>
      {actionButton("Do it another way", "experiment.exploration.next")}</div>}
    {busy && <p role="status">Starting your next step...</p>}
  </section>;
}

export function ExplorationCard({ ventureId, message, onAction, disabled = false }: {
  ventureId: string; message: ChatMessage; onAction?: Action; disabled?: boolean }) {
  const scope = JSON.stringify([verifiedSessionSubject(), verifiedSessionEpoch(), ventureId, message.id]);
  const roundId = (message as ChatMessage & { round_id?: string }).round_id ||
    message.actions?.map(action => action.payload?.round_id).find((id): id is string => typeof id === "string" && !!id) || message.id;
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const [saved, setSaved] = useState<{ scope: string; enabled: boolean; loading: boolean; round: ExplorationRound | null; activity: Activity[]; error: string;
    autopilot?: ExperimentAutopilot | null }>(
    { scope, enabled: false, loading: false, round: null, activity: [], error: "" });
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [completed, setCompleted] = useState<string[]>([]);
  const flight = useRef<string | null>(null);
  const value: typeof saved = saved.scope === scope ? saved : { scope, enabled: false, loading: false, round: null, activity: [], error: "" };
  const resultVersion = JSON.stringify([value.round?.state, value.round?.options.map(option => [option.state, option.experiment_ids])]);
  useEffect(() => { setBusy(false); flight.current = null; }, [scope]);
  useEffect(() => { setCompleted([]); }, [scope, resultVersion]);
  useEffect(() => {
    let cancelled = false, timer: ReturnType<typeof setTimeout> | undefined, cursor = 0;
    const entries = new Map<number, Activity>();
    const current = () => !cancelled && currentScope.current === scope &&
      JSON.stringify([verifiedSessionSubject(), verifiedSessionEpoch(), ventureId, message.id]) === scope;
    const changed = () => { cancelled = true; clearTimeout(timer); setSaved({ scope, enabled: false, loading: false, round: null, activity: [], error: "" }); };
    window.addEventListener("supymem-session-changed", changed);
    async function load() {
      let flagEnabled = false;
      try {
        await ensureSession();
        if (!current()) return;
        const response = await api(`/api/ventures/${encodeURIComponent(ventureId)}/experiments/rounds`);
        if (!current()) return;
        // The endpoint rejects reads while exploration_v1 is off. Never infer the flag from a saved message.
        if (response.status === 404) { setSaved({ scope, enabled: false, loading: false, round: null, activity: [], error: "" }); return; }
        if (!response.ok) throw new Error("load");
        flagEnabled = true;
        const data = response.data as { rounds?: unknown[]; state?: string; experiments_autopilot_v1?: ExperimentAutopilot | null };
        if (!Array.isArray(data?.rounds)) throw new Error("load");
        const initial = message as ChatMessage & Partial<ExplorationRound>;
        const found = data.rounds.find(item => !!item && typeof item === "object" && (item as ExplorationRound).id === roundId);
        const round = found ? { comparison: initial.comparison, ...found as ExplorationRound } : null;
        if (round && !validRound(round)) throw new Error("load");
        const autopilot = data.experiments_autopilot_v1;
        setSaved(previous => ({ scope, enabled: true, loading: true, round, autopilot, activity: previous.scope === scope ? previous.activity : [], error: "" }));
        let more = true;
        while (more) {
          const activityResponse = await api(`/api/ventures/${encodeURIComponent(ventureId)}/activity?phase=experiments&kind=option_activity&after_seq=${cursor}&limit=500`);
          if (!current()) return;
          if (!activityResponse.ok) throw new Error("activity");
          const items = (activityResponse.data as { items?: { seq: number; text: string; detail?: Partial<Activity> }[] })?.items;
          if (!Array.isArray(items)) throw new Error("activity");
          const previousCursor = cursor;
          for (const item of items) {
            if (!Number.isSafeInteger(item.seq) || item.seq <= 0) throw new Error("activity");
            cursor = Math.max(cursor, item.seq);
            const detail = item.detail;
            if (detail && typeof detail.option_id === "string" && typeof detail.action === "string" &&
                typeof detail.outcome === "string" && typeof detail.started_at === "string" && typeof item.text === "string") {
              entries.set(item.seq, { ...detail, id: item.seq, text: item.text } as Activity);
            }
          }
          more = items.length === 500 && cursor > previousCursor;
        }
        const activity = [...entries.values()].filter(item => round?.options.some(option => option.id === item.option_id))
          .sort((first, second) => (timestamp(first.started_at) ?? 0) - (timestamp(second.started_at) ?? 0) || first.id - second.id);
        setSaved({ scope, enabled: true, loading: false, round, autopilot, activity, error: "" });
      } catch {
        if (current()) setSaved(previous => previous.scope === scope ? { ...previous, enabled: previous.enabled || flagEnabled,
          loading: false, error: "Your results could not load. Try again." } : previous);
      }
      if (current()) timer = setTimeout(() => void load(), 4000);
    }
    void load();
    return () => { cancelled = true; clearTimeout(timer); window.removeEventListener("supymem-session-changed", changed); };
  }, [scope, ventureId, message, roundId, refresh]);
  async function act(name: string, payload: Record<string, unknown>) {
    const key = `${name}:${payload.option_id || payload.experiment_id || ""}`;
    if (flight.current || disabled || !onAction || !value.enabled || completed.includes(key) ||
        JSON.stringify([verifiedSessionSubject(), verifiedSessionEpoch(), ventureId, message.id]) !== scope) return false;
    flight.current = scope;
    setBusy(true);
    try {
      const result = await onAction(name, payload);
      if (result === false) throw new Error("action");
      if (currentScope.current !== scope) return false;
      if (name !== "connections.connect") setCompleted(previous => [...previous, key]);
      setRefresh(previous => previous + 1);
      return true;
    } catch {
      if (currentScope.current === scope) setSaved(previous => ({ ...previous, error: "That did not start. Try again or do it another way." }));
      return false;
    } finally {
      if (flight.current === scope) flight.current = null;
      if (currentScope.current === scope) setBusy(false);
    }
  }
  if (!value.enabled) return null;
  return <ExplorationCardView round={value.round} activity={value.activity} loading={value.loading} error={value.error} busy={busy || disabled}
    completed={completed} autopilot={value.autopilot} ventureId={ventureId} onAction={onAction ? act : undefined} onRefresh={() => setRefresh(previous => previous + 1)} />;
}
