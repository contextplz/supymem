"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Deployment = {
  id: string; app_id: string; state: string; version: number; url?: string | null;
  unpublish?: { job_id: string; state: string; error?: string | null } | null;
};
const ACTIVE = new Set(["pending", "scheduled", "retry_wait", "running", "leased"]);

export function UnpublishSite({ ventureId, onOfflineUrls }: {
  ventureId: string; onOfflineUrls: (urls: string[]) => void;
}) {
  const [targets, setTargets] = useState<Deployment[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const mounted = useRef(false);
  const revision = useRef(0);
  const refresh = useCallback(async () => {
    const generation = ++revision.current;
    const apps = await api(`/api/ventures/${encodeURIComponent(ventureId)}/apps`);
    if (!apps.ok) throw new Error("Hosting status could not be loaded. Refresh before changing the public landing page.");
    const rows = (apps.data as { items?: { id: string }[] }).items || [];
    const resolved = await Promise.all(rows.map(async (app) => {
      const result = await api(`/api/apps/${encodeURIComponent(app.id)}/deployments`);
      if (!result.ok) throw new Error("Hosting status could not be verified. Refresh before changing the public landing page.");
      const items = (result.data as { items?: Deployment[] }).items || [];
      return items.find((dep) => dep.state === "live") || (items[0]?.state === "unpublished" ? items[0] : null);
    }));
    if (!mounted.current || generation !== revision.current) return;
    const current = resolved.filter((dep): dep is Deployment => Boolean(dep));
    setTargets(current);
    onOfflineUrls(current.filter((dep) => dep.state === "unpublished" && dep.url).map((dep) => dep.url!));
  }, [ventureId, onOfflineUrls]);
  useEffect(() => {
    mounted.current = true;
    void refresh().catch((failure: unknown) => {
      if (mounted.current) setError(failure instanceof Error ? failure.message : "Hosting status could not be loaded.");
    });
    return () => { mounted.current = false; revision.current += 1; };
  }, [refresh]);
  const pending = targets.some((dep) => dep.unpublish && ACTIVE.has(dep.unpublish.state));
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => {
      void refresh().catch(() => { if (mounted.current) setError("Unpublish status could not be checked. The operation may still be running. Refresh to recover its saved state."); });
    }, 2500);
    return () => window.clearInterval(timer);
  }, [pending, refresh]);

  async function submit(target: Deployment) {
    if (busy) return;
    setBusy(true); setError(""); setNote("");
    try {
      const result = await api(`/api/apps/${encodeURIComponent(target.app_id)}/deployments/${encodeURIComponent(target.id)}/unpublish`, { method: "POST" });
      if (!mounted.current) return;
      if (!result.ok) throw new Error("Unpublishing could not be queued. The landing page may have changed or another publication is in progress. Refresh the current hosting status and review it again.");
      const returned = result.data as Deployment;
      setNote(returned.state === "unpublished" ? "This landing page is already verified offline." : "Unpublishing is queued. The landing page may still be public until hosting and public readback confirm it is offline.");
      await refresh();
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error ? failure.message : "The request may have reached hosting. Refresh its saved status before retrying.");
    } finally { if (mounted.current) setBusy(false); }
  }

  if (!targets.length && !error) return null;
  return <section aria-label="Public hosting controls">
    <h2>Public hosting</h2>
    {targets.map((dep) => <div key={dep.id}>
      <p>{dep.url} · Version {dep.version}</p>
      {dep.state === "unpublished" ? <p role="status">Verified offline. Your source and saved waitlist submissions are retained. Review the accepted preview to publish again.</p>
        : dep.unpublish && ACTIVE.has(dep.unpublish.state) ? <p role="status">Unpublishing is {dep.unpublish.state === "retry_wait" ? "waiting for another verification attempt" : "in progress"}. This landing page may still be public until verification completes.</p>
          : <><p>{dep.unpublish ? "The last unpublish attempt did not confirm completion. Refresh its saved status, or review and retry this exact landing page." : "This deployment is live."}</p>
            <button type="button" disabled={busy} onClick={() => void submit(dep)}>Unpublish landing page</button></>}
    </div>)}
    {note && <p role="status">{note}</p>}
    <ErrorNotice error={error} source="site.unpublish" />
    <button type="button" disabled={busy} onClick={() => { setError(""); void refresh().catch(() => setError("Hosting status could not be loaded. Try again.")); }}>Refresh hosting status</button>
  </section>;
}
