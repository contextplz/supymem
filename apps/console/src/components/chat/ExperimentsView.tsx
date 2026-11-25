"use client";

import { useEffect, useState } from "react";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import type { ChatMessage } from "@/lib/api-types";
import { prefillChat } from "@/lib/chatPrefill";
import { ChatAnchor } from "@/components/chat/ChatLink";
import { MarketBeliefs } from "@/components/chat/MarketBeliefs";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { ExperimentFeed, type ExperimentAutopilot } from "./ExperimentFeed";
import styles from "./ExperimentsView.module.css";

type Option = { id: string; statement: string; state: string; status: string };
type Round = { id: string; number: number; state: string; status: string; created_at: string;
  actions?: { action?: string }[];
  ended_at: string | null; selected_option_id: string | null; options: Option[] };
type Exploration = { state: string; rounds: Round[]; next_moves?: ChatMessage["next_moves"] | null;
  experiments_autopilot_v1?: ExperimentAutopilot | null };
type Activity = { seq: number; text: string; detail: { venture_id: string; option_id: string;
  evidence_level: string; started_at: string; ended_at: string; outcome: string; source_url?: string | null } };

const STATUSES = ["Idea", "Testing", "Worked", "Didn't work", "Too early to tell"];
const METHODS: Record<string, string> = { hypothesis: "Idea", desk_research: "Researched",
  prospect_discovery: "Found people", drafted_outreach: "Drafts only", sent_outreach: "Sent to people",
  market_response: "Heard from people", conclusion: "Compared results" };

function act(action: string, roundId?: string, optionId?: string) {
  window.dispatchEvent(new CustomEvent("supymem:ask", { detail: { action,
    payload: action === "connections.connect" ? { query: "connect gmail" }
      : { ...(roundId ? { round_id: roundId } : {}), ...(optionId ? { option_id: optionId } : {}) } } }));
}

function status(item: { state: string; status: string }) {
  return STATUSES.includes(item.status) ? item.status
    : item.state === "running" ? "Testing" : ["pending", "empty"].includes(item.state) ? "Idea" : "Too early to tell";
}

function method(item: Activity) {
  return item.detail.outcome === "succeeded" ? METHODS[item.detail.evidence_level] || "Recorded action"
    : item.detail.outcome === "empty" ? "No results yet" : "Did not finish";
}

