"use client";

import { routeInChat } from "@/lib/chatRouteAction";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { apiFailure } from "@/lib/apiFailure";

type SavedRun = { id: string; app_id: string };
type Quote = { quote_id: string; quote_digest: string; qa_ceiling_micro: number; credits: number; expires_at: string };

export function ServerCandidateQa({ ventureId, jobId, variant, planId, taskId }: {
  ventureId: string; jobId: string; variant: number; planId: string; taskId: string;
}) {
  const [quote, setQuote] = useState<Quote | null>(null), [saved, setSaved] = useState<SavedRun | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const flight = useRef<AbortController | null>(null), alive = useRef(false);
  const selectionKey = JSON.stringify([ventureId, jobId, variant, planId, taskId]);
  const selectionScope = useRef({key: selectionKey}), quoteScope = useRef<object | null>(null);
  if (selectionScope.current.key !== selectionKey) selectionScope.current = {key: selectionKey};
  const renderedSelection = selectionScope.current;
  const visibleQuote = quoteScope.current === renderedSelection ? quote : null;
  const visibleSaved = quoteScope.current === renderedSelection ? saved : null;
  const owner = useRef({ actor: verifiedSessionSubject(), epoch: verifiedSessionEpoch(),
    path: typeof window === "undefined" ? "" : window.location.pathname });
  const current = () => selectionScope.current === renderedSelection && alive.current && !flight.current?.signal.aborted && !!owner.current.actor
    && owner.current.actor === verifiedSessionSubject() && owner.current.epoch === verifiedSessionEpoch()
    && owner.current.path === window.location.pathname;
  useEffect(() => {
    alive.current = true;
    const retire = () => { flight.current?.abort(); flight.current = null; quoteScope.current = null; setQuote(null); setSaved(null); setBusy(false); setError(""); };
    retire();
    window.addEventListener("supymem-session-changed", retire);
    return () => { alive.current = false; flight.current?.abort(); window.removeEventListener("supymem-session-changed", retire); };
  }, [renderedSelection]);
  const base = `/api/ventures/${encodeURIComponent(ventureId)}/pagebuilder-admission`;
  const selection = {job_id: jobId, variant, plan_id: planId, plan_task_id: taskId};
  function open(run: SavedRun) {
    if (!current() || !/^[A-Za-z0-9_-]+$/.test(run.id) || !/^[A-Za-z0-9_-]+$/.test(run.app_id)) return;
    const href = `/v/${encodeURIComponent(ventureId)}/builds?app=${encodeURIComponent(run.app_id)}&run=${encodeURIComponent(run.id)}`;
    if (!routeInChat(href)) window.location.assign(href);
  }
  async function request(admit: boolean, reviewed = visibleQuote) {
    if (!current() || flight.current || (admit && !reviewed)) return;
    if (admit && (!Number.isFinite(Date.parse(reviewed!.expires_at)) || Date.parse(reviewed!.expires_at) <= Date.now())) { setQuote(null); setError("Your check needs a fresh estimate. Run the check again."); return; }
    const controller = new AbortController(); flight.current = controller; setBusy(true); setError("");
    try {
      const response = await api(`${base}/${admit ? "admit" : "quote"}`, {method: "POST", signal: controller.signal,
        body: JSON.stringify({...selection, ...(admit ? {quote_id: reviewed!.quote_id, quote_digest: reviewed!.quote_digest,
          approve_qa_ceiling_micro: reviewed!.qa_ceiling_micro, confirmed: true} : {})})});
      if (!current()) return;
      if (!response.ok) { setQuote(null); setError(apiFailure(response, admit ? "starting your independent check" : "preparing your independent check")); return; }
      if (admit) open(response.data as SavedRun);
      else {
        const value = response.data as Quote & {existing_run?: SavedRun};
        quoteScope.current = renderedSelection;
        setSaved(value.existing_run || null); setQuote(value.existing_run ? null : value);
        if (!value.existing_run) { flight.current = null; await request(true, value); }
      }
    } catch {
      if (current()) setError("The connection stopped. Check saved builds before trying again.");
    } finally { if (flight.current === controller) { flight.current = null; if (current()) setBusy(false); } }
  }
  return <details><summary>Use independent server QA instead</summary>
    <p>Check this exact saved draft without running a coding tool. Accepting the source and publishing remain separate reviews.</p>
    {visibleSaved ? <button type="button" disabled={busy} onClick={() => open(visibleSaved)}>Open saved QA review</button>
      : <button type="button" disabled={busy} onClick={() => void request(false)}>Run independent QA</button>}
    <ErrorNotice error={error} source="site.serverCandidateQa" />
  </details>;
}
