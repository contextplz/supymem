"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { CopyDraft } from "@/components/gtm/OutreachDraft";
import { api, ensureSession, verifiedSessionEpoch } from "@/lib/client";
import { safeEvidenceUrl } from "@/lib/gtmJourneyApi";
import { serverReason } from "@/lib/serverReason";
import { prefillChat } from "@/lib/chatPrefill";
import styles from "@/components/gtm/GtmJourney.module.css";

type Message = { playbook: string; title: string; connection_note: string; linkedin_dm: string };
type Person = { id: string; url: string; name: string; headline: string; company: string; location?: string; snippet: string; why: string;
  found_on: string; sent_at: string | null; replied_at: string | null; messages: Message[] };
type SearchLink = { name: string; role: string; company: string; source_url: string; linkedin_search: string };
type View = { mac_connected: boolean; notice: string | null; needs_human: string | null; searching: boolean; queued_at?: string | null;
  exa_configured: boolean; has_audience: boolean; searches_today: number; search_limit: number; next_search_at: string | null;
  today: { new: number; target: number }; people: Person[]; fallback: SearchLink[];
  last_search: { state?: string; message?: string } | null };

function queuedLabel(exa: boolean, queuedAt?: string | null): string {
  const label = exa ? "Searching in the background" : "Searching on your Mac";
  const at = queuedAt ? new Date(queuedAt) : null;
  return at && !Number.isNaN(at.getTime())
    ? `${label}. Queued at ${at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}.`
    : `${label}.`;
}

/** Public profiles found in the background. Supymem never opens LinkedIn; the founder sends by hand. */
export default function LinkedInPeoplePage() {
  const ventureId = String(useParams().ventureId || "");
  const local = useLocalMode();
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const generation = useRef(0);
  const actionPending = useRef(false);
  const path = `/api/ventures/${encodeURIComponent(ventureId)}/linkedin`;
  const owner = useRef({ ventureId, epoch: 0 });
  const current = useCallback(() => owner.current.ventureId === ventureId && owner.current.epoch === verifiedSessionEpoch(), [ventureId]);

  const apply = useCallback((result: Awaited<ReturnType<typeof api>>, ticket: number) => {
    if (ticket !== generation.current || !current()) return;
    if (result.ok) { setView(result.data as View); setError(""); }
    else setError(serverReason(result, "Select Try again on LinkedIn to load your saved profiles."));
  }, [current]);

  const load = useCallback(async () => {
    const ticket = ++generation.current;
    setLoading(true); setError("");
    try {
      await ensureSession();
      if (ticket !== generation.current) return;
      owner.current = { ventureId, epoch: verifiedSessionEpoch() };
      apply(await api(path), ticket);
    } catch {
      if (ticket === generation.current && current()) setError("Couldn’t load your LinkedIn people. Try again.");
    } finally {
      if (ticket === generation.current && current()) setLoading(false);
    }
  }, [apply, current, path, ventureId]);

  useEffect(() => {
    setView(null); setError("");
    void load();
    window.addEventListener("supymem-session-changed", load);
    return () => { ++generation.current; window.removeEventListener("supymem-session-changed", load); };
  }, [load]);

  useEffect(() => {
    if (!view?.searching) return;
    let pending = false;
    const timer = window.setInterval(async () => {
      if (pending) return;
      pending = true;
      try { await load(); } finally { pending = false; }
    }, 3_000);
    return () => window.clearInterval(timer);
  }, [view?.searching, load]);

  async function send(suffix: string, method: string, body?: unknown) {
    if (busy || actionPending.current || !current()) return;
    actionPending.current = true;
    setBusy(true);
    setLoading(false);
    const ticket = ++generation.current;
    try {
      apply(await api(path + suffix, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }), ticket);
    } catch {
      if (ticket === generation.current && current()) setError("Select Try again on LinkedIn, then retry your action.");
    } finally { actionPending.current = false; if (ticket === generation.current && current()) setBusy(false); }
  }

  return (
    <AppShell title="LinkedIn" ventureId={ventureId}>
      <div className={styles.wrap}>
        <p className={styles.muted}>{local
          ? "Public-web profile discovery and manual drafts only. You choose when to search. You open each public LinkedIn profile and send the draft yourself. There is no automated messaging in this version. Built-in search works without a key. For other search options, open Settings > Keys and connections > Your search and reader."
          : view?.exa_configured
          ? "People who fit your customer, found in the background from public profiles. Supymem never opens, messages or automates LinkedIn: open a profile and send the note yourself."
          : "People who fit your customer, found from Google results read in your own browser on your Mac. Supymem never opens or messages LinkedIn: open each profile and send the note yourself."}</p>
        <ErrorNotice error={error} source="linkedin_people" />
        {error && <button type="button" className={styles.button} disabled={busy || loading} onClick={() => void load()}>Try again</button>}
        {!view && !error && <p role="status">Loading your LinkedIn people…</p>}
        <div className={styles.row}>
          {view && <span>{Math.min(view.today.new, view.today.target)} of {view.today.target} new people today</span>}
          {view?.next_search_at ? <p role="status">Today&apos;s {view.search_limit} searches are used. Next search can run at {new Date(view.next_search_at).toLocaleString()}.</p>
            : <button type="button" className={styles.button} data-usage-control="gtm_journey_find_customers" disabled={!view || busy || view.searching || !view.has_audience} onClick={() => void send("/find", "POST")}>
              Find more people
            </button>}
        </div>
        {view && <>
          {!view.has_audience && <div className={styles.row}>
            <p className={styles.muted}>Set your audience so Charlie knows whom to find.</p>
            <button type="button" className={`${styles.button} ${styles.secondary}`} data-usage-control="linkedin_set_audience" onClick={() => { if (current()) prefillChat("Charlie, help me set the audience for this venture so we can find LinkedIn people."); }}>Set audience with Charlie</button>
          </div>}
          {view.searching && <p role="status">{queuedLabel(local || view.exa_configured, view.queued_at)}</p>}
          {view.notice && <p role="status">{view.notice}</p>}
          {view.needs_human && <p role="status">{view.needs_human}</p>}
          {(view.last_search?.state === "failed" || view.last_search?.state === "done") && view.last_search.message && <p role="status">{view.last_search.message}</p>}
          {!!view.people.length && <ul className={styles.prospects}>{view.people.map(person => <PersonCard key={person.id} person={person} disabled={busy} current={current}
            mark={(change) => void send(`/people/${encodeURIComponent(person.id)}`, "PATCH", change)} />)}</ul>}
          {!view.people.length && view.has_audience && !view.searching && !view.last_search?.message && <p className={styles.muted}>You have no saved profiles yet. Select Find more people to search{local ? " the public web." : view.exa_configured ? " in the background." : " on your Mac."}</p>}
          {!view.mac_connected && !!view.fallback.length && <section>
            <h3>Decision-makers found on the public web</h3>
            <p className={styles.muted}>These are LinkedIn search links, not profiles. Open one to find the right person yourself.</p>
            <ul className={styles.prospects}>{view.fallback.map(person => <li key={`${person.name}:${person.company}`} className={styles.prospect}>
              <strong>{person.name}</strong> · {person.role}{person.company && ` · ${person.company}`}
              <div className={styles.row}>
                {safeEvidenceUrl(person.linkedin_search) && <a href={safeEvidenceUrl(person.linkedin_search)} target="_blank" rel="noopener noreferrer">Search LinkedIn</a>}
                {safeEvidenceUrl(person.source_url) && <a href={safeEvidenceUrl(person.source_url)} target="_blank" rel="noopener noreferrer">Source</a>}
              </div>
            </li>)}</ul>
          </section>}
        </>}
      </div>
    </AppShell>
  );
}

