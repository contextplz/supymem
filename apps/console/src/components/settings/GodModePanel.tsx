"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, verifiedSessionSubject, verifiedSessionEpoch } from "@/lib/client";
import { godModeOutcomeLabel } from "@/lib/godModeOutcome";
import { serverReason } from "@/lib/serverReason";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { createGodModeRequestFence, sameGodModeScope, type GodModeReceipt, type GodModeRequest } from "@/lib/godModeRequestScope";

export const GOD_MODE_EVENT = "supymem-god-mode";

type Entry = { id: string; action: string; what: string; why: string; outcome: string; display_outcome?: string; completion_verified?: boolean; credits: number; at: string };
type Autopilot = {
  god_mode: boolean;
  spent_today_credits: number;
  allow_send: boolean;
  allow_mac: boolean;
  continue_when_offline?: boolean;
  status?: { state: string; working_on: string };
  send_max_per_day: number;
  floors: string[];
  warning: string;
  timeline: Entry[];
};


export function GodModePanel({ ventureId }: { ventureId: string }) {
  const [receipt, setReceipt] = useState<GodModeReceipt<Autopilot> | null>(null);
  const saved = useRef<GodModeReceipt<Autopilot> | null>(null);
  const active = useRef(ventureId); active.current = ventureId;
  const fence = useRef(createGodModeRequestFence());
  const mutation = useRef<GodModeRequest | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [sendMax, setSendMax] = useState("0");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [loadError, setLoadError] = useState("");
  const scope = () => ({ venture: active.current, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch() });
  const data = sameGodModeScope(receipt?.scope || null, scope()) ? receipt!.data : null;

  const apply = useCallback((next: Autopilot, request: GodModeRequest, fromSave = false) => {
    const nextReceipt = { scope: { venture: request.venture, owner: request.owner, epoch: request.epoch }, data: next };
    // Polling updates saved status, while preserving an unsaved founder draft.
    if (fromSave || !sameGodModeScope(saved.current?.scope || null, nextReceipt.scope)) {
      setSendMax(String(next.send_max_per_day));
    }
    saved.current = nextReceipt;
    setReceipt(nextReceipt);
    setLoadError("");
    if (fromSave) window.dispatchEvent(new CustomEvent(GOD_MODE_EVENT, { detail: next.god_mode }));
  }, []);

  const load = useCallback(async () => {
    if (mutation.current) return;
    const request = fence.current.begin({ venture: ventureId, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch() });
    const current = () => fence.current.current(request, { venture: active.current, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch() });
    try {
      const r = await api(`/api/ventures/${encodeURIComponent(ventureId)}/autopilot`);
      if (!current()) return;
      if (r.ok) apply(r.data as Autopilot, request);
      else setLoadError("God mode settings could not be checked. Refresh saved status before changing them.");
    } catch {
      if (current()) setLoadError("God mode settings could not be checked. Refresh saved status before changing them.");
    }
  }, [ventureId, apply]);

  useEffect(() => {
    const reset = () => {
      fence.current.invalidate(); mutation.current = null; saved.current = null;
      setReceipt(null); setBusy(false); setConfirming(false); setNote(""); setLoadError("");
      setSendMax("0"); void load();
    };
    reset();
    const t = window.setInterval(() => { if (!document.hidden) void load(); }, 15000);
    window.addEventListener("supymem-session-changed", reset);
    return () => { fence.current.invalidate(); window.clearInterval(t); window.removeEventListener("supymem-session-changed", reset); };
  }, [load]);

  async function save(body: Record<string, unknown>, done: string) {
    if (mutation.current || !data || loadError || !sameGodModeScope(receipt?.scope || null, scope())) return;
    const request = fence.current.begin(scope());
    mutation.current = request; setBusy(true); setNote("");
    const current = () => fence.current.current(request, scope());
    try {
      const r = await api(`/api/ventures/${encodeURIComponent(request.venture)}/autopilot`, { method: "PUT", body: JSON.stringify(body) });
      if (!current()) return;
      if (r.ok) { apply(r.data as Autopilot, request, true); setNote(done); setConfirming(false); }
      else {
        setNote(serverReason(r, "The save was not confirmed. Refresh saved status before trying again."));
        setLoadError("The save was not confirmed. Refresh saved status before changing settings again.");
        setConfirming(false);
      }
    } catch {
      if (current()) {
        setNote("The save was not confirmed. Refresh saved status before trying again.");
        setLoadError("The save was not confirmed. Refresh saved status before changing settings again.");
        setConfirming(false);
      }
    } finally {
      if (mutation.current === request) { mutation.current = null; if (current()) setBusy(false); }
    }
  }

  const recovery = loadError ? <>
    <ErrorNotice error={loadError} source="god_mode.settings" intent="check saved God mode settings" />
    <button type="button" disabled={busy} onClick={() => void load()}>Refresh saved status</button>
  </> : null;
  if (!data) return <div className="panel-card" id="god-mode" aria-label="God mode settings">
    <h3>God mode (beta)</h3>{recovery || <p role="status">Checking God mode settings…</p>}
  </div>;
  const on = data.god_mode;
  return (
    <div className="panel-card" id="god-mode">
      <h3>God mode (beta)</h3>
      {recovery}
      <p className="muted">
        Your requested ongoing work can take the next eligible step. Review its recorded actions and results below.
        Required input is shown when your help is needed. You can turn it off any time.
      </p>
      <p className="muted">
        God mode runs in the cloud and pauses when your Mac has been offline for 10 minutes, unless you turn on
        "Keep working while my Mac is off". Turn it off here to stop new autonomous steps; work already running may finish.
      </p>
      <div style={{ display: "flex", gap: 8, marginTop: 12, alignItems: "center", flexWrap: "wrap" }}>
        {on ? (
          <button className="danger" type="button" data-usage-control="settings_god_mode_toggle" disabled={busy || Boolean(loadError)}
            onClick={() => void save({ god_mode: false }, "God mode is off. New autonomous steps are disabled; existing work may still finish.")}>Turn off God mode</button>
        ) : (
          <button className="primary" type="button" data-usage-control="settings_god_mode_toggle" disabled={busy || Boolean(loadError)}
            onClick={() => setConfirming(true)}>Turn on God mode</button>
        )}
        <span role="status" className="muted">{note || (on ? "God mode is on" : "God mode is off")}</span>
        {on && data.status?.state === "mac_offline" && <span role="status" className="muted">{data.status.working_on}</span>}
      </div>

      <h4 style={{ marginTop: 16 }}>Safety limits that always stay on</h4>
      <ul className="muted">{data.floors.map((f) => <li key={f}>{f}</li>)}</ul>
      <form style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}
        onSubmit={(e) => {
          e.preventDefault();
          const n = Number(sendMax);
          void save({ allow_send: n > 0, send_max_per_day: n }, n > 0 ? "Sending allowed up to the daily max." : "Sending is off. Drafts only.");
        }}>
        <label htmlFor="god-send">Messages it may send per day (0 = drafts only)</label>
        <input id="god-send" type="number" min={0} max={1000} disabled={busy || Boolean(loadError)} value={sendMax} onChange={(e) => setSendMax(e.target.value)} style={{ width: 90 }} />
        <button type="submit" data-usage-control="settings_god_mode_save" disabled={busy || Boolean(loadError)}>Save</button>
        <span className="muted">{data.allow_send ? `Up to ${data.send_max_per_day} a day` : "Off: drafts only (default)"}</span>
      </form>

      <label style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
        <input type="checkbox" checked={false} disabled />
        Allow Mac actions: coming soon in beta. God mode works in the cloud and never controls your Mac.
      </label>

      <label style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
        <input type="checkbox" id="god-offline" checked={Boolean(data.continue_when_offline)} disabled={busy || Boolean(loadError)}
          onChange={(e) => void save({ continue_when_offline: e.target.checked }, e.target.checked ? "It keeps working while your Mac is off." : "It pauses when your Mac is offline.")} />
        Keep working while my Mac is off
      </label>

      <h4 style={{ marginTop: 16 }}>Saved autonomous activity</h4>
      {data.timeline.length === 0 ? (
        <p className="muted">Nothing recorded yet. Ask for work, then return to see its saved outcome.</p>
      ) : (
        <ol style={{ paddingLeft: 18, marginTop: 8 }}>
          {data.timeline.map((e) => (
            <li key={e.id} style={{ marginBottom: 8 }}>
              <strong>{e.what}</strong> <span className="muted">· {godModeOutcomeLabel(e)}
                {" · "}{new Date(e.at).toLocaleString()}</span>
              {e.why && <div className="muted">{e.why}</div>}
            </li>
          ))}
        </ol>
      )}

      {confirming && (
        <div role="dialog" aria-modal="true" aria-labelledby="god-warning-title"
          style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.5)", display: "grid", placeItems: "center", zIndex: 1000 }}>
          <div className="panel-card" style={{ maxWidth: 480, margin: 16 }}>
            <h3 id="god-warning-title">Turn on God mode (beta)?</h3>
            <p><strong>{data.warning}</strong></p>
            <ul className="muted">{data.floors.map((f) => <li key={f}>{f}</li>)}</ul>
            <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
              <button className="primary" type="button" data-usage-control="settings_god_mode_toggle" disabled={busy || Boolean(loadError)}
                onClick={() => void save({ god_mode: true, acknowledged_beta: true }, "God mode is on.")}>I understand, turn it on</button>
              <button type="button" onClick={() => setConfirming(false)}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
