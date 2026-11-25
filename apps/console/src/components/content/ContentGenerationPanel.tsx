"use client";

import { runProgress } from "@/lib/contentProgress";
import { useCallback, useEffect, useRef, useState } from "react";
import { opsApi } from "@/lib/opsApi";
import { useOperationsScope } from "@/components/ops/OpsWorkspaceGate";
import { contentIntentOwner, contentIntentKey, saveContentIntent, readContentIntent, contentRequestFence, contentRejectedBeforeEnqueue } from "@/lib/contentIntent";
import { contentCompletionTracker, contentResultKey } from "@/lib/contentCompletion";
import styles from "./ContentStudio.module.css";
import { errorCodeCopy, opsErrorMessage } from "@/lib/errorCopy";
import { ApproveTimeout, channelName, draftGate, withDeadline, quoteContentWithTopicRecovery } from "@/lib/contentFounder";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";

type Intent = { kind: "topics" | "draft"; platform?: string; nonce: number };
type Request = { venture_id: string; kind: "topics" | "draft"; brief_id: string; platform?: string; count: number };
type Quote = { input_digest: string; resource_digest: string; ceiling_micro: number; available_micro: number | null;
  sufficient_now: boolean; provider: string; model: string; payer: string; brief_version: number; topic_id?: string; call_budget: number };
type Envelope = Request & { idempotency_key: string; input_digest: string; resource_digest: string; ceiling_micro: number };
type Run = { run_id: string; venture_id: string; kind: string; state: string; phase: string; error_code: string | null;
  brief_version: number; brief_changed: boolean; ceiling_micro: number; reserved_micro: number; consumed_micro: number; calls_used: number;
  call_budget: number; payer: string; result: { topic_ids?: string[]; post_id?: string } };
const active = (run: Run | null) => !!run && ["queued", "running"].includes(run.state);
const newKey = () => globalThis.crypto?.randomUUID?.() ?? `k-${Date.now()}-${Math.random().toString(36).slice(2)}`;

