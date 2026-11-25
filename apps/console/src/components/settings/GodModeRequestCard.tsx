"use client";

import { useEffect, useState } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { api } from "@/lib/client";
import { serverReason } from "@/lib/serverReason";
import { godModeRequestLine, type GodModeRequestState } from "@/lib/godModeRequest";

/** God mode is early access in the beta: this card only records interest, it never turns God mode on. */
export function GodModeRequestCard({ ventureId, className = "panel-card", compact = false }: { ventureId: string; className?: string; compact?: boolean }) {
  const base = `/api/ventures/${encodeURIComponent(ventureId)}/god-mode/requests`;
  const [state, setState] = useState<GodModeRequestState | null>(null);
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setState(null); setOpen(false); setError("");
    api(`${base}/me?viewed=true`).then((r) => {
      if (!live) return;
      if (r.ok) setState(r.data as GodModeRequestState);
      else setError("God mode access could not be checked. Try again.");
    }).catch(() => { if (live) setError("God mode access could not be checked. Try again."); });
    return () => { live = false; };
  }, [base, attempt]);

  async function submit() {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const r = await api(base, { method: "POST", body: JSON.stringify({ note: note.trim() }) });
      if (r.ok) { setState(r.data as GodModeRequestState); setOpen(false); }
      else setError(serverReason(r, "Your request was not saved. Try again."));
    } catch {
      setError("Your request was not saved. Try again.");
    } finally { setBusy(false); }
  }

  const line = state ? godModeRequestLine(state) : "";
  const description = <>
      <p className="muted">
        You can let Charlie continue the research, page updates, drafts and follow-ups you requested.
        Work you did not ask for does not start on its own. It pauses when your Mac is offline.
      </p>
      <p className="muted">
        It never sends to real people without your approval settings, and "Stop everything" halts it instantly.
      </p>
  </>;
  const action = line ? <p role="status"><strong>{line}</strong></p> : open ? (
        <form onSubmit={(e) => { e.preventDefault(); void submit(); }} style={{ display: "grid", gap: 8, marginTop: 8 }}>
          <label htmlFor={`god-note-${ventureId}`}>What would you want it to do? (optional)</label>
          <textarea id={`god-note-${ventureId}`} rows={3} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />
          <div style={{ display: "flex", gap: 8 }}>
            <button type="submit" className="primary" data-usage-control="settings_god_mode_request" disabled={busy}>
              {busy ? "Saving…" : "Send request"}
            </button>
            <button type="button" className="ghost" disabled={busy} onClick={() => setOpen(false)}>Cancel</button>
          </div>
        </form>
      ) : (
        <button type="button" className="primary" style={{ marginTop: 8 }} disabled={!state} onClick={() => setOpen(true)}>
          Request God mode access
        </button>
      );
  return (
    <div className={className} id="god-mode" aria-label="God mode early access">
      {compact ? <>
        <div className="settings-row"><strong>God mode</strong><div className="settings-actions">{action}</div></div>
        <details><summary>God mode (early access)</summary>{description}</details>
      </> : <><h3>God mode (early access)</h3>{description}{action}</>}
      {!state && error && <button type="button" onClick={() => setAttempt(value => value + 1)}>Try again</button>}
      <ErrorNotice error={error} source="settings.god_mode_request" className="muted" />
    </div>
  );
}
