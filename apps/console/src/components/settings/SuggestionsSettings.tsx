"use client";

import { useEffect, useRef, useState } from "react";
import { desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import {
  DEFAULT_PREFS, GAP_CHOICES, hourLabel, loadPrefs, pausedNote, savePrefs, type SuggestionPrefs,
} from "@/lib/suggestions";
import styles from "@/components/suggestions/suggestions.module.css";

const HOURS = Array.from({ length: 24 }, (_, h) => h);

/** Mac pop-up preferences: on/off, how often, quiet hours, and today's pause. Stored on this Mac. */
export function SuggestionsSettings() {
  const [prefs, setPrefs] = useState<SuggestionPrefs>(DEFAULT_PREFS);
  const [ready, setReady] = useState(false);
  const [desktop, setDesktop] = useState(true);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const flight = useRef(false);

  useEffect(() => {
    let live = true;
    const onMac = isDesktopShell();
    setDesktop(onMac);
    setReady(false); setNote("");
    if (onMac) void loadPrefs().then((saved) => { if (live) { setPrefs(saved); setReady(true); } })
      .catch(() => { if (live) setNote("Couldn’t read your saved settings. Try again before changing them."); });
    return () => { live = false; };
  }, [refresh]);

  async function update(patch: Partial<SuggestionPrefs>) {
    if (!ready || flight.current) return;
    const next = { ...prefs, ...patch };
    flight.current = true; setBusy(true); setNote("");
    try {
      const saved = await savePrefs(next);
      setNote(saved ? "Saved." : "Couldn’t save this on your Mac. Your last confirmed settings are shown.");
      if (saved) setPrefs(saved);
      else setReady(false);
    } catch { setReady(false); setNote("Couldn’t save this on your Mac. Your last confirmed settings are shown."); }
    finally { flight.current = false; setBusy(false); }
  }

  async function pauseToggle(paused: boolean) {
    if (!ready || flight.current) return;
    flight.current = true; setBusy(true); setNote("");
    try {
      const next = await desktopInvokeOrThrow<SuggestionPrefs>(paused ? "suggestions_resume" : "suggestions_pause_today");
      if (!next) throw new Error();
      setPrefs(await loadPrefs()); setNote("Saved.");
    } catch {
      setReady(false); setNote("Couldn’t confirm the pause setting. Read your saved settings before trying again.");
    } finally { flight.current = false; setBusy(false); }
  }

  const paused = pausedNote(prefs);
  return (
    <div className={`panel-card ${styles.prefs}`} aria-label="Suggestion pop-ups">
      <h3>Suggestions</h3>
      <p className="muted">Supymem can show a small pop-up over any app with tasks it can take over. Each card asks first.</p>
      {!desktop ? <p className="muted">These settings live in the Supymem Mac app.</p> : (
        <>
          <label>
            <input type="checkbox" checked={prefs.enabled} disabled={!ready || busy} onChange={(e) => void update({ enabled: e.target.checked })} />
            Show suggestion pop-ups
          </label>
          <label>
            How often
            <select value={prefs.min_gap_minutes} disabled={!ready || busy || !prefs.enabled} onChange={(e) => void update({ min_gap_minutes: Number(e.target.value) })}>
              {GAP_CHOICES.map((c) => <option key={c.minutes} value={c.minutes}>{c.label}</option>)}
            </select>
          </label>
          <label>
            <input type="checkbox" checked={prefs.quiet_enabled} disabled={!ready || busy || !prefs.enabled} onChange={(e) => void update({ quiet_enabled: e.target.checked })} />
            Quiet hours
          </label>
          {prefs.quiet_enabled ? (
            <label>
              From
              <select value={prefs.quiet_start_hour} disabled={!ready || busy || !prefs.enabled} onChange={(e) => void update({ quiet_start_hour: Number(e.target.value) })}>
                {HOURS.map((h) => <option key={h} value={h}>{hourLabel(h)}</option>)}
              </select>
              to
              <select value={prefs.quiet_end_hour} disabled={!ready || busy || !prefs.enabled} onChange={(e) => void update({ quiet_end_hour: Number(e.target.value) })}>
                {HOURS.map((h) => <option key={h} value={h}>{hourLabel(h)}</option>)}
              </select>
            </label>
          ) : null}
          <div>
            <button type="button" disabled={!ready || busy || !prefs.enabled} onClick={() => void pauseToggle(Boolean(paused))}>
              {paused ? "Resume suggestions" : "Pause suggestions for today"}
            </button>
            {paused ? <span role="status" className="muted"> {paused}</span> : null}
          </div>
          {note ? <p role="status" className="muted">{note}</p> : null}
          {busy && <p role="status">Saving your settings…</p>}
          {!ready && !note && <p role="status">Reading your saved settings…</p>}
          {note && !busy && <button type="button" onClick={() => setRefresh(value => value + 1)}>Refresh saved settings</button>}
        </>
      )}
    </div>
  );
}
