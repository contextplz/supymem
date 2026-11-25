"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { SuggestionCard } from "@/components/suggestions/SuggestionCard";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { api, ensureSession, verifiedSessionEpoch } from "@/lib/client";
import { serverReason } from "@/lib/serverReason";
import { SUGGESTIONS_CHANGED, scopedSuggestions, suggestionsPath, statusLabel, type Decision, type Suggestion, type SuggestionList, type SuggestionsView } from "@/lib/suggestions";
import styles from "@/components/suggestions/suggestions.module.css";

export default function SuggestionsPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const [open, setOpen] = useState<Suggestion[] | null>(null);
  const [history, setHistory] = useState<Suggestion[]>([]);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [error, setError] = useState("");
  const [choice, setChoice] = useState<{ ventureId: string; view: SuggestionsView } | null>(null);
  const view: SuggestionsView = choice?.ventureId === ventureId ? choice.view : "venture";
  const [loadedScope, setLoadedScope] = useState<{ ventureId: string; view: SuggestionsView; epoch: number } | null>(null);
  const [waiting, setWaiting] = useState(0);
  const [nextOpen, setNextOpen] = useState<number | null>(null);
  const [nextHistory, setNextHistory] = useState<number | null>(null);
  const scope = useRef({ ventureId, view });
  scope.current = { ventureId, view };
  const generation = useRef(0);
  const deciding = useRef(new Set<string>());

  const current = useCallback((owner: { ventureId: string; view: SuggestionsView; epoch: number }, ticket: number) =>
    ticket === generation.current && owner.ventureId === scope.current.ventureId && owner.view === scope.current.view
      && owner.epoch === verifiedSessionEpoch(), []);

  const load = useCallback(async () => {
    const ticket = ++generation.current;
    await ensureSession();
    const owner = { ventureId, view, epoch: verifiedSessionEpoch() };
    if (!current(owner, ticket)) return;
    const [a, b] = await Promise.all([api(suggestionsPath(ventureId, view)), api(suggestionsPath(ventureId, view, true))]);
    if (!current(owner, ticket)) return;
    if (a.ok) {
      const data = a.data as SuggestionList;
      setOpen(scopedSuggestions(data.items || [], ventureId, view));
      setWaiting(data.new + data.saved);
      setNextOpen(data.next_offset ?? null);
      setLoadedScope(owner);
      setError("");
    } else {
      setOpen(null);
      setLoadedScope(null);
      setError(serverReason(a, "Couldn’t load your suggestions."));
    }
    setHistory(b.ok ? scopedSuggestions((b.data as SuggestionList).items || [], ventureId, view) : []);
    setNextHistory(b.ok ? (b.data as SuggestionList).next_offset ?? null : null);
  }, [ventureId, view, current]);

  useEffect(() => {
    const retire = () => {
      ++generation.current;
      setOpen(null); setHistory([]); setBusy({}); setError(""); setLoadedScope(null); setWaiting(0);
      deciding.current.clear();
      void load();
    };
    deciding.current.clear();
    setBusy({}); setError("");
    window.addEventListener("supymem-session-changed", retire);
    void load();
    return () => { ++generation.current; window.removeEventListener("supymem-session-changed", retire); };
  }, [load]);

  useEffect(() => {
    if (!history.some(item => (item.result_state || item.status) === "running")) return;
    const timer = window.setInterval(() => void load(), 10_000);
    return () => window.clearInterval(timer);
  }, [history, load]);

  async function refresh() {
    const owner = { ventureId, view, epoch: verifiedSessionEpoch() }, ticket = generation.current;
    setError("");
    const r = await api(`/api/ventures/${encodeURIComponent(ventureId)}/suggestions/refresh`, { method: "POST" });
    if (!current(owner, ticket)) return;
    if (!r.ok) setError(serverReason(r, "Couldn’t look for new suggestions."));
    await load();
    window.dispatchEvent(new Event(SUGGESTIONS_CHANGED));
  }

  async function decide(id: string, decision: Decision) {
    const owner = loadedScope, ticket = generation.current;
    const item = (open || []).find(i => i.id === id);
    if (!owner || !current(owner, ticket) || !item || item.venture_id !== ventureId || deciding.current.has(id)) return;
    deciding.current.add(id);
    setBusy((b) => ({ ...b, [id]: true }));
    setError("");
    try {
      const r = await api(`/api/suggestions/${encodeURIComponent(id)}/decision`, { method: "POST", body: JSON.stringify({ decision }) });
      if (!current(owner, ticket)) return;
      if (!r.ok) {
        setError(serverReason(r, "That did not go through. Refresh to check the saved status."));
        return;
      }
      await load();
      window.dispatchEvent(new Event(SUGGESTIONS_CHANGED));
    } catch {
      if (current(owner, ticket)) setError("Could not finish the request. Refresh to check its saved outcome.");
    } finally {
      deciding.current.delete(id);
      setBusy((b) => { const next = { ...b }; delete next[id]; return next; });
    }
  }

  async function more(isHistory: boolean) {
    const offset = isHistory ? nextHistory : nextOpen;
    const owner = loadedScope, ticket = generation.current;
    if (offset === null || !owner) return;
    const path = suggestionsPath(ventureId, view, isHistory);
    const response = await api(`${path}${path.includes("?") ? "&" : "?"}offset=${offset}`);
    if (!current(owner, ticket)) return;
    if (!response.ok) { setError(serverReason(response, "Could not load more suggestions.")); return; }
    const data = response.data as SuggestionList;
    const items = scopedSuggestions(data.items || [], ventureId, view);
    if (isHistory) { setHistory(rows => [...rows, ...items.filter(item => !rows.some(row => row.id === item.id))]); setNextHistory(data.next_offset ?? null); }
    else { setOpen(rows => [...(rows || []), ...items.filter(item => !rows?.some(row => row.id === item.id))]); setNextOpen(data.next_offset ?? null); }
  }

  const visible = loadedScope && current(loadedScope, generation.current) ? open : null;
  const saved = (visible || []).filter((i) => i.status === "saved");
  const fresh = (visible || []).filter((i) => i.status === "new");
  const visibleHistory = visible !== null ? history : [];
  return (
    <AppShell title="Suggestions" ventureId={ventureId}>
      <div className={styles.pageList}>
        <p className="muted">Review proposed work. Nothing runs until you say go ahead; work may still need your input or a separate approval.</p>
        <div role="group" aria-label="Suggestion scope">
          <button type="button" data-usage-control="suggestions_scope_venture" aria-pressed={view === "venture"} onClick={() => setChoice({ ventureId, view: "venture" })}>This venture</button>
          <button type="button" data-usage-control="suggestions_scope_all" aria-pressed={view === "all"} onClick={() => setChoice({ ventureId, view: "all" })}>All my ventures</button>
        </div>
        <div><button type="button" data-usage-control="suggestions_refresh" onClick={() => void refresh()}>Look for new suggestions for this venture</button></div>
        <ErrorNotice error={error} source="suggestions.page.error" />
        {visible === null && !error ? <p role="status">Loading…</p> : null}
        {visible !== null && waiting > visible.length ? <p className="muted">Showing the latest {visible.length} of {waiting} waiting suggestions.</p> : null}
        {visible !== null && fresh.length + saved.length === 0 ? <p className="muted">{view === "venture" ? "Nothing waiting for this venture." : "Nothing waiting across your ventures."}</p> : null}
        {fresh.length ? <h2>New</h2> : null}
        {fresh.map((i) => <SuggestionCard key={i.id} item={i} busy={busy[i.id]} onDecide={(id, d) => void decide(id, d)} showVenture={view === "all"} reviewHref={i.venture_id !== ventureId ? `/v/${encodeURIComponent(i.venture_id)}/suggestions` : undefined} />)}
        {saved.length ? <h2>Saved for later</h2> : null}
        {saved.map((i) => <SuggestionCard key={i.id} item={i} busy={busy[i.id]} hideSave onDecide={(id, d) => void decide(id, d)} showVenture={view === "all"} reviewHref={i.venture_id !== ventureId ? `/v/${encodeURIComponent(i.venture_id)}/suggestions` : undefined} />)}
        {visibleHistory.length ? <h2>Recent history</h2> : null}
        {nextOpen !== null && visible !== null ? <button type="button" data-usage-control="suggestions_more_open" onClick={() => void more(false)}>More suggestions</button> : null}
        {visibleHistory.map((i) => (
          <div key={i.id} className={styles.history}><span>{view === "all" ? `${i.venture_name}: ` : ""}{i.title}</span><span>{i.result_message || statusLabel(i.result_state || i.status, i.result_ref)}</span><a data-usage-control="suggestions_result" href={i.result_href || `/v/${encodeURIComponent(i.venture_id)}`}>Open result or progress</a></div>
        ))}
        {nextHistory !== null ? <button type="button" data-usage-control="suggestions_more_history" onClick={() => void more(true)}>More history</button> : null}
      </div>
    </AppShell>
  );
}