export function ContentGenerationPanel({ briefId, intent, onComplete, founder = false, hasTopics = false }: {
  briefId?: string; intent: Intent | null; onComplete: () => Promise<void>; founder?: boolean; hasTopics?: boolean;
}) {
  const local = useLocalMode();
  const scope = useOperationsScope();
  const venture = scope?.ventureId;
  const owner = contentIntentOwner(scope);
  const scopeKey = contentIntentKey(owner);
  const fence = useRef(contentRequestFence());
  const [quote, setQuote] = useState<Quote | null>(null);
  const [request, setRequest] = useState<Request | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [pending, setPending] = useState<Envelope | null>(null);
  const [platform, setPlatform] = useState("linkedin");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pollEpoch, setPollEpoch] = useState(0);
  const storageKey = scopeKey;
  const completed = useRef(contentCompletionTracker());
  const chainDraft = useRef(false);

  const restore = useCallback(async () => {
    const current = fence.current.capture();
    if (!venture || !current()) return;
    const result = await opsApi.get<{ runs: Run[] }>(`/content/generation/runs?venture_id=${encodeURIComponent(venture)}`);
    if (!current()) return;
    setRun(result.runs.find(active) || result.runs[0] || null);
  }, [venture, scopeKey]);

  useEffect(() => {
    fence.current.activate(scopeKey);
    const current = fence.current.capture();
    const changed = () => {
      fence.current.invalidate();
      chainDraft.current = false;
      setQuote(null); setRequest(null); setRun(null); setPending(null); setBusy(false);
    };
    window.addEventListener("supymem-session-changed", changed);
    setQuote(null); setRequest(null); setRun(null); setPending(null); setError(""); setBusy(false);
    completed.current = contentCompletionTracker();
    chainDraft.current = false;
    try {
      const saved = storageKey ? localStorage.getItem(storageKey) : null;
      setPending(readContentIntent<Envelope>(saved, owner));
    } catch { /* A corrupt local receipt never authorizes a request. */ }
    void restore().catch(() => { if (current()) setError("Saved generation status is unavailable. Refresh status to reconnect."); });
    return () => { fence.current.invalidate(); window.removeEventListener("supymem-session-changed", changed); };
  }, [restore, scopeKey, storageKey, venture]);

  const preview = useCallback(async (kind: "topics" | "draft", selectedPlatform?: string) => {
    const current = fence.current.capture();
    if (!current() || !venture || !briefId || busy || active(run) || pending) return;
    setBusy(true); setError(""); setQuote(null);
    if (selectedPlatform) setPlatform(selectedPlatform);
    if (run && !active(run)) setRun(null); // a finished or failed run never blocks a fresh quote
    const req: Request = { venture_id: venture, kind, brief_id: briefId, count: 12, ...(selectedPlatform ? { platform: selectedPlatform } : {}) };
    try {
      const result = await quoteContentWithTopicRecovery(req, value => opsApi.post<Quote>("/content/generation/quote", value));
      if (!current()) return;
      if (result.plannedTopics) chainDraft.current = true;
      setQuote(result.quote); setRequest(result.request);
      if (result.quote.sufficient_now) await begin({ ...result.request, idempotency_key: newKey(),
        input_digest: result.quote.input_digest, resource_digest: result.quote.resource_digest,
        ceiling_micro: result.quote.ceiling_micro });
    } catch (err) { if (current()) setError(opsErrorMessage(err, "Could not quote this generation.")); }
    finally { if (current()) setBusy(false); }
  }, [venture, briefId, busy, run, pending, scopeKey]);

  const lastIntent = useRef(0);
  useEffect(() => {
    if (!intent || intent.nonce === lastIntent.current) return;
    lastIntent.current = intent.nonce;
    void preview(intent.kind, intent.platform);
  }, [intent, preview]);

  async function begin(recover?: Envelope) {
    const current = fence.current.capture();
    if (busy) return;
    if (!current() || !venture || !owner) { setError("Your session changed. Reopen Content and ask for the draft again."); return; }
    const envelope = recover || (quote && request && quote.sufficient_now ? { ...request,
      idempotency_key: newKey(), input_digest: quote.input_digest,
      resource_digest: quote.resource_digest, ceiling_micro: quote.ceiling_micro } : null);
    if (!envelope) { setError("This estimate is no longer valid. Choose Draft a post again."); return; }
    setBusy(true); setError("");
    const apply = (value: Run) => { setRun(value); setQuote(null); setRequest(null); setPending(null);
      localStorage.removeItem(storageKey); setPollEpoch(v => v + 1); };
    try {
      // Save the exact approved request before transport. Recovery never creates
      // another paid intent merely because the HTTP response was lost.
      localStorage.setItem(storageKey, saveContentIntent(owner, envelope));
      setPending(envelope);
      const sent = opsApi.post<Run>(envelope.kind === "topics" ? "/content/topics/refill" : "/content/draft", envelope);
      // A request that never answers must not leave a silent disabled button; a late answer is still applied.
      void sent.then(value => { if (current()) apply(value); }, () => undefined);
      const value = await withDeadline(sent);
      if (!current()) return;
      apply(value);
    } catch (err) {
      if (current()) {
        const message = err instanceof ApproveTimeout
          ? "No answer yet. Your request is saved; use Recover submitted request, or wait and refresh status."
          : opsErrorMessage(err, "The request result is unknown. Recover this exact request before starting another.");
        if (contentRejectedBeforeEnqueue(err)) { setPending(null); localStorage.removeItem(storageKey); setQuote(null); setRequest(null); }
        setError(message);
      }
    }
    finally { if (current()) setBusy(false); }
  }

  useEffect(() => {
    if (!run || !active(run) || !venture) return;
    const current = fence.current.capture();
    let cancelled = false, count = 0;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (cancelled || !current()) return;
      try {
        const value = await opsApi.get<Run>(`/content/generation/runs/${encodeURIComponent(run!.run_id)}?venture_id=${encodeURIComponent(venture!)}`);
        if (cancelled || !current()) return;
        setRun(value);
        if (active(value) && ++count < 120) timer = setTimeout(poll, 5000);
        else if (active(value)) setError(local ? "Your writing continues on your laptop while your app is running. Choose Refresh generation status when you return." : "The job is still running on the server. Refresh status when you return.");
      } catch { if (!cancelled && current()) setError(local ? "Your writing status could not reconnect. Your requested writing can continue on your laptop while your app is running. Choose Refresh generation status to reconnect." : "Status connection interrupted. Work remains on the server; refresh status to reconnect."); }
    }
    timer = setTimeout(poll, 1500);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [run?.run_id, run?.state, venture, scopeKey, pollEpoch, local]);

  useEffect(() => {
    const current = fence.current.capture();
    void completed.current.refresh(contentResultKey(run), onComplete, current).catch(() => {
      if (current()) setError("Your result is saved, but the post list could not refresh. Refresh generation status to retry loading it; no new generation is submitted.");
    });
  }, [run, onComplete, pollEpoch]);

  useEffect(() => {
    if (!chainDraft.current || !run || run.kind !== "topics" || active(run)) return;
    chainDraft.current = false;
    if (run.result.topic_ids?.length) void preview("draft", platform);
  }, [run, preview, platform]);

  function draftPosts() {
    chainDraft.current = !hasTopics;
    void preview(hasTopics ? "draft" : "topics", hasTopics ? platform : undefined);
  }

  async function refreshStatus() {
    const current = fence.current.capture();
    if (!current()) return;
    setError("");
    try { await restore(); if (current()) setPollEpoch(v => v + 1); }
    catch { if (current()) setError("Could not restore generation status."); }
  }

  const gate = draftGate({ hasVenture: !!venture && !!owner, hasBrief: !!briefId, busy, running: active(run), pending: !!pending });
  const quoting = !!quote && !!request;
  return <section className={styles.card} aria-label="Company content generation">
    {founder ? <>
      <div className={styles.cardHead}><h2>Draft a post</h2><p>Pick a channel and request your draft. Nothing is published.</p></div>
      <div className={styles.row}>
        <select data-usage-control="content_platform_select" aria-label="Channel for the draft" value={platform} onChange={e => setPlatform(e.target.value)}>
          {["linkedin", "x", "reddit"].map(p => <option key={p} value={p}>{channelName(p)}</option>)}
        </select>
        <button data-usage-control="content_draft_budget" className={styles.btn} disabled={gate.disabled || quoting} onClick={draftPosts}>Draft a post</button>
      </div>
      {gate.disabled && !quoting && <p className={styles.muted}>{gate.reason}</p>}
    </> : <>
    <div className={styles.cardHead}><h2>Plan and draft</h2><p>Create content from the saved brief. Generated posts stay editable until you review publication.</p></div>
    {!venture && <p className={styles.caveat}>Open Content from your intended venture to generate its drafts.</p>}
    <div className={styles.row}>
      <button data-usage-control="content_topics_budget" className={styles.btn} disabled={!owner || !briefId || busy || active(run) || !!pending} onClick={() => void preview("topics")}>Generate topics</button>
      <select data-usage-control="content_platform_select" aria-label="Draft platform" value={platform} onChange={e => setPlatform(e.target.value)}>
        {["linkedin", "x", "reddit", "bluesky", "mastodon", "devto"].map(p => <option key={p} value={p}>{p}</option>)}
      </select>
      <button data-usage-control="content_draft_budget" className={styles.btn} disabled={!owner || !briefId || busy || active(run) || !!pending} onClick={() => void preview("draft", platform)}>Generate drafts</button>
    </div>
    </>}
    {quote && !quote.sufficient_now && <ErrorNotice error={errorCodeCopy("insufficient_credits")} source="content.limit" />}
    {pending && <div role="status"><p>You can recover this same submitted request without starting a second run.</p>
      <button data-usage-control="content_generation_recover" className={styles.btn} disabled={busy} onClick={() => void begin(pending)}>Recover submitted request</button>
    </div>}
    {run && <div role="status" className={styles.post}>
      <p>{runProgress(run)}</p>
      {run.result.topic_ids && <p>{run.result.topic_ids.length} post ideas saved for this company.{founder ? "" : " You can now request a draft."}</p>}
      {run.result.post_id && <p>{founder ? "Your draft is in the list below. Copy it, or edit it first." : `Saved ${run.state === "refused" ? "refused content for editing" : "an editable draft"}. Publication requires a separate review.`}</p>}
      {run.error_code && <ErrorNotice error={errorCodeCopy(run.error_code) ?? "This run did not finish."} className={styles.error} source="content.run"> {errorCodeCopy(run.error_code) ?? "This run did not finish."} {run.error_code === "content_invalid_output" || run.error_code === "content_output_truncated"
        ? "The model response did not pass validation. Review this run before approving another paid attempt."
        : "Review your saved result before asking for another attempt."}</ErrorNotice>}
    </div>}
    <ErrorNotice error={error} source="ContentGenerationPanel.error" className={styles.error} />
    <button data-usage-control="content_generation_refresh" className={`${styles.btn} ${styles.btnGhost}`} disabled={busy || !owner} onClick={() => void refreshStatus()}>Refresh generation status</button>
  </section>;
}
