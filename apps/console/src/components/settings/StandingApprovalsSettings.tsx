"use client";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Grant = { id: string; summary: string; expires_at: string; detail: { venture_id: string } };

export function StandingApprovalsSettings({ workspaceId, ventureId }: { workspaceId: string; ventureId: string }) {
  const [grants, setGrants] = useState<Grant[]>([]), [busy, setBusy] = useState(""), [error, setError] = useState("");
  const [loadedOwner, setLoadedOwner] = useState("");
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0), flight = useRef<object | null>(null);
  const owner = `${verifiedSessionSubject()}:${verifiedSessionEpoch()}:${workspaceId}:${ventureId}`;
  const active = useRef(owner); active.current = owner;
  useEffect(() => {
    let mounted = true;
    async function load() {
      const ticket = ++generation.current; flight.current = null; setLoadedOwner(""); setGrants([]); setBusy(""); setError("");
      const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
      if (!subject || !workspaceId) return;
      const current = () => mounted && ticket === generation.current && active.current === owner
        && verifiedSessionSubject() === subject && verifiedSessionEpoch() === epoch;
      try {
        const result = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/approvals?status=standing`);
        if (!current()) return;
        setLoadedOwner(owner);
        if (result.ok && Array.isArray((result.data as { items?: unknown })?.items)) {
          setGrants((result.data as { items: Grant[] }).items.filter(grant => grant.detail?.venture_id === ventureId));
        } else setError("Your standing approvals could not be checked.");
      } catch { if (current()) { setLoadedOwner(owner); setError("Your standing approvals could not be checked."); } }
    }
    void load(); window.addEventListener("supymem-session-changed", load);
    return () => { mounted = false; ++generation.current; flight.current = null; window.removeEventListener("supymem-session-changed", load); };
  }, [owner, workspaceId, ventureId, refresh]);

  async function revoke(id: string) {
    const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch(), ticket = generation.current;
    if (!subject || flight.current || active.current !== owner) return;
    const request = {}; flight.current = request; setBusy(id); setError("");
    const current = () => ticket === generation.current && active.current === owner && flight.current === request
      && verifiedSessionSubject() === subject && verifiedSessionEpoch() === epoch;
    try {
      const result = await api(`/api/approvals/${encodeURIComponent(id)}/decide`, { method: "POST",
        body: JSON.stringify({ decision: "withdrawn", decided_by: subject }) });
      if (!current()) return;
      if (result.ok) setGrants(previous => previous.filter(grant => grant.id !== id));
      else setError("Your revocation could not be confirmed. Refresh its saved status before trying again.");
    } catch { if (current()) setError("Your revocation could not be confirmed. Refresh its saved status before trying again."); }
    finally { if (current()) { flight.current = null; setBusy(""); } }
  }

  if (loadedOwner !== owner || !grants.length && !error) return null;
  return <section className="panel-card" aria-label="Standing approvals">
    <h3>What Charlie may do without asking</h3>
    <p>These approvals apply only to you and this venture. They expire after 30 days.</p>
    <ul>{grants.map(grant => <li key={grant.id}>
      {grant.summary} <small>Expires {new Date(grant.expires_at).toLocaleDateString()}</small>
      <button type="button" className="secondary sm" disabled={Boolean(busy)}
        data-usage-control="approval_revoke" aria-label={`Revoke ${grant.summary}`}
        onClick={() => void revoke(grant.id)}>{busy === grant.id ? "Revoking…" : "Revoke"}</button>
    </li>)}</ul>
    <ErrorNotice error={error} source="settings.standing_approvals" />
    {error && <button type="button" disabled={Boolean(busy)} onClick={() => setRefresh(value => value + 1)}>Refresh saved status</button>}
  </section>;
}
