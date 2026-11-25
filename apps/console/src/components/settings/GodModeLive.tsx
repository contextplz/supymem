"use client";

import { ChatAnchor } from "@/components/chat/ChatLink";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, verifiedSessionSubject, verifiedSessionEpoch } from "@/lib/client";
import { godModeOutcomeLabel } from "@/lib/godModeOutcome";
import { serverReason } from "@/lib/serverReason";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { GOD_MODE_EVENT } from "./GodModePanel";
import { godModeStatusText } from "@/lib/godModeStatus";
import { createGodModeRequestFence, sameGodModeScope, type GodModeReceipt, type GodModeRequest } from "@/lib/godModeRequestScope";

type Status = { state: string; working_on: string; waiting_on_you: { title: string; why: string; href: string }[] };
type Entry = { id: string; what: string; outcome: string; display_outcome?: string; completion_verified?: boolean; at: string };
type Pmf = { n: number; variable: string; change: string; metric: string; baseline: number; measured: number; target: number; state: string; decision: string | null; metric_qualified?: boolean };
type Brief = {
  now: string; now_detail: string; next: string[]; learned: string[]; more_waiting: number;
  waiting_on_you: { title: string; why: string; how: string }[];
  recent: { at: string; kind: string; text: string }[];
};
type Live = { god_mode: boolean; warning: string; floors: string[]; status: Status; timeline: Entry[]; pmf_journal: Pmf[]; brief?: Brief };

const DISMISS = "supymem-god-offer-dismissed";
const dismissed = () => { try { return localStorage.getItem(DISMISS) === "1"; } catch { return false; } };

