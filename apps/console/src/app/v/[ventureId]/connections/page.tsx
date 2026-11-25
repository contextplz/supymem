"use client";

import { plainJob } from "@/lib/jobCopy";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";

type ConnectionScope = { ventureId: string; workspaceId: string; subject: string; epoch: number; ticket: number };
type Connection = {
  id: string;
  workspace_id: string;
  provider: string;
  external_account: string;
  status: string;
  health: string;
  simulated?: boolean;
  scopes?: string[];
  last_error?: string | null;
  last_sync_at?: string | null;
};
type ReceiptEvent = {
  id: string;
  at: string;
  direction: string;
  operation: string;
  target?: string | null;
  outcome: string;
  denied_reason?: string | null;
  simulated?: boolean;
};

export default function ConnectionsPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const [workspaceId, setWorkspaceId] = useState("");
  const [items, setItems] = useState<Connection[]>([]);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [openId, setOpenId] = useState("");
  const [events, setEvents] = useState<ReceiptEvent[]>([]);
  const [loadedScope, setLoadedScope] = useState<ConnectionScope | null>(null);
  const [loadError, setLoadError] = useState("");
  const active = useRef(ventureId); active.current = ventureId;
  const generation = useRef(0), receiptRead = useRef(0);
  const mutation = useRef<{ owner: ConnectionScope } | null>(null);
  const current = useCallback((owner: ConnectionScope) => owner.ventureId === active.current
    && owner.ticket === generation.current && owner.subject === verifiedSessionSubject()
    && owner.epoch === verifiedSessionEpoch(), []);
  const ready = Boolean(loadedScope && current(loadedScope));

  const load = useCallback(async () => {
    if (ventureId !== active.current) return;
    const ticket = ++generation.current; ++receiptRead.current;
    setLoadedScope(null); setLoadError("");
    try {
      const auth = await ensureSession(), subject = verifiedSessionSubject();
      if (ticket !== generation.current || ventureId !== active.current) return;
      if (!auth.ok || !subject) { setLoadError("Your account could not be verified. Reconnect before managing connections."); return; }
      const owner: ConnectionScope = { ventureId, workspaceId: "", subject, epoch: verifiedSessionEpoch(), ticket };
      const ven = await api(`/api/ventures/${encodeURIComponent(ventureId)}`);
      if (!current(owner)) return;
      const wid = (ven.data as { workspace_id?: unknown } | null)?.workspace_id;
      if (!ven.ok || typeof wid !== "string" || !wid) { setLoadError("This venture's workspace could not be checked. Refresh before managing connections."); return; }
      owner.workspaceId = wid;
      const listed = await api(`/api/workspaces/${encodeURIComponent(wid)}/connections`);
      if (!current(owner)) return;
      const rows = (listed.data as { items?: unknown } | null)?.items;
      if (!listed.ok || !Array.isArray(rows) || !rows.every(row => row && typeof row === "object"
          && typeof row.id === "string" && typeof row.provider === "string" && typeof row.status === "string" && row.workspace_id === wid)) {
        setLoadError("Saved connections could not be verified for this workspace. Refresh before changing them."); return;
      }
      setWorkspaceId(wid); setItems(rows);
      setOpenId(""); setEvents([]); setLoadedScope(owner);
    } catch {
      if (ticket === generation.current && ventureId === active.current) setLoadError("Connections could not be checked. Refresh before changing them.");
    }
  }, [ventureId, current]);

  useEffect(() => {
    const retire = () => {
      ++generation.current; ++receiptRead.current; mutation.current = null;
      setLoadedScope(null); setWorkspaceId(""); setItems([]); setOpenId(""); setEvents([]); setMsg(""); setBusy(false); setLoadError("");
      void load();
    };
    retire(); window.addEventListener("supymem-session-changed", retire);
    return () => { ++generation.current; ++receiptRead.current; mutation.current = null; window.removeEventListener("supymem-session-changed", retire); };
  }, [load]);

  async function revoke(id: string) {
    const owner = loadedScope;
    if (!owner || !current(owner) || mutation.current || !items.some(item => item.id === id)) return;
    const request = { owner }; mutation.current = request; ++receiptRead.current;
    setBusy(true); setMsg("");
    try {
      const r = await api(`/api/connections/${encodeURIComponent(id)}/revoke`, { method: "POST", body: "{}" });
      if (mutation.current !== request || !current(owner)) return;
      const saved = r.data as { id?: unknown; workspace_id?: unknown; status?: unknown } | null;
      if (!r.ok || saved?.id !== id || saved.workspace_id !== owner.workspaceId || saved.status !== "revoked") {
        setMsg("Revocation was not confirmed. Refresh saved connections before retrying."); return;
      }
      setMsg("Saved connection marked revoked."); await load();
    } finally {
      if (mutation.current === request) { mutation.current = null; setBusy(false); }
    }
  }

  async function openReceipt(id: string) {
    const owner = loadedScope;
    if (!owner || !current(owner) || mutation.current || !items.some(item => item.id === id)) return;
    const read = ++receiptRead.current;
    if (openId === id) {
      setOpenId("");
      setEvents([]);
      return;
    }
    const r = await api(`/api/connections/${encodeURIComponent(id)}/events`);
    if (!current(owner) || read !== receiptRead.current) return;
    const rows = (r.data as { items?: unknown } | null)?.items;
    if (!r.ok || !Array.isArray(rows)) { setMsg("The connection receipt could not be loaded. No provider result was confirmed."); return; }
    setOpenId(id);
    setEvents(rows);
  }

  const renderedSubject = verifiedSessionSubject(), renderedEpoch = verifiedSessionEpoch();
  const refreshSaved = () => {
    if (ventureId === active.current && renderedSubject === verifiedSessionSubject() && renderedEpoch === verifiedSessionEpoch()) void load();
  };
  if (!ready) return <AppShell title="Connections" ventureId={ventureId}>
    <section className="panel-card"><p role="status">{loadError || "Checking this venture's saved connections…"}</p>
      {loadError && <button type="button" onClick={refreshSaved}>Refresh saved connections</button>}
    </section>
  </AppShell>;

  return (
    <AppShell title="Connections" ventureId={ventureId}>
      <div className="chat-scroll">
        <div className="chat-inner" key={`${loadedScope!.subject}:${loadedScope!.epoch}:${loadedScope!.ticket}:${ventureId}`}>
          <section className="panel-card">
            <h3>Linked accounts</h3>
            <p className="muted">
              Health comes from the last twenty events. Tokens never appear on this page, in
              the list, or in the trust receipt.
            </p>
            {items.length === 0 ? (
              <p className="muted">No accounts connected yet.</p>
            ) : (
              <ul style={{ marginTop: 16, paddingLeft: 0, listStyle: "none" }}>
                {items.map((c) => (
                  <li key={c.id} style={{ marginBottom: 16 }}>
                    <strong style={{ textTransform: "capitalize" }}>{c.provider}</strong>
                    {c.external_account ? ` · ${c.external_account}` : ""}
                    {` · ${c.status}`}
                    {` · health ${c.health || "unknown"}`}
                    {c.simulated ? " · simulated" : ""}
                    {c.scopes && c.scopes.length ? (
                      <div className="muted">Scopes: {c.scopes.join(", ")}</div>
                    ) : null}
                    {c.last_error ? <div className="muted">{plainJob(c).body}</div> : null}
                    <div style={{ marginTop: 8 }}>
                      <button type="button" onClick={() => void openReceipt(c.id)}>
                        {openId === c.id ? "Hide receipt" : "Trust receipt"}
                      </button>{" "}
                      <button type="button" disabled={busy} onClick={() => void revoke(c.id)}>
                        Revoke
                      </button>
                    </div>
                    {openId === c.id ? (
                      <ul style={{ marginTop: 10, paddingLeft: 18 }}>
                        {events.length === 0 ? (
                          <li className="muted">No events yet.</li>
                        ) : (
                          events.map((ev) => (
                            <li key={ev.id}>
                              {ev.at} · {ev.direction}/{ev.operation}
                              {ev.target ? ` · ${ev.target}` : ""} · {ev.outcome}
                              {ev.simulated ? " (simulated)" : ""}
                              {ev.denied_reason ? ` Not recorded ${ev.denied_reason}` : ""}
                            </li>
                          ))
                        )}
                      </ul>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section className="panel-card">
            <h3>Connect a system</h3>
            <p className="muted">
              Credential entry is unavailable on this page. Open company connections for the supported provider setup and review its saved account status.
            </p>
            <a className="primary" href={`/connections?workspace=${encodeURIComponent(workspaceId)}&venture=${encodeURIComponent(ventureId)}`}>
              Open company connections
            </a>
            {msg ? <p role="status" className="muted">{msg}</p> : null}
            <button type="button" className="ghost sm" onClick={refreshSaved}>Refresh saved connections</button>
          </section>
        </div>
      </div>
    </AppShell>
  );
}
