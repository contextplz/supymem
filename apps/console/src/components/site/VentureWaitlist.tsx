"use client";

import { useEffect, useRef, useState } from "react";
import { api, ensureSession, verifiedSessionSubject } from "@/lib/client";
import { apiErrorCode, apiFailure } from "@/lib/apiFailure";
import { waitlistUsageControl } from "@/lib/usageControl";
import { forgetWaitlistStatus, lastWaitlistStatus, rememberWaitlistStatus, scopedWaitlistStatus, scopedWaitlistUrl, verifiedPublishedSiteUrl, waitlistReadFailure, waitlistStatusDuringCooldown, type WaitlistSummary } from "@/lib/waitlistStatusCache";
import styles from "./VentureWaitlist.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Waitlist = WaitlistSummary & {
  leads?: {id: string; email: string; created_at: string; consent_text: string}[];
};

export function VentureWaitlist({ventureId, usageSurface}: {ventureId: string; usageSurface: "website" | "gtm"}) {
  const [data, setData] = useState<Waitlist | null>(null);
  const [statusConfirmed, setStatusConfirmed] = useState(false);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [cooldownUntil, setCooldownUntil] = useState(0);
  const [now, setNow] = useState(0);
  const generation = useRef(0);

  async function action(operation: "load" | "enable" | "disable" | "test") {
    const token = ++generation.current;
    setBusy(true); setStatusConfirmed(false); setError(""); setNotice("");
    try {
      const session = await ensureSession();
      if (token !== generation.current) return;
      const subject = (session.data as {subject?: unknown} | null)?.subject;
      const cacheSubject = session.ok && typeof subject === "string" ? subject : "";
      const path = `/api/ventures/${encodeURIComponent(ventureId)}/waitlist`;
      const savedBeforeRequest = cacheSubject ? waitlistStatusDuringCooldown(cacheSubject, ventureId, Date.now()) : undefined;
      if (operation === "test" && savedBeforeRequest) {
        setData(savedBeforeRequest.data);
        setCooldownUntil(savedBeforeRequest.cooldownUntil);
        setNow(Date.now());
        setNotice("A public submission test was started recently. Try again when the countdown ends.");
        return;
      }
      let probeRecentlyRun = false;
      if (operation !== "load") {
        const result = await api(path + (operation === "test" ? "/test" : ""), {
          method: operation === "test" ? "POST" : "PUT",
          ...(operation !== "test" ? {body: JSON.stringify({enabled: operation === "enable"})} : {}),
        });
        if (token !== generation.current) return;
        if (!result.ok) {
          probeRecentlyRun = operation === "test" && result.status === 429
            && apiErrorCode(result) === "waitlist_probe_recent";
          if (!probeRecentlyRun) {
            if (result.status === 429) {
              setNotice("The request limit was reached. Wait a moment, then refresh the waitlist status.");
              return;
            }
            setData(null); // A failed response may follow a committed change: re-read before relying on old status.
            setError(apiFailure(result, operation === "test" ? "verifying the public waitlist" : "updating the waitlist"));
            return;
          }
        } else {
          // Both configuration changes and the public probe return the committed
          // status. A second GET can hit the shared gateway limit after success.
          const committed = scopedWaitlistStatus(result.data, ventureId) as Waitlist | null;
          if (committed) {
            setData(committed);
            setStatusConfirmed(true);
            const checkedAt = Date.now();
            setNow(checkedAt);
            const saved = cacheSubject ? rememberWaitlistStatus(cacheSubject, committed, checkedAt) : null;
            setCooldownUntil(saved?.cooldownUntil || checkedAt);
            window.dispatchEvent(new CustomEvent("supymem-waitlist-changed", { detail: { ventureId } }));
            return;
          }
        }
      }
      const current = await api(path);
      if (token !== generation.current) return;
      const saved = cacheSubject ? lastWaitlistStatus(cacheSubject, ventureId) : undefined;
      const previous = data?.venture_id === ventureId ? data
        : saved?.data || null;
      const readFailure = !current.ok && waitlistReadFailure(current.status, previous);
      if (readFailure) {
        if (readFailure.data) {
          setData(readFailure.data);
          if (saved) { setNow(Date.now()); setCooldownUntil(saved.cooldownUntil); }
        }
        setNotice(readFailure.notice);
        return;
      }
      const next = current.data as Waitlist;
      if (!current.ok || next?.venture_id !== ventureId) {
        if (cacheSubject && [401, 403, 404].includes(current.status)) forgetWaitlistStatus(cacheSubject, ventureId);
        setData(null); setError(apiFailure(current, "loading the venture waitlist")); return;
      }
      setData(next);
      setStatusConfirmed(true);
      const checkedAt = Date.now();
      setNow(checkedAt);
      const updated = cacheSubject ? rememberWaitlistStatus(cacheSubject, next, checkedAt) : null;
      setCooldownUntil(updated?.cooldownUntil || checkedAt);
      if (operation !== "load") window.dispatchEvent(new CustomEvent("supymem-waitlist-changed", { detail: { ventureId } }));
      if (probeRecentlyRun) setNotice("A public submission test was started recently. Its status is shown below.");
    } catch {
      if (token === generation.current) {
        setData(null); setError("The waitlist result could not be confirmed. Refresh its status before trying another change.");
      }
    } finally { if (token === generation.current) setBusy(false); }
  }

  useEffect(() => {
    setData(null); setStatusConfirmed(false); void action("load");
    return () => { generation.current++; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ventureId]);

  // A chat action (e.g. "enable the waitlist") or coming back to this tab re-reads the status.
  useEffect(() => {
    const refresh = (event?: Event) => {
      const id = (event as CustomEvent<{ ventureId?: string }> | undefined)?.detail?.ventureId;
      if (document.hidden || (id && id !== ventureId)) return;
      const subject = verifiedSessionSubject();
      if (subject) forgetWaitlistStatus(subject, ventureId);
      void action("load");
    };
    window.addEventListener("supymem-chat-action-done", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("supymem-chat-action-done", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ventureId]);

  useEffect(() => {
    if (cooldownUntil <= Date.now()) return;
    const timer = window.setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= cooldownUntil) window.clearInterval(timer);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [cooldownUntil]);

  const cooldownSeconds = Math.max(0, Math.ceil((cooldownUntil - now) / 1000));

  const publicUrl = data?.venture_id === ventureId ? scopedWaitlistUrl(data) : undefined;
  const shareUrl = data?.venture_id === ventureId ? verifiedPublishedSiteUrl(data, statusConfirmed) : undefined;

  async function copyVerifiedLink() {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setNotice("Published landing page link copied. Supymem has not sent or posted it.");
    } catch {
      setNotice("Clipboard access is unavailable. Open Website and copy the published landing page address yourself.");
    }
  }

  return <section id={usageSurface === "gtm" ? "gtm-waitlist" : undefined} className={styles.panel} aria-label="Venture waitlist" aria-busy={busy}>
    <h2>Give visitors a next step</h2>
    <p>Collect early-access interest through a consent form for this venture. Test its public submission and storage before publishing a landing page that links to it.</p>
    <ErrorNotice error={error} source="VentureWaitlist.error" />
    {notice && <p role="status">{notice}</p>}
    {data && <>
      <p role="status">{!statusConfirmed ? cooldownSeconds > 0
        ? "This is the last loaded waitlist status. Wait for the test cooldown, then refresh before copying a landing page link or relying on conversion readiness."
        : "This is the last loaded waitlist status. Refresh it before copying a landing page link or relying on conversion readiness."
        : !data.enabled ? "Waitlist is closed. Enable it, then test public submission." : data.submission_verified
        ? shareUrl
          ? "Public submission and storage verified. Review the published landing page and confirm its button reaches this form before sharing."
          : "Public submission and storage verified. No published venture landing page is available yet; review Website before sharing."
        : "Waitlist is open; its public submission has not been verified."}</p>
      {data.verified_at && <p>Last verified: {new Date(data.verified_at).toLocaleString()}</p>}
      {data.enabled && publicUrl && <p><a href={publicUrl} target="_blank" rel="noopener noreferrer">Open this venture’s signup form</a></p>}
      {shareUrl && <p><button type="button" data-usage-control={waitlistUsageControl(usageSurface, "copy")} disabled={busy} onClick={() => void copyVerifiedLink()}>Copy published landing page link</button><br />Supymem has not shared this link or contacted anyone. Confirm its signup button before posting it.</p>}
      <p><strong>{data.total} early-access requests.</strong> These show interest, not active users or payments. Test submissions are excluded.</p>
      {data.enabled && cooldownSeconds > 0 && <p role="status">The public submission test is available again in {cooldownSeconds} second{cooldownSeconds === 1 ? "" : "s"}.</p>}
      <div className={styles.actions}>
        <button type="button" data-usage-control={waitlistUsageControl(usageSurface, data.enabled ? "close" : "enable")} disabled={busy} onClick={() => void action(data.enabled ? "disable" : "enable")}>
          {data.enabled ? "Close waitlist" : "Enable waitlist"}
        </button>
        {data.enabled && <button type="button" data-usage-control={waitlistUsageControl(usageSurface, "test")} disabled={busy || cooldownSeconds > 0} onClick={() => void action("test")}>Test public submission</button>}
      </div>
      {Boolean(data.leads?.length) && <details><summary>View recent consented requests</summary>
        <ul>{data.leads!.map(lead => <li key={lead.id}><strong>{lead.email}</strong> · {new Date(lead.created_at).toLocaleString()}<p>{lead.consent_text}</p></li>)}</ul>
      </details>}
    </>}
    <button type="button" data-usage-control={waitlistUsageControl(usageSurface, "refresh")} disabled={busy || cooldownSeconds > 0} onClick={() => void action("load")}>{busy ? "Checking waitlist…" : "Refresh waitlist status"}</button>
  </section>;
}
