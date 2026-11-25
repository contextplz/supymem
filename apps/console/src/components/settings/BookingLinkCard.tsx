"use client";

import { useEffect, useState } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { verifiedSessionSubject } from "@/lib/client";
import { journeyClient } from "@/lib/gtmJourneyApi";

/** The link Supymem puts in a suggested reply to someone interested. Saving it never sends anything. */
export function BookingLinkCard({ ventureId, workspaceId }: { ventureId: string; workspaceId: string }) {
  const [saved, setSaved] = useState<string | null>(null);
  const [link, setLink] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const owner = () => ({ actorId: verifiedSessionSubject() || "", workspaceId, ventureId });

  useEffect(() => {
    let live = true;
    setSaved(null); setError("");
    if (workspaceId) journeyClient.get(owner()).then(j => { if (live) { setSaved(j.booking_link || ""); setLink(j.booking_link || ""); } })
      .catch(() => { if (live) setError("Your booking link could not be loaded. Try again, or ask people to reply with a time."); });
    else setError("The workspace could not be loaded. Refresh saved settings to try again.");
    return () => { live = false; };
  }, [ventureId, workspaceId, attempt]);

  async function save() {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const j = await journeyClient.bookingLink(owner(), link.trim());
      setSaved(j.booking_link || ""); setLink(j.booking_link || "");
    } catch (err) {
      setError(err instanceof Error ? err.message : "The link was not saved. Try again.");
    } finally { setBusy(false); }
  }

  return (
    <div className="panel-card" id="booking-link">
      <h3>Booking link (optional)</h3>
      <p className="muted">Your booking page is included in reachable-lead drafts and suggested replies. Leave it blank to ask people to reply with a time. Nothing is sent automatically.</p>
      <form style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }} onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <input type="url" inputMode="url" placeholder="https://cal.com/your-name" maxLength={300} value={link} disabled={saved === null || busy}
          onChange={(e) => setLink(e.target.value)} style={{ flex: "1 1 16rem" }} aria-label="Booking link" />
        <button className="primary" type="submit" data-usage-control="settings_booking_link_save" disabled={saved === null || busy || link.trim() === saved}>
          {busy ? "Saving…" : "Save link"}
        </button>
      </form>
      <ErrorNotice error={error} source="settings.booking_link" />
      {saved === null && error && workspaceId && <button type="button" onClick={() => setAttempt(value => value + 1)}>Try again</button>}
    </div>
  );
}
