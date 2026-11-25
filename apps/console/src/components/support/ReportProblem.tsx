"use client";

import { useState } from "react";
import { usePathname } from "next/navigation";
import { api } from "@/lib/client";
import { buildProblemBody, screenshotProblem } from "@/lib/problemReport";
import { currentAppVersion } from "@/components/layout/UsageTracker";

/** User-initiated bug report: text plus an optional screenshot the founder picks and explicitly consents to. */
export function ReportProblem({ ventureId, label = "Report a problem" }: { ventureId?: string | null; label?: string }) {
  const pathname = usePathname() || "";
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [shot, setShot] = useState<{ b64: string; name: string } | null>(null);
  const [consent, setConsent] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  async function pick(file: File | undefined) {
    setNote("");
    setShot(null);
    setConsent(false);
    if (!file) return;
    const problem = screenshotProblem(file);
    if (problem) return setNote(problem);
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    setShot({ b64: btoa(binary), name: file.name });
  }

  async function submit() {
    setBusy(true);
    setNote("");
    const body = buildProblemBody({ message, pathname, appVersion: currentAppVersion(), ventureId,
      screenshotB64: shot?.b64, consent });
    const result = await api("/api/problem-reports", { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });
    setBusy(false);
    if (result.ok) return setSent(true);
    setNote(result.status === 429 ? "You have sent a few reports already. Please try again in a few minutes."
      : "We could not send your report. Check your connection and try again.");
  }

  if (!open) return <button type="button" className="ghost sm" onClick={() => { setOpen(true); setSent(false); }}>{label}</button>;
  return (
    <form aria-label="Report a problem" onSubmit={(event) => { event.preventDefault(); void submit(); }} style={{ display: "grid", gap: 8 }}>
      {sent ? (
        <>
          <p role="status">Thank you. Your report was sent and Supymem's team will look at it.</p>
          <button type="button" className="ghost sm" onClick={() => { setOpen(false); setMessage(""); setShot(null); setConsent(false); }}>Close</button>
        </>
      ) : (
        <>
          <label>What went wrong?
            <textarea value={message} maxLength={2000} rows={4} required minLength={3} onChange={(event) => setMessage(event.target.value)}
              placeholder="Tell us what you did and what you expected." />
          </label>
          <p className="muted" style={{ fontSize: "0.8rem", margin: 0 }}>
            We also attach this app version and any recent error codes from your account. Nothing from your screen is sent unless you attach a screenshot below.
          </p>
          <label>Screenshot (optional)
            <input type="file" accept="image/png,image/jpeg" onChange={(event) => void pick(event.target.files?.[0])} />
          </label>
          {shot ? (
            <label style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
              <input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
              <span>I agree to send {shot.name}. It may show information visible on my screen.</span>
            </label>
          ) : null}
          {note ? <p role="status">{note}</p> : null}
          <div style={{ display: "flex", gap: 8 }}>
            <button type="submit" className="primary sm" disabled={busy || message.trim().length < 3 || (shot !== null && !consent)}>
              {busy ? "Sending…" : "Send report"}
            </button>
            <button type="button" className="ghost sm" onClick={() => setOpen(false)}>Cancel</button>
          </div>
        </>
      )}
    </form>
  );
}