/** Offer God mode when off; report the current observed activity when enabled. */
export function GodModeLive({ ventureId }: { ventureId: string }) {
  const [receipt, setReceipt] = useState<GodModeReceipt<Live> | null>(null);
  const active = useRef(ventureId); active.current = ventureId;
  const fence = useRef(createGodModeRequestFence());
  const mutation = useRef<GodModeRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const scope = () => ({ venture: active.current, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch() });
  const data = sameGodModeScope(receipt?.scope || null, scope()) ? receipt!.data : null;
  const [confirming, setConfirming] = useState(false);
  const [open, setOpen] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    if (mutation.current) return;
    const request = fence.current.begin({ venture: ventureId, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch() });
    const current = () => fence.current.current(request, { venture: active.current, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch() });
    try {
      const r = await api(`/api/ventures/${encodeURIComponent(ventureId)}/autopilot`);
      if (!current()) return;
      if (r.ok) { setReceipt({ scope: request, data: r.data as Live }); setLoadError(""); }
      else setLoadError("Could not check God mode. Refresh saved status before changing settings.");
    } catch {
      if (current()) setLoadError("Could not check God mode. Refresh saved status before changing settings.");
    }
  }, [ventureId]);

  useEffect(() => {
    const reset = () => {
      fence.current.invalidate(); mutation.current = null;
      setReceipt(null); setLoadError(""); setError(""); setBusy(false); setConfirming(false); setOpen(false);
      setHidden(dismissed()); void load();
    };
    reset();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 15000);
    const onChange = () => void load();
    window.addEventListener(GOD_MODE_EVENT, onChange);
    window.addEventListener("supymem-session-changed", reset);
    return () => {
      fence.current.invalidate(); window.clearInterval(timer);
      window.removeEventListener(GOD_MODE_EVENT, onChange);
      window.removeEventListener("supymem-session-changed", reset);
    };
  }, [load]);

  async function turnOn() {
    if (mutation.current || !data || loadError || !sameGodModeScope(receipt?.scope || null, scope())) return;
    const request = fence.current.begin(scope());
    mutation.current = request; setBusy(true); setError("");
    const current = () => fence.current.current(request, scope());
    try {
      const r = await api(`/api/ventures/${encodeURIComponent(request.venture)}/autopilot`, { method: "PUT", body: JSON.stringify({ god_mode: true, acknowledged_beta: true }) });
      if (!current()) return;
      if (!r.ok) {
        setError(serverReason(r, "God mode's saved setting could not be confirmed. Check saved status before trying again."));
        setLoadError("God mode's saved setting could not be confirmed. Check saved status before changing it again.");
        setConfirming(false); return;
      }
      setReceipt({ scope: request, data: r.data as Live }); setLoadError(""); setConfirming(false);
      window.dispatchEvent(new CustomEvent(GOD_MODE_EVENT, { detail: true }));
    } catch {
      if (current()) {
        setLoadError("God mode's saved setting could not be confirmed. Check saved status before changing it again.");
        setConfirming(false);
      }
    } finally {
      if (mutation.current === request) { mutation.current = null; if (current()) setBusy(false); }
    }
  }

  const retry = loadError ? <>
    <ErrorNotice error={loadError} source="god_mode.status" intent="check God mode status" />
    <button type="button" onClick={() => void load()}>Try again</button>
    <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/settings#god-mode`}>Open God mode settings</ChatAnchor>
  </> : null;
  if (!data) return <section className="panel-card" aria-label="God mode" style={{ margin: "8px 0" }}>
    {retry || <p role="status">Checking God mode…</p>}
  </section>;
  if (!data.god_mode) {
    if (hidden && !loadError) return null;
    return (
      <section className="panel-card" aria-label="God mode" style={{ margin: "8px 0" }}>
        {retry}
        <h3>Let Supymem do it for you (God mode, beta)</h3>
        <p className="muted">
          Your requested ongoing work can take the next eligible step. Check its recorded activity,
          and respond when it needs your help. You can turn it off any time.
        </p>
        <p><strong>{data.warning}</strong></p>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button className="primary" type="button" data-usage-control="settings_god_mode_toggle" disabled={busy || Boolean(loadError)} onClick={() => setConfirming(true)}>Turn on God mode</button>
          <button type="button" onClick={() => { try { localStorage.setItem(DISMISS, "1"); } catch { /* ignore */ } setHidden(true); }}>Not now</button>
        </div>
        {confirming && (
          <div role="dialog" aria-modal="true" aria-labelledby="god-offer-title"
            style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.5)", display: "grid", placeItems: "center", zIndex: 1000 }}>
            <div className="panel-card" style={{ maxWidth: 480, margin: 16 }}>
              <h3 id="god-offer-title">Turn on God mode (beta)?</h3>
              <p><strong>{data.warning}</strong></p>
              <ul className="muted">{data.floors.map((f) => <li key={f}>{f}</li>)}</ul>
              <ErrorNotice error={error} source="god_mode.offer" />
              <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                <button className="primary" type="button" data-usage-control="settings_god_mode_toggle" disabled={busy || Boolean(loadError)} onClick={() => void turnOn()}>I understand, turn it on</button>
                <button type="button" onClick={() => setConfirming(false)}>Cancel</button>
              </div>
            </div>
          </div>
        )}
      </section>
    );
  }
  const { status } = data;
  if (data.brief) return <Cofounder ventureId={ventureId} data={data} brief={data.brief} loadError={loadError} retry={retry} open={open} setOpen={setOpen} />;
  return (
    <section className="panel-card" aria-label="God mode activity" style={{ margin: "8px 0" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <strong role="status">{loadError ? "God mode's latest activity could not be confirmed." : godModeStatusText(status)}</strong>
        <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "Hide" : "What it did"}</button>
        <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/settings#god-mode`}>Settings</ChatAnchor>
      </div>
      {retry}
      {status.waiting_on_you.map((w) => (
        <p key={w.title} style={{ marginTop: 8 }}>
          <strong>Needs you:</strong> {w.title}. <span className="muted">{w.why}</span>{" "}
          {w.href && <ChatAnchor href={w.href}>Open</ChatAnchor>}
        </p>
      ))}
      {open && (
        <div style={{ marginTop: 8 }}>
          <h4>Timeline</h4>
          <ol style={{ paddingLeft: 18 }}>
            {data.timeline.slice(0, 8).map((e) => (
              <li key={e.id}>{e.what} <span className="muted">· {godModeOutcomeLabel(e)} · {new Date(e.at).toLocaleString()}</span></li>
            ))}
          </ol>
          {data.pmf_journal.length > 0 && (
            <>
              <h4>Experiment journal</h4>
              <ol style={{ paddingLeft: 18 }}>
                {data.pmf_journal.slice(0, 6).map((p) => (
                  <li key={p.n}>
                    #{p.n} {p.variable}: {p.change}{" "}
                    <span className="muted">· {p.metric_qualified === true ? `${p.metric.replace(/_/g, " ")} ${p.baseline} to ${p.measured} (target ${p.target}) · ${(p.decision || p.state).toUpperCase()}` : "Historical measurement unqualified, review needed"}</span>
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>
      )}
    </section>
  );
}

/** The status strip read as a cofounder at work: now doing / next / the one thing waiting on you / what it learned. */
function Cofounder({ ventureId, data, brief, loadError, retry, open, setOpen }: {
  ventureId: string; data: Live; brief: Brief; loadError: string; retry: React.ReactNode; open: boolean; setOpen: (v: boolean) => void;
}) {
  const ask = brief.waiting_on_you[0];
  return (
    <section className="panel-card" aria-label="God mode activity" style={{ margin: "8px 0" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <strong role="status">{loadError ? "God mode's latest activity could not be confirmed." : brief.now}</strong>
        <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "Hide" : "What it did"}</button>
        <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/settings#god-mode`}>Settings</ChatAnchor>
      </div>
      {retry}
      {brief.now_detail && <p className="muted" style={{ margin: "4px 0" }}>{brief.now_detail}</p>}
      {brief.next.length > 0 && <p style={{ margin: "4px 0" }}><strong>Next:</strong> {brief.next.join(", then ")}.</p>}
      {ask ? (
        <p style={{ margin: "4px 0" }}>
          <strong>Waiting on you (1 thing):</strong> {ask.title}. <span className="muted">{ask.why}</span>{" "}
          <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/now`}>Open</ChatAnchor>
          {brief.more_waiting > 0 && <span className="muted"> · {brief.more_waiting} more after this</span>}
        </p>
      ) : <p className="muted" style={{ margin: "4px 0" }}>Nothing needs you right now.</p>}
      {brief.learned.length > 0 && (
        <div style={{ marginTop: 4 }}>
          <strong>What I learned</strong>
          <ul style={{ paddingLeft: 18, margin: "2px 0" }}>{brief.learned.map((l) => <li key={l}>{l}</li>)}</ul>
        </div>
      )}
      {open && (
        <div style={{ marginTop: 8 }}>
          <h4>What I did lately</h4>
          {brief.recent.length === 0 ? <p className="muted">Nothing yet.</p> : (
            <ol style={{ paddingLeft: 18 }}>
              {brief.recent.map((r, i) => (
                <li key={`${r.at}-${i}`}>{r.text} <span className="muted">· {new Date(r.at).toLocaleString()}</span></li>
              ))}
            </ol>
          )}
          {data.pmf_journal.length > 0 && (
            <>
              <h4>Experiment journal</h4>
              <ol style={{ paddingLeft: 18 }}>
                {data.pmf_journal.slice(0, 6).map((p) => (
                  <li key={p.n}>
                    #{p.n} {p.variable}: {p.change}{" "}
                    <span className="muted">· {p.metric_qualified === true ? `${p.metric.replace(/_/g, " ")} ${p.baseline} to ${p.measured} (target ${p.target}) · ${(p.decision || p.state).toUpperCase()}` : "Historical measurement unqualified, review needed"}</span>
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>
      )}
    </section>
  );
}