function PersonCard({ person, disabled, current, mark }: { person: Person; disabled: boolean; current: () => boolean;
  mark: (change: { sent?: boolean; replied?: boolean }) => void }) {
  const profile = safeEvidenceUrl(person.url);
  const message = person.messages[0];
  const detail = [person.headline, person.company && !person.headline?.toLowerCase().includes(person.company.toLowerCase()) ? person.company : "", person.location].filter(Boolean).join(" · ");
  return <li className={styles.prospect}>
    <strong>{person.name || "LinkedIn member"}</strong>
    {detail && <p className={styles.muted}>{detail}</p>}
    <p>{person.why}</p>
    <div className={styles.row}>
      {profile && <a href={profile} target="_blank" rel="noopener noreferrer" data-usage-control="gtm_reach_linkedin">{person.url}</a>}
      <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled} aria-pressed={!!person.sent_at} onClick={() => mark({ sent: !person.sent_at })}>{person.sent_at ? "Sent ✓" : "Mark sent"}</button>
      <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled} aria-pressed={!!person.replied_at} onClick={() => mark({ replied: !person.replied_at })}>{person.replied_at ? "Replied ✓" : "Replied"}</button>
    </div>
    {message && <>
      <CopyDraft title={`Connection note · ${message.title}`} buttonLabel="Copy note" text={message.connection_note} disabled={disabled} current={current} />
      <CopyDraft title="Follow-up DM" buttonLabel="Copy DM" text={message.linkedin_dm} disabled={disabled} current={current} />
    </>}
    {person.messages.slice(1).map(other => <details key={other.playbook}><summary>{other.title}: note and DM</summary>
      <CopyDraft title="Connection note" buttonLabel="Copy note" text={other.connection_note} disabled={disabled} current={current} />
      <CopyDraft title="Follow-up DM" buttonLabel="Copy DM" text={other.linkedin_dm} disabled={disabled} current={current} />
    </details>)}
  </li>;
}
