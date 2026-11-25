"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { verifiedSessionEpoch, verifiedSessionSubject, ensureSession } from "@/lib/client";
import { browserTimezone, captureFounderTimezone, taskClient, type TaskPreferences as Preferences } from "@/lib/taskApi";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Owner = { venture: string; subject: string; epoch: number; generation: number };
type Saved = Owner & Preferences;

export function TaskPreferences({ ventureId }: { ventureId: string }) {
  const [saved, setSaved] = useState<Saved | null>(null);
  const [timezone, setTimezone] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const active = useRef(ventureId); active.current = ventureId;
  const generation = useRef(0), inFlight = useRef(false);
  const inputId = useId();
  const current = (owner: Owner) => owner.venture === active.current && owner.subject === verifiedSessionSubject()
    && owner.epoch === verifiedSessionEpoch() && owner.generation === generation.current;
  const visible = saved && current(saved) ? saved : null;

  const load = useCallback(async () => {
    const ticket = ++generation.current;
    await ensureSession();
    const subject = verifiedSessionSubject();
    if (!subject || ticket !== generation.current || ventureId !== active.current) return;
    const owner = { venture: ventureId, subject, epoch: verifiedSessionEpoch(), generation: ticket };
    try {
      const preferences = await captureFounderTimezone(ventureId);
      if (current(owner)) { setSaved({ ...owner, ...preferences }); setTimezone(preferences.timezone); setError(""); }
    } catch (failure) { if (current(owner)) setError(failure instanceof Error ? failure.message : "Task preferences could not be confirmed. Refresh to try again."); }
  }, [ventureId]);
  useEffect(() => {
    const reset = () => { setSaved(null); setTimezone(""); setBusy(false); inFlight.current = false; setError(""); setNote(""); void load(); };
    reset(); window.addEventListener("supymem-session-changed", reset);
    return () => { ++generation.current; window.removeEventListener("supymem-session-changed", reset); };
  }, [load]);

  async function save() {
    if (!visible || !current(visible) || inFlight.current || !timezone.trim()) return;
    const owner = visible;
    inFlight.current = true; setBusy(true); setError(""); setNote("");
    try {
      const result = await taskClient.savePreferences(ventureId, timezone.trim());
      if (current(owner)) { setSaved({ ...owner, ...result }); setTimezone(result.timezone); setNote("Timezone saved for new task schedules. Existing schedules keep their saved timezone until you edit them."); }
    } catch (failure) { if (current(owner)) setError(failure instanceof Error ? failure.message : "The timezone change could not be confirmed. Refresh saved preferences."); }
    finally { if (current(owner)) { inFlight.current = false; setBusy(false); } }
  }
  const zones = [...new Set(["UTC", browserTimezone(), ...(typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [])])];
  return <section className="panel-card" aria-label="Task timezone">
    <h3>Task timezone</h3>
    <p>{visible ? visible.confirmed ? "New task schedules use your saved timezone." : "UTC is an unconfirmed default. Choose your timezone." : "Checking your saved timezone…"}</p>
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <label htmlFor={inputId}>Timezone</label>
      <input id={inputId} list={`${inputId}-zones`} value={timezone} required maxLength={80} disabled={!visible || busy}
        onChange={event => setTimezone(event.target.value)} style={{ display: "block", width: "100%", margin: "8px 0" }} />
      <datalist id={`${inputId}-zones`}>{zones.map(zone => <option key={zone} value={zone} />)}</datalist>
      <button type="submit" data-usage-control="task_timezone_save" disabled={!visible || busy || !timezone.trim()}>{busy ? "Saving timezone…" : "Save timezone"}</button>
      <button type="button" data-usage-control="task_timezone_detect" disabled={!visible || busy} onClick={() => setTimezone(browserTimezone())}>Use device timezone</button>
    </form>
    <p>Changing your timezone keeps existing task schedules unchanged. Edit a standing task to change its future runs.</p>
    <ErrorNotice error={error} source="tasks.timezone" />
    {error && <button type="button" data-usage-control="task_timezone_refresh" disabled={busy} onClick={() => void load()}>Refresh saved timezone</button>}
    {note && <p role="status">{note}</p>}
  </section>;
}
