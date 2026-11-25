"use client";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Routine = {
  id: string; title: string; state: string; timezone: string;
  quiet_hours: { start: string; end: string }; next_fire_at: string | null;
};

export function RoutinesSettings({ ventureId, workspaceId }: { ventureId: string; workspaceId: string }) {
  const [items, setItems] = useState<Routine[]>([]);
  const [loaded, setLoaded] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const generation = useRef(0), flight = useRef<object | null>(null);
  const owner = `${verifiedSessionSubject()}:${verifiedSessionEpoch()}:${workspaceId}:${ventureId}`;
  const active = useRef(owner); active.current = owner;
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let mounted = true;
    async function load() {
      const ticket = ++generation.current, subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
      flight.current = null; setLoaded(""); setEnabled(false); setItems([]); setBusy(""); setError("");
      if (!subject || !workspaceId || owner !== `${subject}:${epoch}:${workspaceId}:${ventureId}`) return;
      const current = () => mounted && ticket === generation.current && active.current === owner
        && verifiedSessionSubject() === subject && verifiedSessionEpoch() === epoch;
      try {
        const config = await api("/api/ui-config");
        if (!current() || !config.ok || (config.data as { routines_v1?: boolean })?.routines_v1 !== true) return;
        setEnabled(true); setLoaded(owner);
        const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/routines`);
        if (!current()) return;
        if (result.ok && Array.isArray((result.data as { items?: unknown })?.items)) {
          setItems((result.data as { items: Routine[] }).items);
        } else setError("Your routines could not be loaded.");
      } catch { if (current()) setError("Your routines could not be loaded."); }
    }
    void load(); window.addEventListener("supymem-session-changed", load);
    return () => { mounted = false; ++generation.current; flight.current = null; window.removeEventListener("supymem-session-changed", load); };
  }, [owner, ventureId, workspaceId, refresh]);

  async function change(item: Routine, method: "POST" | "DELETE" | "PATCH", settings?: { timezone: string; quiet_hours: { start: string; end: string } }) {
    const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch(), ticket = generation.current;
    if (!subject || flight.current || active.current !== owner || owner !== `${subject}:${epoch}:${workspaceId}:${ventureId}`) return;
    const request = {}; flight.current = request; setBusy(item.id); setError("");
    const current = () => ticket === generation.current && active.current === owner && flight.current === request
      && verifiedSessionSubject() === subject && verifiedSessionEpoch() === epoch;
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/routines/${encodeURIComponent(item.id)}${method === "POST" ? "/pause" : ""}`,
        { method, ...(settings ? { body: JSON.stringify(settings) } : {}) });
      if (!current()) return;
      if (result.ok) setItems(previous => method === "DELETE" ? previous.filter(saved => saved.id !== item.id)
        : previous.map(saved => saved.id === item.id ? result.data as Routine : saved));
      else setError("Your routine change could not be confirmed. Refresh before trying again.");
    } catch { if (current()) setError("Your routine change could not be confirmed. Refresh before trying again."); }
    finally { if (current()) { flight.current = null; setBusy(""); } }
  }

  if (loaded !== owner || !enabled) return null;
  return <section className="panel-card" aria-label="Your routines">
    <h3>Your routines</h3>
    <p>Save a repeating request in chat. Your standing approvals cover only eligible preparation. Other actions still need your review.</p>
    {!items.length && !error && <p>You have no saved routines.</p>}
    <ul>{items.map(item => <li key={item.id}>
      <strong>{item.title}</strong> <span>{item.state}</span>
      <p>{item.next_fire_at ? `Next run: ${new Date(item.next_fire_at).toLocaleString(undefined, { timeZone: item.timezone })} (${item.timezone})` : "Not scheduled"}</p>
      <form key={`${owner}:${item.id}:${item.timezone}:${item.quiet_hours.start}:${item.quiet_hours.end}`} onSubmit={event => {
        event.preventDefault(); const values = new FormData(event.currentTarget);
        void change(item, "PATCH", { timezone: String(values.get("timezone")), quiet_hours: { start: String(values.get("start")), end: String(values.get("end")) } });
      }}>
        <label>Timezone <input name="timezone" required maxLength={80} defaultValue={item.timezone} disabled={Boolean(busy)} /></label>
        <label>Quiet hours start <input name="start" type="time" required defaultValue={item.quiet_hours.start} disabled={Boolean(busy)} /></label>
        <label>Quiet hours end <input name="end" type="time" required defaultValue={item.quiet_hours.end} disabled={Boolean(busy)} /></label>
        <button type="submit" className="secondary sm" disabled={Boolean(busy)}>Save schedule settings</button>
      </form>
      <button type="button" className="secondary sm" aria-label={`Pause ${item.title}`} disabled={Boolean(busy) || item.state === "paused"} onClick={() => void change(item, "POST")}>Pause</button>
      <button type="button" className="secondary sm" aria-label={`Delete ${item.title}`} disabled={Boolean(busy)} onClick={() => void change(item, "DELETE")}>Delete</button>
    </li>)}</ul>
    <ErrorNotice error={error} source="settings.routines" />
    {error && <button type="button" disabled={Boolean(busy)} onClick={() => setRefresh(value => value + 1)}>Refresh saved status</button>}
  </section>;
}
