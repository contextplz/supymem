"use client";

import { useEffect, useState } from "react";
import type { InboxBriefingSettings as Settings } from "@/lib/api-types";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { useInboxBriefing, validBriefingSettings } from "./useInboxBriefing";
import styles from "@/app/account/account.module.css";

export function InboxBriefingSettings({ ventureId }: { ventureId?: string }) {
  const state = useInboxBriefing(ventureId);
  const settings = state.briefing?.settings;
  const [draft, setDraft] = useState<Settings | null>(null);
  const [note, setNote] = useState("");
  useEffect(() => { setDraft(validBriefingSettings(settings) ? { ...settings, windows: [...settings.windows] } : null); setNote(""); }, [settings, ventureId]);
  if (!state.enabled || !validBriefingSettings(settings)) return null;
  const value = draft || settings;
  const change = (next: Settings) => { setDraft(next); setNote(""); };
  return <section className={styles.section} aria-label="Inbox briefings" aria-busy={state.busy}>
    <h2>Inbox briefings</h2>
    <p className={styles.muted}>Choose when you hear about important mail. Times use your timezone. You control each inbox on Connections.</p>
    <form onSubmit={event => { event.preventDefault(); void state.save({ action: "inbox.briefing.settings", payload: value }).then(ok => { if (ok) setNote("Your briefing schedule is saved."); }); }}>
      <fieldset disabled={state.busy || !ventureId}>
        <legend>Briefing times</legend>
        {value.windows.map((time, index) => <div key={index} style={{ display: "flex", gap: 8, marginBottom: 8 }}>
          <label>Briefing {index + 1}<input type="time" required aria-label={`Briefing time ${index + 1}`} value={time}
            data-usage-control="inbox_briefing_window" onChange={event => change({ ...value, windows: value.windows.map((entry, position) => position === index ? event.target.value : entry) })} /></label>
          {value.windows.length > 1 && <button type="button" className="secondary sm" data-usage-control="inbox_briefing_window_remove"
            aria-label={`Remove briefing time ${index + 1}`} onClick={() => change({ ...value, windows: value.windows.filter((_, position) => position !== index) })}>Remove</button>}
        </div>)}
        {value.windows.length < 2 && <button type="button" className="secondary sm" data-usage-control="inbox_briefing_window_add" onClick={() => change({ ...value, windows: [...value.windows, ""] })}>Add a time</button>}
        <label>Quiet hours start<input type="time" required value={value.quiet_hours.start} data-usage-control="inbox_briefing_quiet_start" onChange={event => change({ ...value, quiet_hours: { ...value.quiet_hours, start: event.target.value } })} /></label>
        <label>Quiet hours end<input type="time" required value={value.quiet_hours.end} data-usage-control="inbox_briefing_quiet_end" onChange={event => change({ ...value, quiet_hours: { ...value.quiet_hours, end: event.target.value } })} /></label>
        <label><input type="checkbox" checked={false} disabled data-usage-control="inbox_briefing_email_copy" />Email copies are not available yet</label>
        <button type="submit" className="primary sm" data-usage-control="inbox_briefing_settings_save" disabled={!validBriefingSettings(value)}>{state.busy ? "Saving…" : "Save briefing schedule"}</button>
      </fieldset>
    </form>
    {!ventureId && <p className={styles.muted}>Choose a venture to save your briefing schedule.</p>}
    {note && <p role="status">{note}</p>}
    <ErrorNotice error={state.error} source="inbox.briefing.settings" />
  </section>;
}
