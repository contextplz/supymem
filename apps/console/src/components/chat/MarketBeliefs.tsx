"use client";

import { useEffect, useState } from "react";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import { prefillChat } from "@/lib/chatPrefill";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export type MarketBelief = {
  id: string; type: string; statement: string; status: string; parent_id?: string | null;
  evidence: { experiment_id: string; metric: string; value: number | null; events: number;
    threshold: { op?: string; value?: number; min_events?: number }; date: string }[];
};
type MarketPlan = {
  id: string; state: string; days: number; ceiling_micro: number; reserved_micro: number; max_emails_per_day: number;
  tests: { belief_id: string; experiment_id: string; hypothesis: string; state: string;
    deadline: string; started_at?: string; ended_at?: string; error?: string }[];
};
export type MarketMap = { beliefs: MarketBelief[]; plan: MarketPlan | null };
const STATUS: Record<string, string> = { to_test: "To test", testing: "Testing", confirmed: "Confirmed", weak: "Weak", disproven: "Disproven" };
const IDEA_STATUS: Record<string, string> = { to_test: "Idea", testing: "Testing" };
const TYPES: Record<string, string> = { who: "Who", pain: "Pain", price: "Price", channel: "Channel", message: "Message", competitor_gap: "Competitor gap", switching: "Willingness to switch" };

export function BeliefList({ data, timeline = false, plainWords = false }: { data: MarketMap; timeline?: boolean; plainWords?: boolean }) {
  const status = (belief: MarketBelief) => plainWords
    ? (IDEA_STATUS[belief.status] || "Too early to tell")
    : STATUS[belief.status] || belief.status;
  return <section aria-label={plainWords ? "Your market ideas" : "Your market beliefs"}>
    <h2>{plainWords ? "Your market ideas" : "What we believe about your market"}</h2>
    {Object.entries(TYPES).map(([type, title]) => {
      const items = data.beliefs.filter(belief => belief.type === type);
      return items.length ? <section key={type} aria-label={title}>
        <h3>{title}</h3>
        <ul style={{ listStyle: "none", padding: 0 }}>
          {items.map(belief => <li key={belief.id} style={{ marginBottom: 16 }}>
            <span className="tag" aria-label={`Status: ${status(belief)}`}>{status(belief)}</span>
            <p>{belief.statement}</p>
            {belief.parent_id && <small className="muted">{plainWords ? "Follow-up idea" : "Follow-up belief"}</small>}
            {timeline && belief.evidence.map(evidence => <p className="muted" key={evidence.experiment_id}>
              <time dateTime={evidence.date}>{evidence.date.slice(0, 10)}</time>: {evidence.metric.replaceAll("_", " ")} {evidence.value ?? "being measured"}, {evidence.events} events.
              {!plainWords && evidence.threshold.op && <> Threshold: {evidence.threshold.op} {evidence.threshold.value}, at least {evidence.threshold.min_events ?? 1} events.</>}
            </p>)}
          </li>)}
        </ul>
      </section> : null;
    })}
    {!data.beliefs.length && <p>{plainWords ? "Your market ideas will appear here after you prepare them in chat." : "Your market beliefs will appear here after you prepare them in chat."}</p>}
    {timeline && data.plan && <section aria-label={plainWords ? "Your earlier tests" : "Your experiment timeline"}>
      <h3>{plainWords ? "Your earlier tests" : "Experiment timeline"}</h3>
      {plainWords ? <p>Your {data.plan.days}-day plan. Your selected sending pace: {data.plan.max_emails_per_day} emails a day.</p>
        : <p>Your {data.plan.days}-day plan: {data.plan.state.replaceAll("_", " ")}. Your selected sending pace: {data.plan.max_emails_per_day} emails/day.</p>}
      <ol>{data.plan.tests.map(test => <li key={test.experiment_id}>
        <strong>{test.hypothesis}</strong> <span className="tag">{plainWords ? test.state === "running" ? "Testing" : ["pending", "proposed"].includes(test.state) ? "Idea" : "Too early to tell" : test.state}</span>
        <p className="muted">{test.started_at ? <>Started <time dateTime={test.started_at}>{test.started_at.slice(0, 10)}</time>. </> : null}
          Deadline <time dateTime={test.deadline}>{test.deadline.slice(0, 10)}</time>.
          {test.ended_at ? <> Finished <time dateTime={test.ended_at}>{test.ended_at.slice(0, 10)}</time>.</> : null}</p>
        <ErrorNotice error={plainWords && test.error ? "Your test did not finish. You can retry in chat." : test.error || ""} source="market_beliefs.test_error" />
        {plainWords && test.error && <button type="button" onClick={() => prefillChat(`Help me safely retry or test this idea another way: ${test.hypothesis}`)}>Retry in chat</button>}
      </li>)}</ol>
    </section>}
  </section>;
}

export function MarketBeliefs({ ventureId, timeline = false, plainWords = false }: { ventureId: string; timeline?: boolean; plainWords?: boolean }) {
  const [data, setData] = useState<MarketMap | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let bound = false;
    setData(null);
    setError("");
    const changed = () => { if (bound) { cancelled = true; setData(null); setError(plainWords ? "Your session changed. Refresh your ideas to continue." : "Your session changed. Refresh your beliefs to continue."); } };
    window.addEventListener("supymem-session-changed", changed);
    void (async () => {
      try {
        await ensureSession();
        bound = true;
        const actor = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
        const response = await api(`/api/ventures/${encodeURIComponent(ventureId)}/market-map`);
        if (cancelled || actor !== verifiedSessionSubject() || epoch !== verifiedSessionEpoch()) return;
        if (response.ok) setData(response.data as MarketMap);
        else setError(plainWords ? response.status === 401 ? apiFailure(response, "loading your market ideas") : "Your market ideas could not load. Refresh to try again."
          : apiFailure(response, "loading your market beliefs"));
      } catch {
        if (!cancelled) setError(plainWords ? "Your market ideas could not load. Refresh to try again." : "Your market beliefs could not load. Refresh to try again.");
      }
    })();
    return () => { cancelled = true; window.removeEventListener("supymem-session-changed", changed); };
  }, [ventureId, refresh, plainWords]);
  return <div className="panel-card">
    <ErrorNotice error={error} source="market_beliefs.error" />
    {!data && !error && <p role="status">{plainWords ? "Loading your market ideas..." : "Loading your market beliefs..."}</p>}
    {data && <BeliefList data={data} timeline={timeline} plainWords={plainWords} />}
    <button type="button" onClick={() => setRefresh(value => value + 1)}>{plainWords ? "Refresh your ideas" : "Refresh your beliefs"}</button>
    {timeline && !plainWords && <button type="button" data-usage-control="market_beliefs_prepare" onClick={() => prefillChat("Show my market beliefs and prepare a seven-day test plan.")}>Prepare your tests in chat</button>}
  </div>;
}
