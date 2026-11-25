"use client";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { apiErrorCode, apiFailure } from "@/lib/apiFailure";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import type { ReviewBuild } from "@/lib/buildReview";
import { correctionAvailabilityMatches, type CorrectionAvailability } from "@/lib/privateCorrectionAvailability";
import { isLocalConsole } from "@/lib/localMode";

type Owner = { actor: string | null; epoch: number; build: string; app: string; artifact: string; report: string };
type Estimate = { quote_id: string; input_digest: string; ready: boolean; ceiling_micro?: number; reasons: string[]; mode?: string; next_step?: string };
type Request = { id: string; state: string; build_id: string; source_artifact_id: string; report_digest: string };

function knownPreflightRefusal(result: {status: number; data: unknown}): boolean {
  if (![400, 402, 409].includes(result.status)) return false;
  const data = result.data as {detail?: {code?: string}} | null;
  return ["build_budget_approval_mismatch", "build_quote_expired", "build_quote_inputs_changed",
    "build_credit_account_missing", "build_credit_account_suspended", "insufficient_credits",
    "private_correction_baseline_changed", "private_correction_plan_changed", "private_correction_venture_paused"].includes(data?.detail?.code || "");
}

export function PrivateCorrection({ build, onQueued }: { build: ReviewBuild; onQueued: () => void }) {
  const local = isLocalConsole();
  const ownerRef = useRef<Owner | null>(null), inFlight = useRef(false), mounted = useRef(true);
  const requestCustody = useRef<{owner: Owner; value: Request} | null>(null);
  const quoteCustody = useRef<{owner: Owner; value: Estimate} | null>(null);
  const dispatchCustody = useRef<{owner: Owner; quoteId: string} | null>(null);
  const approved = useRef<{ owner: Owner; quote: Estimate } | null>(null);
  const eligibilityCustody = useRef<{owner: Owner; value: CorrectionAvailability} | null>(null);
  const eligibilityFlight = useRef<Owner | null>(null);
  const owner: Owner = {actor: verifiedSessionSubject(), epoch: verifiedSessionEpoch(), build: build.id,
    app: build.app_id || "", artifact: build.artifact_id || "", report: build.review?.report_digest || ""};
  if (!ownerRef.current || JSON.stringify(ownerRef.current) !== JSON.stringify(owner)) {
    ownerRef.current = owner; dispatchCustody.current = null; approved.current = null; requestCustody.current = null; quoteCustody.current = null; eligibilityCustody.current = null;
  }
  const renderedOwner = ownerRef.current;
  const [instruction, setInstruction] = useState(""), [busy, setBusy] = useState(false);
  const [error, setError] = useState(""), [saved, setSaved] = useState<Request | null>(null);
  const [quote, setQuote] = useState<Estimate | null>(null), [confirmed, setConfirmed] = useState(false);
  const [queued, setQueued] = useState(false), [dispatchHeld, setDispatchHeld] = useState(false);
  const [eligibility, setEligibility] = useState<CorrectionAvailability | null>(null), [checking, setChecking] = useState(false);
  const current = () => !!renderedOwner.actor && mounted.current && ownerRef.current === renderedOwner &&
    renderedOwner.actor === verifiedSessionSubject() && renderedOwner.epoch === verifiedSessionEpoch();
  useEffect(() => {
    mounted.current = true;
    if (!ownerRef.current && renderedOwner.actor === verifiedSessionSubject() && renderedOwner.epoch === verifiedSessionEpoch()) ownerRef.current = renderedOwner;
    const retire = () => {ownerRef.current = null; dispatchCustody.current = null; setDispatchHeld(false); approved.current = null; requestCustody.current = null; quoteCustody.current = null; eligibilityCustody.current = null; setChecking(false); setEligibility(null); setSaved(null); setQuote(null); setConfirmed(false); setInstruction("");};
    window.addEventListener("supymem-session-changed", retire);
    return () => {mounted.current = false; ownerRef.current = null; approved.current = null; requestCustody.current = null; quoteCustody.current = null; eligibilityCustody.current = null; window.removeEventListener("supymem-session-changed", retire);};
  }, []);
  useEffect(() => {setSaved(null); setQuote(null); setConfirmed(false); setQueued(false); setDispatchHeld(false); setChecking(false); setEligibility(null); setError(""); setInstruction(""); void inspectEligibility();},
    [build.id, build.artifact_id, build.review?.report_digest, owner.actor, owner.epoch]);

  async function inspectEligibility() {
    if (!current() || inFlight.current || eligibilityFlight.current === renderedOwner) return;
    eligibilityFlight.current = renderedOwner; setChecking(true); setError(""); eligibilityCustody.current = null; setEligibility(null);
    try {
      const result = await api(`/api/builds/${encodeURIComponent(renderedOwner.build)}/corrections/availability`);
      if (!current()) return;
      if (!result.ok) throw new Error(apiFailure(result, "checking private correction availability"));
      const value = result.data as CorrectionAvailability;
      if (value?.build_id !== renderedOwner.build || typeof value.available !== "boolean" || typeof value.reason !== "string" ||
          (value.available && !correctionAvailabilityMatches(value, renderedOwner))) throw new Error("The correction availability does not match this saved build. Refresh its current evidence.");
      eligibilityCustody.current = {owner: renderedOwner, value}; setEligibility(value);
    } catch (failure) {if (current()) setError(failure instanceof Error ? failure.message : "Private correction availability is unconfirmed.");}
    finally {if (eligibilityFlight.current === renderedOwner) eligibilityFlight.current = null; if (current()) setChecking(false);}
  }

  async function saveCorrection() {
    if (!current() || dispatchCustody.current || inFlight.current || instruction.trim().length < 8 ||
        eligibilityCustody.current?.owner !== renderedOwner || !correctionAvailabilityMatches(eligibilityCustody.current.value, renderedOwner)) return;
    inFlight.current = true; setBusy(true); setError(""); approved.current = null; quoteCustody.current = null; setQuote(null); setConfirmed(false);
    try {
      const result = await api(`/api/builds/${encodeURIComponent(renderedOwner.build)}/corrections`, {method: "POST",
        body: JSON.stringify({source_artifact_id: renderedOwner.artifact, report_digest: renderedOwner.report, instruction: instruction.trim()})});
      if (!current()) return;
      if (!result.ok) {
        const code = apiErrorCode(result);
        if (result.status === 409 && code) {
          eligibilityCustody.current = null; setEligibility({build_id: renderedOwner.build, available: false, reason: code});
        }
        throw new Error(apiFailure(result, "saving the private correction"));
      }
      const request = result.data as Request;
      if (!request?.id || request.build_id !== renderedOwner.build || request.source_artifact_id !== renderedOwner.artifact || request.report_digest !== renderedOwner.report) throw new Error("The correction receipt does not match the reviewed build.");
      requestCustody.current = {owner: renderedOwner, value: request}; setSaved(request);
      inFlight.current = false;
      await estimateCorrection(request);
    } catch (failure) {if (current()) setError(failure instanceof Error ? failure.message : "The correction could not be confirmed. Review saved status before retrying.");}
    finally {inFlight.current = false; if (current()) setBusy(false);}
  }
  async function estimateCorrection(request = saved) {
    if (!current() || dispatchCustody.current || inFlight.current || !request || queued || requestCustody.current?.owner !== renderedOwner || requestCustody.current.value !== request) return;
    inFlight.current = true; setBusy(true); setError(""); approved.current = null; quoteCustody.current = null; setQuote(null); setConfirmed(false);
    try {
      const result = await api(`/api/builds/${encodeURIComponent(renderedOwner.build)}/corrections/estimate`, {method: "POST", body: JSON.stringify({revision_request_id: request.id})});
      if (!current()) return;
      if (!result.ok) throw new Error(apiFailure(result, "estimating the private correction"));
      const value = result.data as Estimate;
      if (!value?.quote_id || !value.input_digest || (value.mode !== "connected_tool" && (!Number.isSafeInteger(value.ceiling_micro) || value.ceiling_micro! < 0))) throw new Error("The correction estimate could not be verified.");
      quoteCustody.current = {owner: renderedOwner, value}; setQuote(value);
      if (value.ready) {
        approved.current = {owner: renderedOwner, quote: value};
        inFlight.current = false;
        await startCorrection(value);
      } else {
        setError(value.mode === "connected_tool" ? value.next_step || "Open Settings > Keys and connections > Coding tools, connect your tool, then retry your correction."
          : apiFailure({ok: false, status: value.reasons?.includes("insufficient_credits") ? 402 : 409,
            data: {detail: {code: value.reasons?.[0]}}}, "starting your private correction"));
      }
    } catch (failure) {if (current()) setError(failure instanceof Error ? failure.message : "The estimate could not be confirmed.");}
    finally {inFlight.current = false; if (current()) setBusy(false);}
  }
  async function startCorrection(quote = quoteCustody.current?.value) {
    const approval = approved.current;
    if (!current() || dispatchCustody.current || inFlight.current || !approval || approval.owner !== renderedOwner || approval.quote !== quote || !quote?.ready || !renderedOwner.app || quoteCustody.current?.owner !== renderedOwner || quoteCustody.current.value !== quote) return;
    dispatchCustody.current = {owner: renderedOwner, quoteId: quote.quote_id}; quoteCustody.current = null;
    approved.current = null; setDispatchHeld(true); setConfirmed(false); inFlight.current = true; setBusy(true); setError("");
    try {
      const result = await api(`/api/apps/${encodeURIComponent(renderedOwner.app)}/builds`, {method: "POST", headers: {"Idempotency-Key": `private-correction:${quote.quote_id}`}, body: JSON.stringify({
        quote_id: quote.quote_id, input_digest: quote.input_digest,
        ...(quote.mode === "connected_tool" ? {} : {approve_budget_micro: quote.ceiling_micro})})});
      if (!current()) return;
      if (!result.ok) {
        if (knownPreflightRefusal(result)) {dispatchCustody.current = null; setDispatchHeld(false); setQuote(null);}
        throw new Error(apiFailure(result, "queuing the reviewed correction"));
      }
      if (quote.mode === "connected_tool" && !(result.data as {build_run_id?: string})?.build_run_id) {
        throw new Error("Your correction receipt could not be verified. Open Builds and review its saved status before requesting another correction.");
      }
      setQueued(true); setQuote(null); setDispatchHeld(false); onQueued();
    } catch (failure) {if (current()) setError(failure instanceof Error ? failure.message : "The correction dispatch is unconfirmed. Review the Builds list before another attempt.");}
    finally {inFlight.current = false; if (current()) setBusy(false);}
  }
  function changeApproval(checked: boolean) {
    const allow = current() && !dispatchCustody.current && checked && !!quote?.ready && quoteCustody.current?.owner === renderedOwner && quoteCustody.current.value === quote;
    approved.current = allow && quote ? {owner: renderedOwner, quote} : null; setConfirmed(allow);
  }
  async function reviewSavedDispatch() {
    const attempt = dispatchCustody.current;
    if (!current() || inFlight.current || !attempt || attempt.owner !== renderedOwner) return;
    inFlight.current = true; setBusy(true); setError("");
    try {
      const result = await api(`/api/apps/${encodeURIComponent(renderedOwner.app)}/builds`);
      if (!current() || dispatchCustody.current !== attempt) return;
      if (!result.ok) throw new Error(apiFailure(result, "reviewing saved correction status"));
      const data = result.data as {items?: {id?: string; app_id?: string; quote_id?: string; correction_request_id?: string; state?: string}[]};
      const matches = Array.isArray(data?.items) ? data.items.filter(row => row.app_id === renderedOwner.app && row.quote_id === attempt.quoteId && row.correction_request_id === saved?.id) : [];
      if (matches.length !== 1 || !matches[0].id) {
        setError("No unique saved correction receipt confirms this attempt yet. It remains held; do not queue another correction."); return;
      }
      setQueued(true); setQuote(null); setDispatchHeld(false); onQueued();
    } catch (failure) {if (current()) setError(failure instanceof Error ? failure.message : "Saved status could not be confirmed. The dispatch remains held.");}
    finally {inFlight.current = false; if (current()) setBusy(false);}
  }
  return <section aria-label="Correct accepted private build">
    <h3>Request a private correction</h3>
    <p>{local ? "Your coding tool makes a separate correction from this exact accepted source. Your saved acceptance stays in history. Open Builds to check the result; nothing is published here."
      : "The saved acceptance stays in history. A correction is a separate build with its own estimate, QA and review. Nothing is published here."}</p>
    <ErrorNotice error={error} source="PrivateCorrection.error" />
    {checking && <p role="status">Checking whether this exact accepted source is still eligible for a private correction…</p>}
    {!checking && !correctionAvailabilityMatches(eligibility, renderedOwner) && <><p>{eligibility?.available === false
      ? apiFailure({ok: false, status: 409, data: {detail: {code: eligibility.reason}}}, "using this build’s private correction baseline")
      : "Private correction availability is unconfirmed. Check the current saved baseline before requesting changes."}</p>
      <button type="button" disabled={busy || checking} onClick={() => void inspectEligibility()}>Check correction availability</button></>}
    {correctionAvailabilityMatches(eligibility, renderedOwner) && <>
    <label>Requested changes<textarea maxLength={2000} value={instruction} disabled={busy || queued || dispatchHeld} onChange={event => {
      approved.current = null; requestCustody.current = null; quoteCustody.current = null; setConfirmed(false); setQuote(null); setSaved(null); setInstruction(event.target.value);
    }} /></label>
    <button type="button" disabled={busy || queued || dispatchHeld || !renderedOwner.actor || instruction.trim().length < 8} onClick={() => void saveCorrection()}>Request correction</button>
    {saved && !queued && <><p>Your correction request is saved. Check its status, or retry when you are ready.</p><button type="button" disabled={busy || dispatchHeld} onClick={() => void estimateCorrection()}>Retry correction</button></>}
    {quote && <><p>You can check the saved status of your correction.</p>
      {!quote.ready && <p>Your request stays saved. Retry your correction when you are ready.</p>}
      </>}
    {dispatchHeld && <><p role="status">Correction dispatch is unconfirmed. Approval cannot retry it.</p><button type="button" disabled={busy} onClick={() => void reviewSavedDispatch()}>Review saved correction status</button></>}
    {queued && <p role="status">A saved correction build is recorded. Review its current status, separate result and QA in Builds before accepting it.</p>}
    </>}
  </section>;
}
