"use client";

import Link from "@/components/chat/ChatLink";
import { useEffect, useRef, useState } from "react";
import { OneCard } from "@/components/primitives/OneCard";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { evidenceLabel, evidenceSummary, nextStepClient, stepDestination, validCard, type ActionCard, type NextStepDecision, type StepOutcome, type StepResult } from "@/lib/nextStepEngine";
import { NEXT_STEP_TOOLS } from "./NextStepTools";

export function NextStepView({ decision, action = decision?.hero, opened = false, busy = false, error = "", current = () => true,
  onOpen, onOutcome, onRetry }: { decision: NextStepDecision | null; action?: ActionCard | null; opened?: boolean; busy?: boolean;
  error?: string; current?: () => boolean; onOpen: () => void;
  onOutcome: (outcome: StepOutcome, result?: StepResult) => Promise<void>; onRetry: () => void }) {
  if (!decision) return <section aria-label="Next best action"><ErrorNotice error={error} source="NextStepCard.load" />{!error && <p role="status">Loading your next best action…</p>}<button type="button" disabled={busy} onClick={onRetry}>Retry next step</button></section>;
  if (!action) return <section aria-label="Next best action"><p>All current suggestions are handled or snoozed. New evidence will unlock another step.</p><Link href={`/gtm?venture=${encodeURIComponent(decision.venture_id)}`}>Review your venture work</Link><button type="button" onClick={onRetry}>Refresh next step</button></section>;
  if (!validCard(action, decision.evidence) || !Object.hasOwn(NEXT_STEP_TOOLS, action.component)) return <ErrorNotice error="This tool is not supported. Refresh your next step." source="NextStepCard.registry" />;
  const Tool = NEXT_STEP_TOOLS[action.component];
  return <OneCard step="Your single next best action" title={action.title} body={action.why[0].text} status={busy ? "working" : "idle"}
    actions={<>{action.component === "journey_entry" && !busy ? <Link href={stepDestination(action.action_id, decision.venture_id)}>Do it</Link>
      : <button type="button" disabled={busy} aria-expanded={action.component === "journey_entry" ? undefined : opened} onClick={onOpen}>Do it</button>}
      <button type="button" disabled={busy} onClick={() => void onOutcome("skip")}>Skip for now</button>
      <button type="button" disabled={busy} onClick={() => void onOutcome("not_relevant")}>Not relevant</button></>}>
    {action.draft_only && <p>Your draft stays private. Ask to send or publish when you are ready.</p>}
    <details><summary>Why this?</summary>{action.why.map((why, index) => <p key={index}>{why.text}</p>)}
      <ul>{[...new Set(action.why.flatMap(why => why.evidence_ids))].map(identifier => <li id={`next-evidence-${encodeURIComponent(identifier)}`} key={identifier}>
        <strong>{evidenceLabel(decision.evidence[identifier].kind)}</strong>: {evidenceSummary(decision.evidence[identifier])}
      </li>)}</ul></details>
    {opened && action.component !== "journey_entry" && <Tool key={`${decision.decision_id}:${action.action_id}`} action={action} decision={decision} disabled={busy} current={current} onDone={result => onOutcome("done", result)} />}
    <ErrorNotice error={error} source="NextStepCard.outcome" />
    <button type="button" disabled={busy} onClick={onRetry}>Refresh saved next step</button>
  </OneCard>;
}

export function NextStepCard({ ventureId, revision = "" }: { ventureId: string; revision?: string }) {
  const [decision, setDecision] = useState<NextStepDecision | null>(null);
  const [selected, setSelected] = useState<ActionCard | null>(null);
  const [opened, setOpened] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const scope = useRef({ ventureId, subject: verifiedSessionSubject(), epoch: verifiedSessionEpoch() });
  const flight = useRef(false);
  const interacted = useRef(false);
  const refreshed = useRef("");
  const expectedSubject = verifiedSessionSubject(), expectedEpoch = verifiedSessionEpoch();
  const current = () => scope.current.ventureId === ventureId && scope.current.subject === expectedSubject && scope.current.epoch === expectedEpoch
    && expectedSubject === verifiedSessionSubject() && expectedEpoch === verifiedSessionEpoch();
  async function load(refinement = false) {
    if (refinement && interacted.current) return;
    const token = ++generation.current;
    let owner = scope.current;
    const active = () => token === generation.current && owner.ventureId === ventureId
      && owner.subject === verifiedSessionSubject() && owner.epoch === verifiedSessionEpoch();
    if (!refinement) { setError(""); setBusy(true); }
    try {
      await ensureSession();
      if (token !== generation.current) return;
      owner = { ventureId, subject: verifiedSessionSubject(), epoch: verifiedSessionEpoch() };
      scope.current = owner;
      const next = await nextStepClient.get(ventureId);
      if (!active() || refinement && interacted.current) return;
      if (!refinement) interacted.current = false;
      setDecision(next); setSelected(null); setOpened(false);
    } catch (err) { if (!refinement && active()) setError(err instanceof Error ? err.message : "Next step could not be loaded."); }
    finally { if (!refinement && active()) setBusy(false); }
  }
  useEffect(() => {
    const reset = () => { generation.current++; flight.current = false; interacted.current = false; refreshed.current = ""; scope.current = { ventureId, subject: verifiedSessionSubject(), epoch: verifiedSessionEpoch() }; setDecision(null); setSelected(null); setOpened(false); void load(); };
    reset(); window.addEventListener("supymem-session-changed", reset);
    return () => { generation.current++; scope.current.ventureId = ""; window.removeEventListener("supymem-session-changed", reset); };
  }, [ventureId, revision]);
  useEffect(() => {
    if (!decision || decision.source !== "rules" || !decision.refining || !current()) return;
    const refresh = () => {
      if (interacted.current || !current() || refreshed.current === decision.decision_id) return;
      refreshed.current = decision.decision_id;
      void load(true);
    };
    const timer = window.setTimeout(refresh, 2500);
    window.addEventListener("focus", refresh);
    return () => { window.clearTimeout(timer); window.removeEventListener("focus", refresh); };
  }, [decision, ventureId]);
  async function outcome(value: StepOutcome, result: StepResult = {}) {
    const action = selected || decision?.hero;
    if (!decision || !action || flight.current || !current()) return;
    interacted.current = true;
    flight.current = true; setBusy(true); setError("");
    try {
      await nextStepClient.outcome(decision, action, value, result);
      if (!current()) return;
      window.dispatchEvent(new CustomEvent("supymem-run-status-refresh", { detail: { ventureId } }));
      await load();
    } catch (err) { if (current()) setError(err instanceof Error ? err.message : "Outcome was not confirmed. Refresh saved work before retrying."); }
    finally { if (current()) { flight.current = false; setBusy(false); } }
  }
  return <section aria-label="Founder next step" onPointerDownCapture={() => { interacted.current = true; }} onKeyDownCapture={() => { interacted.current = true; }}><NextStepView decision={current() ? decision : null} action={selected || decision?.hero} opened={opened} busy={busy} error={error}
    current={current} onOpen={() => { interacted.current = true; setOpened(true); }} onOutcome={outcome} onRetry={() => void load()} />
    {current() && decision && decision.alternatives.length > 0 && <details><summary>Other things you could do ({decision.alternatives.length})</summary>
      {decision.alternatives.map(action => <button key={action.action_id} type="button" disabled={busy} onClick={() => { interacted.current = true; setSelected(action); setOpened(false); }}>{action.title}</button>)}
    </details>}
  </section>;
}
