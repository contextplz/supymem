"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { safeUrl } from "./CaseReferences";
import styles from "./CaseReferences.module.css";

export type CoachingCard = {
  id: string; company: string; stage: string; situation_at_this_stage: string;
  what_they_did: string; why_it_worked?: string | null; do: string; avoid?: string | null; metric?: string | null;
  honesty: string; sources: { title: string; url: string }[];
  yc_says?: { text: string; title: string; url: string };
};
type CoachingState = { ventureId: string; owner: string; epoch: number; stage: string; items: CoachingCard[]; dismissed: string[] };

export function coachingDismissalKey(owner: string, ventureId: string, stage: string): string {
  return `supymem:coaching:${JSON.stringify([owner, ventureId, stage])}`;
}

export function CoachingView({ item, loading, error, onDismiss, onMore }: {
  item?: CoachingCard; loading?: boolean; error?: boolean; onDismiss: () => void; onMore: () => void;
}) {
  if (loading) return <p role="status">Finding founders who were here…</p>;
  if (error) return <p role="status">Founder references are unavailable. <button type="button" onClick={onMore} data-usage-control="coaching_retry">Try again</button></p>;
  if (!item) return null;
  const quoteUrl = item.yc_says && safeUrl(item.yc_says.url);
  return <section className={`${styles.section} ${styles.coaching}`} aria-label="From founders who were here">
    <h2>From founders who were here</h2>
    <article className={styles.card}>
      <header className={styles.coachingHeading}><h3>{item.company}</h3><span className={styles.caseStudy}>Case study</span></header>
      <p>{item.situation_at_this_stage}</p>
      <dl className={styles.coachingRows}>
        <div><dt>What they did</dt><dd>{item.what_they_did}</dd></div>
        {item.why_it_worked && <div><dt>Why it worked</dt><dd>{item.why_it_worked}</dd></div>}
        <div><dt>Try this</dt><dd>{item.do.replace(/^For\s+[^:\r\n]+:\s*/i, "")}</dd></div>
        {item.avoid && <div><dt>Avoid</dt><dd>{item.avoid.replace(/^Avoid:\s*/i, "")}</dd></div>}
        {item.metric && <div><dt>Measure</dt><dd>{item.metric}</dd></div>}
      </dl>
      {quoteUrl && item.yc_says && <blockquote className={styles.coachingQuote}><p>YC says: “{item.yc_says.text}”</p><cite><a href={quoteUrl} target="_blank" rel="noopener noreferrer" data-usage-control="coaching_yc_source">{item.yc_says.title}</a></cite></blockquote>}
      <p className={styles.meta}>{item.honesty}</p>
      <footer className={styles.coachingSources}>{item.sources.map(source => { const href = safeUrl(source.url); return href ? <a key={href} href={href} target="_blank" rel="noopener noreferrer" data-usage-control="coaching_source">{source.title}</a> : null; })}</footer>
    </article>
    <button type="button" onClick={onDismiss} data-usage-control="coaching_dismiss">Dismiss this card</button>{" "}
    <button type="button" onClick={onMore} data-usage-control="coaching_more">More like this</button>
  </section>;
}

export function FounderCoaching({ ventureId }: { ventureId?: string }) {
  const pathname = usePathname();
  const params = useSearchParams();
  const id = ventureId || params.get("venture") || params.get("ventureId") || undefined;
  const visible = Boolean(id && /^\/v\/[^/]+\/?$/.test(pathname));
  const [state, setState] = useState<CoachingState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [position, setPosition] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const live = useRef(false);
  const current = useRef({ id, pathname, state });
  current.current = { id, pathname, state };
  useEffect(() => {
    if (!id || !visible) return;
    live.current = true;
    let stopped = false;
    let reads = 0;
    const load = async (reset = false) => {
      const ticket = ++reads;
      if (reset) { setState(null); setLoading(true); setPosition(0); }
      try {
        await ensureSession();
        const owner = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
        if (!owner || stopped) return;
        const result = await api(`/api/ventures/${encodeURIComponent(id)}/knowledge/coaching`, { cache: "no-store" });
        if (stopped || reads !== ticket || owner !== verifiedSessionSubject() || epoch !== verifiedSessionEpoch()) return;
        const data = result.data as { venture_id?: string; stage?: string; items?: CoachingCard[] } | null;
        if (!result.ok || data?.venture_id !== id || !data.stage || !Array.isArray(data.items)) { setState(null); setError(true); return; }
        let dismissed: string[] = [];
        try { const saved = JSON.parse(localStorage.getItem(coachingDismissalKey(owner, id, data.stage)) || "[]"); if (Array.isArray(saved)) dismissed = saved.filter(value => typeof value === "string"); } catch {}
        if (current.current.state?.stage !== data.stage) setPosition(0);
        setState({ ventureId: id, owner, epoch, stage: data.stage, items: data.items, dismissed });
        setError(false);
      } catch { if (!stopped && reads === ticket) { setState(null); setError(true); } }
      finally { if (!stopped && reads === ticket) setLoading(false); }
    };
    const reset = () => { void load(true); };
    const changed = (event: Event) => { if ((event as CustomEvent<{ ventureId?: string }>).detail?.ventureId === id) reset(); };
    reset();
    const timer = setInterval(() => { if (!document.hidden) void load(); }, 15000);
    window.addEventListener("supymem-venture-changed", changed);
    window.addEventListener("supymem-session-changed", reset);
    return () => { stopped = true; live.current = false; clearInterval(timer); window.removeEventListener("supymem-venture-changed", changed); window.removeEventListener("supymem-session-changed", reset); };
  }, [id, visible, pathname, refresh]);
  if (!visible) return null;
  const owned = state !== null && state.ventureId === id && state.owner === verifiedSessionSubject() && state.epoch === verifiedSessionEpoch();
  const items = owned && state ? state.items.filter(item => !state.dismissed.includes(item.id)) : [];
  const item = items[position % Math.max(items.length, 1)];
  const active = () => live.current && current.current.id === id && current.current.pathname === pathname && current.current.state === state
    && (!state || (state.owner === verifiedSessionSubject() && state.epoch === verifiedSessionEpoch()));
  return <CoachingView item={item} loading={loading} error={error} onMore={() => {
    if (!active()) return;
    if (items.length > 1) setPosition(position + 1); else setRefresh(refresh + 1);
  }} onDismiss={() => {
    if (!active() || !owned || !item || !state) return;
    const dismissed = [...state.dismissed, item.id];
    try { localStorage.setItem(coachingDismissalKey(state.owner, state.ventureId, state.stage), JSON.stringify(dismissed)); } catch {}
    setState({ ...state, dismissed }); setPosition(0);
  }} />;
}