function sourceLink(source: string | null | undefined) {
  if (!source) return undefined;
  try {
    const url = new URL(source);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

function NextStep({ state, roundId, optionId, needsSearch }: { state: string; roundId: string; optionId?: string; needsSearch?: boolean }) {
  if (state === "failed") return <div className={styles.actions}>
    <ErrorNotice error="Your test did not finish. You can retry or try another way." source="experiments_view.option" />
    <button type="button" onClick={() => act("experiment.exploration.retry", roundId, optionId)}>Retry your test</button>
    <button type="button" onClick={() => act("experiment.exploration.next", roundId, optionId)}>Try another way</button>
  </div>;
  if (state === "blocked_by_permission") return <div className={styles.actions}>
    <p>You need to connect a service or review its permissions before this test can continue.</p>
    <button type="button" onClick={() => act(needsSearch ? "connections.status" : "connections.connect")}>{needsSearch ? "Review your connections" : "Connect your email"}</button>
    {needsSearch && <button type="button" onClick={() => act("experiment.exploration.retry", roundId, optionId)}>Try your test</button>}
  </div>;
  if (["insufficient_evidence", "too_early"].includes(state)) return <div className={styles.actions}>
    <p>You do not have enough real responses yet. You can extend the test or try another group.</p>
    <button type="button" onClick={() => act("experiment.exploration.next", roundId, optionId)}>Extend your test</button>
  </div>;
  return null;
}

export function YourPriorities({ data, retry }: { data: Exploration; retry: () => void }) {
  const moves = data.next_moves?.actions;
  const available = data.next_moves?.kind === "next_moves" && Array.isArray(moves) && moves.every(move => move && typeof move.label === "string"
    && !!move.label.trim() && typeof move.action === "string" && !!move.action.trim()
    && (move.disabled_reason === undefined || typeof move.disabled_reason === "string")
    && (move.payload === undefined || move.payload !== null && typeof move.payload === "object" && !Array.isArray(move.payload)));
  const options = data.rounds.flatMap(round => round.options.map(option => ({ round, option })));
  return <section className={`panel-card ${styles.priorities}`} aria-labelledby="your-priorities">
    <h2 id="your-priorities">Your priorities</h2>
    <p>Your next moves and the ideas you are testing. Nothing starts until you choose an action.</p>
    {!available ? <div className={styles.actions}>
      <ErrorNotice error="Your next moves could not load. Your experiments are still available." source="experiments_view.priorities" />
      <button type="button" onClick={retry}>Reload your priorities</button>
      <button type="button" onClick={() => act("next_moves")}>Ask for your next move</button>
    </div> : !moves.length ? <p>You have no next moves saved. <button type="button" onClick={() => act("next_moves")}>Ask for your next move</button></p>
      : <details open={moves.length <= 2}><summary>Your next moves ({moves.length})</summary>
        <ol tabIndex={0} aria-label="Your next moves">{moves.map(move => <li key={move.action}>
          <button type="button" disabled={!!move.disabled_reason} title={move.disabled_reason}
            onClick={() => window.dispatchEvent(new CustomEvent("supymem:ask", { detail: { action: move.action, payload: move.payload || {} } }))}>{move.label}</button>
          {move.disabled_reason && <p>{move.disabled_reason}</p>}
        </li>)}</ol>
      </details>}
    {!options.length ? <p>You have no experiment options yet. Prepare your ideas below.</p>
      : <details open={options.length <= 2}><summary>Your experiment options ({options.length})</summary>
        <ul tabIndex={0} aria-label="Your experiment options">{options.map(({ round, option }) => <li key={`${round.id}:${option.id}`}>
          <p>{option.statement}</p><p className="muted">Round {round.number}: {status(option)}</p>
          {option.state === "pending" && <p>Your idea is ready. You can start your round below.</p>}
          {option.state === "running" && <p role="status">You are testing this idea.</p>}
          {["worked", "did_not_work"].includes(option.state) && status(option) !== "Too early to tell" && <button type="button"
            onClick={() => act(round.selected_option_id === option.id ? "experiment.exploration.continue" : "experiment.exploration.select", round.id, option.id)}>
            {round.selected_option_id === option.id ? "Continue with this idea" : "Choose this idea"}</button>}
          <NextStep state={["worked", "did_not_work"].includes(option.state) && status(option) === "Too early to tell" ? "too_early" : option.state}
            roundId={round.id} optionId={option.id} needsSearch={Array.isArray(round.actions) && round.actions.some(action => action?.action === "connections.status")} />
        </li>)}</ul>
      </details>}
  </section>;
}

export function InvestigationsChart({ rounds, activity, now = Date.now() }: { rounds: Round[]; activity: Activity[]; now?: number }) {
  const rows = rounds.flatMap(round => round.options.map(option => {
    const events = activity.filter(item => item.detail.option_id === option.id);
    const starts = events.map(item => Date.parse(item.detail.started_at)).filter(Number.isFinite);
    const ends = events.map(item => Date.parse(item.detail.ended_at)).filter(Number.isFinite);
    const start = starts.length ? Math.min(...starts) : null;
    const end = start === null ? null : option.state === "running" ? Math.max(now, start)
      : Math.max(start, ...ends);
    return { round, option, start, end };
  }));
  const times = rows.flatMap(row => row.start === null ? [] : [row.start, row.end!]);
  const first = times.length ? Math.min(...times) : now;
  const last = times.length ? Math.max(...times) : now;
  const position = (time: number) => 4 + (time - first) / Math.max(last - first, 1) * 392;
  return <figure className={styles.chart} aria-label="Your investigation timeline and status bars">
    <figcaption><h2>Your investigations</h2><p>Timing comes from recorded actions, not estimates.</p></figcaption>
    <div className={styles.axis}><time dateTime={new Date(first).toISOString()}>{new Date(first).toLocaleString()}</time>
      <time dateTime={new Date(last).toISOString()}>{new Date(last).toLocaleString()}</time></div>
    <ol className={styles.timeline} tabIndex={0} aria-label="Recorded investigation times">{rows.map(({ round, option, start, end }) => <li key={option.id}>
      <div><strong>Round {round.number}: {option.statement}</strong><span>{status(option)}</span></div>
      {start === null ? <p className="muted">No recorded timing yet.</p> : <svg viewBox="0 0 400 24" preserveAspectRatio="none" role="img"
        aria-label={`${option.statement}. ${status(option)}. Started ${new Date(start).toLocaleString()}. ${option.state === "running" ? "Still testing" : `Last recorded ${new Date(end!).toLocaleString()}`}.`}>
        <line x1="4" x2="396" y1="12" y2="12" className={styles.track} />
        <rect x={position(start)} y="5" width={Math.max(position(end!) - position(start), 3)} height="14" rx="3" className={styles.bar} data-status={status(option)} />
      </svg>}
    </li>)}</ol>
    <h3>Ideas by status</h3>
    <ul className={styles.statusBars}>{STATUSES.map(label => {
      const count = rows.filter(row => status(row.option) === label).length;
      return <li key={label}><span>{label}: {count}</span><div role="img" aria-label={`${label}: ${count} of ${rows.length} ideas`} className={styles.trackBar}>
        <span className={styles.bar} data-status={label} style={{ width: `${rows.length ? count / rows.length * 100 : 0}%` }} />
      </div></li>;
    })}</ul>
  </figure>;
}

export function ExperimentsView({ ventureId }: { ventureId: string }) {
  const actor = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
  const [data, setData] = useState<Exploration | null>(null);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [disabled, setDisabled] = useState(false);
  const [error, setError] = useState("");
  const [activityError, setActivityError] = useState("");
  const [activityLoaded, setActivityLoaded] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let cancelled = false, bound = false, enabled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let actor: string | null = null, epoch = 0, cursor = 0;
    const recorded = new Map<number, Activity>();
    const current = () => !cancelled && actor === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
    setData(null); setActivity([]); setDisabled(false); setError(""); setActivityError(""); setActivityLoaded(false);
    const changed = () => {
      if (!bound) return;
      cancelled = true; clearTimeout(timer); setData(null); setActivity([]); setDisabled(false);
      setError("Your session changed. Refresh to continue."); setActivityError("");
    };
    window.addEventListener("supymem-session-changed", changed);
    const load = async () => {
      try {
        const response = await api(`/api/ventures/${encodeURIComponent(ventureId)}/experiments/rounds`);
        if (!current()) return;
        // The server returns 404 when exploration_v1 is off; it is the only flag authority.
        if (response.status === 404) { enabled = false; setDisabled(true); setData(null); setActivity([]); setError(""); return; }
        if (!response.ok) throw new Error("rounds");
        const result = response.data as Exploration;
        if (!result || !Array.isArray(result.rounds) || !result.rounds.every(round => round && typeof round.id === "string"
          && Number.isInteger(round.number) && Array.isArray(round.options) && round.options.every(option => option
            && typeof option.id === "string" && typeof option.statement === "string"))) throw new Error("rounds");
        enabled = true; setData(result); setDisabled(false); setError("");
        const optionIds = new Set(result.rounds.flatMap(round => round.options.map(option => option.id)));
        try {
          while (current()) {
            const response = await api(`/api/ventures/${encodeURIComponent(ventureId)}/activity?phase=experiments&kind=option_activity&after_seq=${cursor}&limit=500`);
            if (!current()) return;
            const page = response.data as { items: Activity[]; next_cursor: string | null };
            if (!response.ok || !page || !Array.isArray(page.items)) throw new Error("activity");
            for (const item of page.items) {
              if (Number.isInteger(item.seq) && typeof item.text === "string" && item.detail?.venture_id === ventureId
                && optionIds.has(item.detail.option_id)) recorded.set(item.seq, item);
            }
            setActivity([...recorded.values()].filter(item => optionIds.has(item.detail.option_id)));
            if (!page.items.length) break;
            const next = Number(page.next_cursor);
            if (!Number.isSafeInteger(next) || next <= cursor) throw new Error("activity");
            cursor = next;
            if (page.items.length < 500) break;
          }
          setActivityError("");
        } catch { if (current()) setActivityError("Your recorded actions could not load. Your ideas are still available."); }
        finally { if (current()) setActivityLoaded(true); }
      } catch { if (current()) setError("Your experiments could not load. Try again or ask in chat."); }
      finally { if (current() && enabled) timer = setTimeout(() => void load(), 5000); }
    };
    void (async () => {
      try {
        await ensureSession();
        if (cancelled) return;
        actor = verifiedSessionSubject(); epoch = verifiedSessionEpoch(); bound = true;
        if (!actor) throw new Error("session");
        await load();
      } catch { if (!cancelled) setError("Your market map could not load. Try again."); }
    })();
    return () => { cancelled = true; clearTimeout(timer); window.removeEventListener("supymem-session-changed", changed); };
  }, [ventureId, refresh]);

  if (disabled) return <MarketBeliefs ventureId={ventureId} timeline />;
  if (!data) return <div className={`panel-card ${styles.view}`}>
    {!error && <p role="status">Loading your market map...</p>}
    <ErrorNotice error={error} source="experiments_view.load" />
    {error && <div className={styles.actions}><button type="button" onClick={() => setRefresh(value => value + 1)}>Try again</button>
      <button type="button" onClick={() => prefillChat("Help me review my market ideas in chat.")}>Try in chat</button></div>}
  </div>;
  return <div className={styles.view}>
    <section className="panel-card"><h1>Experiments</h1><p>Compare what could work, what you are testing, and what happened.</p>
      <p className="muted">Research and drafts are not proof that people want your idea. You can start your next step here. Sending still needs your approval.</p>
      <ErrorNotice error={error} source="experiments_view.load" />
      <ErrorNotice error={activityError} source="experiments_view.activity" />
      {!activityLoaded && <p role="status">Loading your recorded actions...</p>}
      <button type="button" onClick={() => setRefresh(value => value + 1)}>Refresh your experiments</button>
    </section>
    <YourPriorities data={data} retry={() => setRefresh(value => value + 1)} />
    {data.experiments_autopilot_v1 && <ExperimentFeed ventureId={ventureId}
      data={data.experiments_autopilot_v1} onAction={(action, payload) => {
        if (verifiedSessionSubject() !== actor || verifiedSessionEpoch() !== epoch) return false;
        window.dispatchEvent(new CustomEvent("supymem:ask", { detail: { action, payload } }));
      }} />}
    {!data.rounds.length && <section className="panel-card"><h2>No ideas to compare yet</h2>
      <p>You can prepare a round of ideas, then choose what to test.</p>
      <button type="button" onClick={() => act("experiment.exploration.next")}>Prepare your ideas</button>
    </section>}
    {!!data.rounds.length && <>
      <section className="panel-card"><InvestigationsChart rounds={data.rounds} activity={activity} /></section>
      <section aria-label="Your rounds"><h2>Your rounds</h2>{data.rounds.map((round, index) => <details key={round.id} className={`panel-card ${styles.round}`} open={data.rounds.length <= 2 || index === data.rounds.length - 1}>
        <summary>Round {round.number} <span className="tag">{status(round)}</span></summary>
        {round.state === "pending" && <button type="button" onClick={() => act("experiment.exploration.start", round.id)}>Start all three</button>}
        {round.selected_option_id && <button type="button" onClick={() => act("experiment.exploration.continue", round.id, round.selected_option_id!)}>Continue with your selected idea</button>}
        {!round.options.some(option => option.state === round.state) && <NextStep state={round.state} roundId={round.id} needsSearch={Array.isArray(round.actions) && round.actions.some(action => action?.action === "connections.status")} />}
        <div className={styles.lanes}>{round.options.map(option => {
          const events = activity.filter(item => item.detail.option_id === option.id)
            .sort((first, second) => Date.parse(first.detail.started_at) - Date.parse(second.detail.started_at) || first.seq - second.seq);
          const latestMethod = events.length ? method(events[events.length - 1]) : null;
          return <section key={option.id} className={styles.lane} aria-label={option.statement}>
            <h3>{option.statement}</h3><span className="tag" aria-label={`Status: ${status(option)}`}>{status(option)}</span>
            {latestMethod && <p className="muted">{latestMethod}</p>}
            {round.selected_option_id === option.id && <p>Your selected idea</p>}
            {option.state === "pending" && <p>Your idea is ready. Testing has not started yet.</p>}
            {option.state === "running" && <p role="status">You are testing this idea. New actions appear here.</p>}
            {["worked", "did_not_work"].includes(option.state) && <p>{status(option) === "Too early to tell" ? "You have research to review, but not enough real responses to decide." : "Your test is complete. Review the recorded actions below."}</p>}
            <NextStep state={["worked", "did_not_work"].includes(option.state) && status(option) === "Too early to tell" ? "too_early" : option.state} roundId={round.id} optionId={option.id} needsSearch={Array.isArray(round.actions) && round.actions.some(action => action?.action === "connections.status")} />
            <details className={styles.log} open={events.length > 0 && events.length <= 2}>
              <summary>Your recorded actions ({events.length})</summary>
              {!events.length && <p>{!activityLoaded ? "Loading your recorded actions..." : activityError ? "Your actions are unavailable. Refresh to try again." : "No actions recorded yet. Nothing has been reported as completed."}</p>}
              <ol tabIndex={0} aria-label={`Recorded actions for ${option.statement}`}>{events.map(item => <li key={item.seq}>
                <p>{item.text}</p><p className="muted">{method(item)}</p>
                {Number.isFinite(Date.parse(item.detail.started_at)) && <time dateTime={item.detail.started_at}>{new Date(item.detail.started_at).toLocaleString()}</time>}
                {sourceLink(item.detail.source_url) && <ChatAnchor href={sourceLink(item.detail.source_url)} target="_blank" rel="noopener noreferrer">Read source</ChatAnchor>}
              </li>)}</ol>
            </details>
          </section>;
        })}</div>
      </details>)}</section>
    </>}
    <details className={`panel-card ${styles.ideas}`}><summary>Your market ideas and earlier tests</summary><MarketBeliefs ventureId={ventureId} plainWords timeline /></details>
  </div>;
}
