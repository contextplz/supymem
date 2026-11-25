"use client";

import Link from "@/components/chat/ChatLink";
import { useCallback, useEffect, useRef, useState } from "react";
import { useOperationsScope } from "@/components/ops/OpsWorkspaceGate";
import { diagnosisClient, DiagnosisApiError, rejectedBeforeEnqueue, type GtmVenture } from "@/lib/gtmDiagnosisApi";
import { approveDiagnosis, diagnosisActive, diagnosisNeedsResolution, gtmIntentKey, gtmRequestFence,
  readGtmIntent, persistDiagnosisIntent, validDiagnosisInput, type DiagnosisInput, type DiagnosisIntent,
  type DiagnosisQuote, type DiagnosisRun, type GtmOwner } from "@/lib/gtmDiagnosisIntent";
import { errorCodeCopy } from "@/lib/errorCopy";
import { VentureWaitlist } from "@/components/site/VentureWaitlist";
import { diagnosisExcerpt, diagnosisFirstAction, diagnosisNextStep } from "@/lib/gtmDiagnosisPresentation";
import styles from "@/components/outreach/Outreach.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { GtmJourney } from "./GtmJourney";
import { FounderCoaching } from "@/components/planning/FounderCoaching";

const readable = (text: string) => text.replaceAll("_", " ");
const runWhen = (iso: string) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "earlier" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); };
const message = (error: unknown) => error instanceof Error ? error.message : "Diagnosis status could not be loaded.";

export function GtmGod() {
  const scope = useOperationsScope();
  const [ventures, setVentures] = useState<GtmVenture[]>([]);
  const [ventureId, setVentureId] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const workspaceId = scope?.workspaceId || "", actorId = scope?.actorId || "";

  useEffect(() => {
    let current = true;
    const changed = () => { current = false; setVentureId(""); setVentures([]); };
    window.addEventListener("supymem-session-changed", changed);
    setVentures([]); setVentureId(""); setError(""); setLoading(true);
    if (workspaceId && actorId) {
      void diagnosisClient.ventures(workspaceId).then(rows => {
        if (!current) return;
        setVentures(rows);
        if (rows.some(row => row.id === scope?.ventureId)) setVentureId(scope?.ventureId || "");
        setLoading(false);
      }).catch(err => { if (current) { setError(message(err)); setLoading(false); } });
    } else setLoading(false);
    return () => { current = false; window.removeEventListener("supymem-session-changed", changed); };
  }, [workspaceId, actorId, scope?.ventureId, refresh]);

  const owner = actorId && workspaceId && ventures.some(row => row.id === ventureId)
    ? { actorId, workspaceId, ventureId } : null;
  const resolving = !owner && loading && !error && Boolean(scope?.ventureId); // venture already in context: no picker flash
  return <div className={styles.wrap}>
    {resolving && <p className={styles.muted} role="status">Loading your venture…</p>}
    {!owner && !resolving && <section className={styles.card}>
      <h2>Which venture are we growing?</h2>
      <p className={styles.muted}>Choose a venture to review its audience, prospects and outreach.</p>
      <div className={styles.row}>{ventures.map(row => <Link key={row.id} className={styles.btn} href={`/gtm?workspace=${encodeURIComponent(workspaceId)}&venture=${encodeURIComponent(row.id)}`}>{row.name || "Your venture"}</Link>)}</div>
      {loading && <p role="status">Loading companies…</p>}
      {!loading && !ventures.length && !error && <p>Create a company in this workspace before requesting a diagnosis.</p>}
      <ErrorNotice error={error} source="GtmGod.error" className={styles.err} />
      <button data-usage-control="gtm_companies_refresh" className={`${styles.btn} ${styles.btnGhost}`} disabled={loading} onClick={() => setRefresh(value => value + 1)}>Refresh companies</button>
    </section>}
    {owner && <FounderCoaching key={gtmIntentKey(owner)} ventureId={owner.ventureId} />}
    {owner && <GtmJourney key={gtmIntentKey(owner)} owner={owner} />}
    {owner && <details id="waitlist" className={styles.card}><summary>Waitlist and website</summary>
      <VentureWaitlist key={owner.ventureId} ventureId={owner.ventureId} usageSurface="gtm" />
      <p><Link href={`/v/${encodeURIComponent(owner.ventureId)}/site`}>Review this venture’s website and its signup link →</Link></p>
    </details>}
    {owner && <details className={styles.card}><summary>Review your diagnosis</summary><GtmDiagnosisUI key={gtmIntentKey(owner)} owner={owner} venture={ventures.find(row => row.id === ventureId)!} /></details>}
  </div>;
}

