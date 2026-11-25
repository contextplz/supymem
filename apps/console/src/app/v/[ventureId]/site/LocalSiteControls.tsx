"use client";

import { useEffect, useRef, useState } from "react";
import { api, authenticatedFetch } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import type { PageBuilderProfile } from "@/lib/pageBuild";

type Publication = { state: string; url?: string; notice?: string };
type Status = { ready: boolean; next_step: string; publication?: Publication; verification_pending?: boolean; can_take_offline?: boolean; destination?: { notice: string } };
type Selection = { job_id: string; variant: number };

function failure(data: unknown): string {
  const detail = (data as { detail?: unknown } | null)?.detail;
  return typeof detail === "string" ? detail : "Open Settings > Keys and connections > Publishing, check your account details, then retry from Website.";
}

export function LocalSiteControls({ ventureId, appId, buildId, selection, savedPage, onPublication }: {
  ventureId: string; appId?: string; buildId?: string; selection?: Selection; savedPage?: boolean; onPublication: (publication: Publication) => void;
}) {
  const [status, setStatus] = useState<Status | null>(null);
  const [savedSource, setSavedSource] = useState<Selection | null | undefined>(undefined);
  const [readError, setReadError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [emails, setEmails] = useState("");
  const [consent, setConsent] = useState(false);
  const [note, setNote] = useState("");
  const scope = useRef<AbortController | null>(null);
  const flight = useRef(false);
  const base = `/api/ventures/${encodeURIComponent(ventureId)}/site/local`;
  const profileLoaded = savedSource !== undefined;
  const source = selection || savedSource;
  const canExport = Boolean(source || profileLoaded && savedPage);
  const awaitingCheck = status?.verification_pending ?? status?.publication?.state === "verification_pending";
  const canTakeOffline = status?.can_take_offline ?? (awaitingCheck || status?.publication?.state === "live");
  const canPublish = status?.ready && !awaitingCheck && (canExport || profileLoaded && Boolean(appId && buildId));

  useEffect(() => { setEmails(""); setConsent(false); }, [ventureId]);

  useEffect(() => {
    const controller = new AbortController();
    scope.current = controller;
    flight.current = false;
    setStatus(null); setSavedSource(undefined); setReadError("");
    setError(""); setBusy(false); setNote("");
    void Promise.allSettled([
      api(base, { signal: controller.signal }),
      api(`/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder/profile`, { signal: controller.signal }),
    ]).then(([saved, profile]) => {
      if (controller.signal.aborted || scope.current !== controller) return;
      const errors: string[] = [];
      const value = saved.status === "fulfilled" && saved.value.ok ? saved.value.data as Status | null : null;
      if (typeof value?.ready === "boolean") {
        setStatus(value);
        if (value.publication) onPublication(value.publication);
      } else errors.push("Your publishing settings could not be loaded.");
      const profileData = profile.status === "fulfilled" && profile.value.ok ? profile.value.data as PageBuilderProfile | null : null;
      if (profileData && typeof profileData === "object" && "current" in profileData) {
        const current = profileData.current;
        const candidate = current?.native_ready && Array.isArray(current.candidates)
          ? current.candidates.find(item => item && Number.isInteger(item.variant) && item.variant >= 0 && item.variant <= 2) : null;
        setSavedSource(current?.state === "ready" && candidate && typeof current.job_id === "string" && current.job_id
          ? { job_id: current.job_id, variant: candidate.variant } : null);
      } else errors.push("Your saved page could not be checked.");
      setReadError(errors.length ? `${errors.join(" ")} Choose Try again to check your page and publishing settings.` : "");
    });
    return () => controller.abort();
  }, [base, ventureId, attempt]);

  async function act(action: "publish" | "unpublish" | "verify" | "signups" | "test") {
    const controller = scope.current;
    if (!controller || controller.signal.aborted || flight.current || action === "publish" && !canPublish) return;
    flight.current = true;
    setBusy(true); setError(""); setNote("");
    const current = () => scope.current === controller && !controller.signal.aborted;
    try {
      const path = action === "test" ? `/api/ventures/${encodeURIComponent(ventureId)}/waitlist/test` : `${base}/${action}`;
      const payload = action === "publish" ? { job_id: source?.job_id || null, ...(source?.variant ? { variant: source.variant } : {}), ...(appId && buildId ? { app_id: appId, build_id: buildId } : {}) }
        : action === "signups" ? { emails: emails.split(/[\s,;]+/).filter(Boolean), consent } : undefined;
      const result = await api(path, { method: "POST", signal: controller.signal, ...(payload ? { body: JSON.stringify(payload) } : {}) });
      if (!current()) return;
      if (!result.ok) {
        setError(failure(result.data));
        if (action === "publish" || action === "unpublish") {
          const saved = await api(base, { signal: controller.signal });
          if (!current()) return;
          const value = saved.ok ? saved.data as Status | null : null;
          if (typeof value?.ready === "boolean") {
            setStatus(value);
            if (value.publication) onPublication(value.publication);
          }
        }
        return;
      }
      if (action === "signups" || action === "test") {
        setNote((result.data as { note: string }).note);
        if (action === "signups") { setEmails(""); setConsent(false); }
        return;
      }
      const publication = result.data as Publication;
      if (action === "verify" && publication.state === "failed") {
        setStatus(previous => previous ? { ...previous, verification_pending: false,
          can_take_offline: previous.publication?.state === "verification_pending" ? undefined : previous.can_take_offline,
          publication: previous.publication?.state === "verification_pending" ? publication : previous.publication } : previous);
      } else {
        setStatus(previous => previous ? { ...previous, publication, verification_pending: false,
          can_take_offline: action !== "unpublish" } : previous);
        onPublication(publication);
      }
      setNote(publication.notice || (action === "unpublish" ? "Your public page is confirmed offline. Your saved source and signups are kept." : "Your public URL serves your saved page."));
    } catch {
      if (current()) setError("Your app did not respond. Restart it, open Website, and retry.");
    } finally { if (current()) { flight.current = false; setBusy(false); } }
  }

  async function download(kind: "html" | "zip") {
    const controller = scope.current;
    if (!controller || controller.signal.aborted || flight.current || !canExport) return;
    flight.current = true;
    setBusy(true); setError("");
    try {
      const query = source ? `?job_id=${encodeURIComponent(source.job_id)}&variant=${source.variant}` : "";
      const path = `${base}/export.${kind === "zip" ? "zip" : "html"}${query}`;
      const response = await authenticatedFetch(path, { signal: controller.signal });
      if (!response.ok) {
        const message = failure(await response.json());
        if (!controller.signal.aborted) setError(message);
        return;
      }
      const blob = await response.blob();
      if (controller.signal.aborted || scope.current !== controller) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = kind === "zip" ? "landing-page.zip" : "landing.html";
      anchor.click(); URL.revokeObjectURL(url);
    } catch { if (!controller.signal.aborted) setError("Open Website and build your page before exporting, then retry."); }
    finally { if (!controller.signal.aborted && scope.current === controller) { flight.current = false; setBusy(false); } }
  }

  return <section id="local-publishing" aria-label="Your publishing account">
    <h2>Publish your landing page</h2>
    {readError && <ErrorNotice error={readError} as="div" source="site.local.publication.read">{readError}{" "}
      <button type="button" disabled={busy} onClick={() => setAttempt(value => value + 1)}>Try again</button>
    </ErrorNotice>}
    {!readError && (!status || !profileLoaded) && <p role="status">Checking your page and publishing settings…</p>}
    {status && !status.ready && <p>{status.next_step} <a href="/settings/local#publishing-title">Publishing settings</a></p>}
    {status?.destination && (status.ready || status.destination.notice !== status.next_step) && <p>{status.destination.notice}</p>}
    {awaitingCheck && <p>Your host accepted your page. Choose Check public URL on Website before publishing again.</p>}
    {status?.publication?.state === "takedown_pending" && !error && <p>Some of your pages are not confirmed offline. Choose Take offline to retry.</p>}
    {profileLoaded && !canExport && !(appId && buildId) && <p>Build with your coding tool before you publish or export. Ask Charlie in your venture chat to build your landing page.</p>}
    {profileLoaded && !canExport && appId && buildId && <p>You can publish your verified build. Build a landing page with your coding tool if you want an HTML or ZIP export.</p>}
    <div className="row">
      <button type="button" className="primary" disabled={busy || !canPublish} onClick={() => void act("publish")}>{busy ? "Working…" : "Publish"}</button>
      <button type="button" disabled={busy || !canExport} onClick={() => void download("html")}>Export HTML</button>
      <button type="button" disabled={busy || !canExport} onClick={() => void download("zip")}>Export ZIP</button>
      {(awaitingCheck || status?.publication?.state === "live") && <button type="button" disabled={busy} onClick={() => void act("verify")}>Check public URL</button>}
      {canTakeOffline && <button type="button" disabled={busy} onClick={() => void act("unpublish")}>Take offline</button>}
    </div>
    <h3>Collect signups</h3>
    <button type="button" disabled={busy} onClick={() => void act("test")}>Test waitlist</button>
    <label>Consented email addresses<textarea value={emails} onChange={event => setEmails(event.target.value)} placeholder="Paste email addresses separated by commas or new lines" /></label>
    <label><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} /> Each person agreed to early-access updates about your product.</label>
    <button type="button" disabled={busy || !consent || !emails.trim()} onClick={() => void act("signups")}>Import signups</button>
    <ErrorNotice error={error} source="site.local.publication" />
    {note && <p role="status">{note}</p>}
  </section>;
}
