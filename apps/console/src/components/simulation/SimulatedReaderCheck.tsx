"use client";

import { useEffect, useRef, useState } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import type { GtmOwner } from "@/lib/gtmDiagnosisIntent";

type Draft = { doc_id: string; revision: string; subject?: string };
type Quote = { workspace_id: string; venture_id: string; actor_id: string; input_digest: string; quote_digest: string; epoch: number; expires_at: number; resources: { ceiling_micro: number; source: string; model: string }; existing_job?: Run | null };
type Run = { workspace_id: string; venture_id: string; actor_id: string; input_digest: string; job_id: string; state: string; uncertain: boolean; credits_held: boolean; credits_micro: number; result?: {
  clarity_order: string[]; explanation: string; top_objections: string[]; confusing_phrases: string[]; suggested_edit: string; interview_questions: string[];
  persona_count: number; model_call_count: number;
} };
type Receipt = { key: string; quote?: Quote; run?: Run; error?: string };

/** Optional review detail. Mounting never starts a model call. */
export function SimulatedReaderCheck({ owner, drafts, enabled = false }: { owner: GtmOwner; drafts: Draft[]; enabled?: boolean }) {
  const refs = drafts.slice(0, 3).map(({ doc_id, revision }) => ({ doc_id, revision }));
  const key = JSON.stringify([owner.actorId, owner.workspaceId, owner.ventureId, verifiedSessionEpoch(), refs,
    typeof window === "undefined" ? "" : window.location.href]);
  const live = useRef(key); live.current = key;
  const generation = useRef(0);
  const intent = useRef<{ key: string; quote: Quote; request_id: string } | null>(null);
  const abort = useRef<AbortController | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [busy, setBusy] = useState(false);
  const current = receipt?.key === key ? receipt : null;
  const root = `/api/api/v1/ventures/${encodeURIComponent(owner.ventureId)}/simulation/rehearse?workspace_id=${encodeURIComponent(owner.workspaceId)}`;

  useEffect(() => {
    const retire = () => { generation.current++; abort.current?.abort(); intent.current = null; setReceipt(null); setBusy(false); };
    retire();
    for (const event of ["supymem-session-changed", "popstate", "hashchange", "pagehide"]) window.addEventListener(event, retire);
    return () => { retire(); for (const event of ["supymem-session-changed", "popstate", "hashchange", "pagehide"]) window.removeEventListener(event, retire); };
  }, [key, enabled]);

  async function request(kind: "quote" | "start" | "status", saved = current) {
    if (!enabled || busy || !owner.actorId || verifiedSessionSubject() !== owner.actorId || refs.length === 0) return;
    const ticket = ++generation.current, captured = key, epoch = verifiedSessionEpoch();
    const location = window.location.href;
    const controller = new AbortController(); abort.current = controller;
    const owned = () => live.current === captured && generation.current === ticket && epoch === verifiedSessionEpoch()
      && verifiedSessionSubject() === owner.actorId && window.location.href === location && !controller.signal.aborted;
    setBusy(true);
    try {
      let path = root, body: unknown;
      if (kind === "quote") {
        intent.current = null;
        path = root.replace("?", "/quote?"); body = { drafts: refs };
      } else if (kind === "start") {
        const quote = saved?.quote;
        if (!quote || (!intent.current && quote.expires_at * 1000 <= Date.now())) throw new Error("This estimate expired. Review a fresh estimate before running.");
        if (!intent.current || intent.current.key !== captured) intent.current = { key: captured, quote, request_id: crypto.randomUUID() };
        body = { drafts: refs, epoch: intent.current.quote.epoch, quote_digest: intent.current.quote.quote_digest,
          confirmed: true, request_id: intent.current.request_id };
      } else {
        if (!current?.run) return;
        path = root.replace("?", `/${encodeURIComponent(current.run.job_id)}?`);
      }
      const response = await api(path, { method: kind === "status" ? "GET" : "POST", signal: controller.signal,
        headers: { "X-Workspace-ID": owner.workspaceId }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      if (!owned()) return;
      if (!response.ok) throw new Error(apiFailure(response, "running your saved check"));
      const value = response.data as Quote | Run;
      if (!value || value.actor_id !== owner.actorId || value.workspace_id !== owner.workspaceId || value.venture_id !== owner.ventureId
          || (kind !== "quote" && value.input_digest !== saved?.quote?.input_digest)) throw new Error("The response did not match the reviewed drafts.");
      const quote = value as Quote;
      if (kind === "quote" && (!Number.isSafeInteger(quote.epoch) || !Number.isFinite(quote.expires_at)
          || !/^[a-f0-9]{64}$/.test(quote.quote_digest) || !Number.isSafeInteger(quote.resources?.ceiling_micro)
          || quote.resources.ceiling_micro < 0 || typeof quote.resources.model !== "string")) throw new Error("The estimate could not be verified. Refresh before running.");
      if (kind === "quote" && quote.existing_job && (quote.existing_job.actor_id !== owner.actorId || quote.existing_job.workspace_id !== owner.workspaceId
          || quote.existing_job.venture_id !== owner.ventureId || quote.existing_job.input_digest !== quote.input_digest)) throw new Error("The saved check did not match these drafts.");
      const run = kind === "quote" ? quote.existing_job : value as Run;
      if (run && (typeof run.job_id !== "string" || !run.job_id || !["queued", "running", "completed", "needs_attention"].includes(run.state)
          || (kind === "status" && run.job_id !== current?.run?.job_id))) throw new Error("The saved check could not be verified.");
      setReceipt(kind === "quote" ? { key: captured, quote, run: quote.existing_job ?? undefined } : { key: captured, quote: saved?.quote, run: value as Run });
      if (kind === "quote" && !quote.existing_job) await request("start", {key: captured, quote});
    } catch (error) {
      if (owned()) setReceipt({ ...saved, key: captured, error: error instanceof Error ? error.message : "The saved check could not be confirmed." });
    } finally { if (owned()) setBusy(false); }
  }

  if (!enabled || !refs.length) return null;
  const result = current?.run?.result;
  return <details><summary>Check clarity with simulated readers</summary>
    <p><strong>Simulated · AI personas</strong></p>
    <p>Not customer evidence. Use it to decide what to test with real people.</p>
    <p>Eight hypothetical readers compare up to three saved outreach drafts in four batched model calls. Your messages stay saved for your review.</p>
    <ol>{drafts.slice(0, 3).map((draft, i) => <li key={draft.doc_id}>Draft {i + 1}: {draft.subject || "Saved outreach message"}</li>)}</ol>
    {!current?.quote && <button type="button" disabled={busy} onClick={() => void request("quote")}>Run check</button>}
    {current?.quote && !current.run && <div>
      <button type="button" disabled={busy} onClick={() => void request("start")}>{intent.current ? "Check saved run" : "Run check"}</button>
    </div>}
    <ErrorNotice error={current?.error} source="simulation.rehearse" />
    {current?.run && <>
      <p role="status">{current.run.state === "completed" ? "Check saved" : current.run.state === "needs_attention" ? "Check needs attention. It will not retry automatically." : "Checking saved drafts…"}</p>
      {current.run.uncertain && <p>The result is uncertain. Refresh your saved check before taking further action. This run will not retry.</p>}
      {current.run.credits_held && <p>Your check is still being resolved. Refresh its status before starting another run.</p>}
      <button type="button" disabled={busy} onClick={() => void request("status")}>Refresh saved check</button>
    </>}
    {result && <>
      <p>Relative clarity: {result.clarity_order.map(id => `Draft ${id.slice(1)}`).join(" → ")}. {result.explanation}</p>
      <p>{result.persona_count} simulated readers · {result.model_call_count} batched calls. These are synthetic reactions from one model, not independent customer interviews.</p>
      <ul>{result.top_objections.map((item, i) => <li key={i}>{item}</li>)}</ul>
      {!!result.confusing_phrases.length && <><p>Phrases to test with real people:</p><ul>{result.confusing_phrases.map((item, i) => <li key={i}>{item}</li>)}</ul></>}
      <label>Suggested edit to review<textarea readOnly value={result.suggested_edit} /></label>
      <p>Ask real people before changing your outreach:</p><ul>{result.interview_questions.map((item, i) => <li key={i}>{item}</li>)}</ul>
    </>}
  </details>;
}