export function GtmDiagnosisUI({ owner, venture }: { owner: GtmOwner; venture: GtmVenture }) {
  const key = gtmIntentKey(owner);
  const fence = useRef(gtmRequestFence());
  const inFlight = useRef(false);
  const [input, setInput] = useState<DiagnosisInput>({ kind: "diagnose", idea: "", problem: "", who: "", users_count: 0 });
  const [quote, setQuote] = useState<DiagnosisQuote | null>(null);
  const [quotedInput, setQuotedInput] = useState<DiagnosisInput | null>(null);
  const [pending, setPending] = useState<DiagnosisIntent | null>(null);
  const [runs, setRuns] = useState<DiagnosisRun[]>([]);
  const [run, setRun] = useState<DiagnosisRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const [receiptError, setReceiptError] = useState(false);
  const [error, setError] = useState("");
  const [contextLoaded, setContextLoaded] = useState(false);
  const [contextSources, setContextSources] = useState<string[]>([]);
  const [pollEpoch, setPollEpoch] = useState(0);
  const inputTouched = useRef(false);

  const adopt = useCallback((value: DiagnosisRun) => {
    setRun(value); setRuns(rows => [value, ...rows.filter(row => row.run_id !== value.run_id)]);
  }, []);
  const restore = useCallback(async () => {
    const current = fence.current.capture();
    if (!current()) return;
    const value = await diagnosisClient.runs(owner);
    if (!current()) return;
    setRuns(value.runs); setRun(value.runs.find(diagnosisNeedsResolution) || value.runs[0] || null); setReady(true);
    setPollEpoch(value => value + 1);
  }, [key]); // The key includes actor, workspace, and venture.

  useEffect(() => {
    fence.current.activate(key);
    const current = fence.current.capture();
    const changed = () => {
      fence.current.invalidate(); setQuote(null); setQuotedInput(null); setPending(null); setRun(null); setRuns([]); setReady(false); setBusy(false);
    };
    window.addEventListener("supymem-session-changed", changed);
    try {
      const raw = localStorage.getItem(key), saved = readGtmIntent(raw, owner);
      if (raw && !saved) { setReceiptError(true); setError("The saved approval receipt is unreadable. Resolve existing work before another approval; no replacement request was submitted."); }
      setPending(saved);
      if (saved) setInput({ kind: "diagnose", idea: saved.request.idea, problem: saved.request.problem, who: saved.request.who, users_count: saved.request.users_count });
      else void diagnosisClient.context(owner, venture).then(value => {
        if (!current() || inputTouched.current) return;
        setInput(existing => ({ ...existing, idea: value.idea || existing.idea, problem: value.problem || existing.problem, who: value.who || existing.who }));
        setContextSources(value.sources); setContextLoaded(true);
      }).catch(err => { if (current()) { setContextLoaded(true); setError(message(err)); } });
    } catch { setReceiptError(true); setError("Approval storage is unavailable. Enable local storage before submitting work so a timeout can be recovered safely."); }
    void restore().catch(err => { if (current()) setError(message(err)); });
    return () => { fence.current.invalidate(); window.removeEventListener("supymem-session-changed", changed); };
  }, [key, restore]);

  async function reviewBudget() {
    const current = fence.current.capture();
    if (!current() || inFlight.current || !ready || pending || receiptError || runs.some(diagnosisNeedsResolution) || !validDiagnosisInput(input)) return;
    inFlight.current = true; setBusy(true); setError(""); setQuote(null); setQuotedInput(null);
    const snapshot = { ...input, idea: input.idea.trim(), problem: input.problem.trim(), who: input.who.trim() };
    try {
      const value = await diagnosisClient.quote(owner, snapshot);
      if (!current()) return;
      setQuote(value); setQuotedInput(snapshot);
      if (!value.sufficient_now) return;
      const intent = approveDiagnosis(owner, snapshot, value, crypto.randomUUID());
      inFlight.current = false;
      await submit(intent);
    } catch (err) { if (current()) setError(message(err)); }
    finally { if (current()) { inFlight.current = false; setBusy(false); } }
  }

  async function submit(recovered?: DiagnosisIntent) {
    const current = fence.current.capture();
    if (!current() || inFlight.current || receiptError || !ready) return;
    if (!recovered && (pending || runs.some(diagnosisNeedsResolution))) return;
    inFlight.current = true; setBusy(true); setError("");
    try {
      const intent = recovered || (quote && quotedInput ? approveDiagnosis(owner, quotedInput, quote, crypto.randomUUID()) : null);
      if (!intent) return;
      // Approval is persisted before transport, including its unchanged retry key.
      const value = await persistDiagnosisIntent(intent, localStorage, saved => {
        setPending(saved);
        return diagnosisClient.start(saved);
      });
      if (!current()) return;
      adopt(value); setPending(null); setQuote(null); setQuotedInput(null); setPollEpoch(value => value + 1);
      try { localStorage.removeItem(key); } catch { setError("The run is saved on the server. A retained local approval can safely recover this same run after restart."); }
    } catch (err) {
      if (current()) {
        // Only a definite pre-enqueue rejection permits a new quoted attempt.
        if (err instanceof DiagnosisApiError && rejectedBeforeEnqueue(err)) {
          setPending(null); setQuote(null); setQuotedInput(null);
          try { localStorage.removeItem(key); } catch { setReceiptError(true); }
        }
        setError(message(err));
      }
    } finally { if (current()) { inFlight.current = false; setBusy(false); } }
  }

  useEffect(() => {
    if (!run || !diagnosisActive(run)) return;
    const current = fence.current.capture(), runId = run.run_id;
    let stopped = false, count = 0;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (stopped || !current()) return;
      try {
        const value = await diagnosisClient.run(owner, runId);
        if (stopped || !current()) return;
        adopt(value);
        if (diagnosisActive(value) && ++count < 120) timer = setTimeout(poll, 5000);
        else if (diagnosisActive(value)) setError("Work remains on the server. Refresh status to continue watching this diagnosis.");
      } catch (err) { if (!stopped && current()) setError(`${message(err)} Work remains on the server; refreshing status does not submit another request.`); }
    }
    timer = setTimeout(poll, 1500);
    return () => { stopped = true; clearTimeout(timer); };
  }, [key, run?.run_id, run?.state, pollEpoch, adopt]);

  async function refreshStatus() {
    const current = fence.current.capture();
    if (!current() || inFlight.current) return;
    setError("");
    try { await restore(); } catch (err) { if (current()) setError(message(err)); }
  }
  const locked = busy || !!pending || !!quote || runs.some(diagnosisNeedsResolution);
  const diagnosis = run?.result?.diagnosis;
  const completed = run?.state === "completed" && !!diagnosis;
  const nextAction = diagnosisFirstAction(diagnosis);
  const nextStep = nextAction ? diagnosisNextStep(nextAction.kind, owner.ventureId) : null;
  const inputSection = <section className={styles.card} aria-label="GTM diagnosis input">
      <h2>What needs diagnosing?</h2>
      <p className={styles.muted}>You can edit your saved company context before requesting a diagnosis.</p>
      {contextLoaded && contextSources.length > 0 && <p role="status" className={styles.muted}>Prefilled from your saved {contextSources.includes("charter") ? "idea and approved plan" : contextSources.includes("idea_session") ? "idea and confirmed answers" : "idea"}. Nothing has been submitted.</p>}
      <label><span className={styles.label}>What are you building?</span><textarea className={styles.textarea} maxLength={12000} value={input.idea} disabled={locked} onChange={event => { inputTouched.current = true; setInput({ ...input, idea: event.target.value }); }} /></label>
      <label><span className={styles.label}>What is broken today?</span><textarea className={styles.textarea} maxLength={12000} value={input.problem} disabled={locked} onChange={event => { inputTouched.current = true; setInput({ ...input, problem: event.target.value }); }} /></label>
      <label><span className={styles.label}>Who feels it?</span><input className={styles.input} maxLength={2000} value={input.who} disabled={locked} onChange={event => { inputTouched.current = true; setInput({ ...input, who: event.target.value }); }} /></label>
      <label><span className={styles.label}>Reported users (unverified)</span><input className={styles.input} type="number" min={0} max={1000000000} step={1} value={input.users_count} disabled={locked} onChange={event => { inputTouched.current = true; setInput({ ...input, users_count: event.target.value === "" ? 0 : Number(event.target.value) }); }} /></label>
      <button data-usage-control="gtm_diagnosis_budget" className={styles.btn} disabled={!ready || locked || receiptError || !validDiagnosisInput(input)} onClick={() => void reviewBudget()}>Run diagnosis</button>
      {!ready && <p role="status">Restore saved diagnosis status before approving new work.</p>}
    </section>;
  return <>
    {completed && diagnosis && <section className={`${styles.card} ${styles.nextStep}`} aria-label="GTM diagnosis next step">
      <p className={styles.pill}>Saved advice · not a verdict</p>
      <h2>One useful next step</h2>
      {nextAction ? <>
        <p><strong>{readable(nextAction.kind)}:</strong> {diagnosisExcerpt(nextAction.instruction, 220)}</p>
        <p>{diagnosisExcerpt(nextAction.reason, 180)}</p>
      </> : <ErrorNotice error="The saved diagnosis did not include a usable next action. Review the complete report before deciding what to do." source="GtmGod.the_saved_diagnosis_did" />}
      <p className={styles.muted}>Suggested from founder-reported inputs. Supymem has not verified demand or performed this action.</p>
      {nextStep ? <>
        <p>{nextStep.guidance}</p>
        <Link className={styles.btn} href={nextStep.href} data-usage-control={nextStep.control}>{nextStep.label} →</Link>
      </> : nextAction && <ErrorNotice error="This action has no guided workflow. Read the complete report and decide how to do it yourself." source="GtmGod.this_action_has_no" />}
    </section>}
    {completed ? <details className={styles.card}><summary>Review inputs or run another diagnosis</summary>{inputSection}</details> : inputSection}
    {quote && !quote.sufficient_now && <ErrorNotice error={errorCodeCopy("insufficient_credits")} source="GtmGod.limit" />}
    {pending && <section className={styles.card} role="status">
      <h2>Check your submitted request</h2>
      <p>You can recover this same request without starting a second diagnosis.</p>
      <button data-usage-control="gtm_diagnosis_recover" className={styles.btn} disabled={busy || !ready || receiptError} onClick={() => void submit(pending)}>Recover same approved request</button>
    </section>}
    <section className={styles.card} aria-label="Saved diagnosis status">
      <h2>Saved diagnoses</h2>
      <button data-usage-control="gtm_diagnosis_refresh" className={`${styles.btn} ${styles.btnGhost}`} disabled={busy} onClick={() => void refreshStatus()}>Refresh diagnosis status</button>
      {!runs.length && ready && <p>No diagnosis has been saved for this company.</p>}
      {runs.length > 0 && <label><span className={styles.label}>Run history</span><select data-usage-control="gtm_run_select" className={styles.input} value={run?.run_id || ""} onChange={event => { setRun(runs.find(row => row.run_id === event.target.value) || null); setPollEpoch(value => value + 1); }}>
        {runs.map(row => <option key={row.run_id} value={row.run_id}>{readable(row.state)} · {runWhen(row.created_at)}</option>)}
      </select></label>}
      {run && <div aria-live="polite">
        <p><span className={styles.pill}>{readable(run.state)}</span> {readable(run.phase)}</p>
        {run.state === "completed" && diagnosis && <p>Diagnosis delivered. Strategy execution, research, publishing, and outreach remain separate work.</p>}
        {run.state === "completed" && !diagnosis && <ErrorNotice error="The saved result is unavailable. Refresh status before treating this diagnosis as delivered." source="GtmGod.the_saved_result_is" className={styles.err} />}
        {run.state === "failed" && run.calls_used === 0 && !diagnosisNeedsResolution(run) && <p>Nothing was started. Correct the recorded issue, then retry your diagnosis.</p>}
        {run.state === "needs_input" && <p>Your diagnosis needs attention. Review the recorded reason, then refresh status before trying again.</p>}
        {run.reserved_micro > 0 && <p>Your earlier request is still being checked. Refresh status before starting another attempt.</p>}
        {run.error_code && <ErrorNotice error={errorCodeCopy(run.error_code) ?? "Your diagnosis could not finish. Refresh its saved status to check what happened."} className={styles.err} source="gtm.diagnosis_run" />}
        {diagnosis && <details><summary>Read the complete saved diagnosis</summary><article>
          <h3>{diagnosis.bottleneck}</h3><p>{diagnosis.rationale}</p>
          <h3>Assumptions</h3><ul>{diagnosis.assumptions.map((value, index) => <li key={index}>{value}</li>)}</ul>
          <h3>Evidence gaps</h3><ul>{diagnosis.evidence_gaps.map((value, index) => <li key={index}>{value}</li>)}</ul>
          <h3>Suggested next steps</h3><ul>{diagnosis.next_actions.map((action, index) => <li key={index}><strong>{readable(action.kind)}:</strong> {action.instruction}<p className={styles.muted}>{action.reason}</p></li>)}</ul>
          <p className={styles.muted}>These are advisory steps. None has been executed or verified by completing this diagnosis.</p>
        </article></details>}
      </div>}
      <ErrorNotice error={error} source="GtmGod.error" className={styles.err} />
    </section>
    <details className={styles.card}><summary>Other reviewed workflows</summary>
      <p className={styles.muted}>Strategy execution is not implemented here. Research, content, and external actions require their own review and authorization.</p>
      <div className={styles.row}>
        <Link className={`${styles.btn} ${styles.btnGhost}`} href={`/v/${encodeURIComponent(owner.ventureId)}`}>Company workspace</Link>
        <Link className={`${styles.btn} ${styles.btnGhost}`} href={`/connections?workspace=${encodeURIComponent(owner.workspaceId)}&venture=${encodeURIComponent(owner.ventureId)}`}>Connections</Link>
      </div>
    </details>
  </>;
}
