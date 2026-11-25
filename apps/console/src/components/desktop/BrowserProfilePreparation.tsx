"use client";

import { routeInChat } from "@/lib/chatRouteAction";
import { ventureQueryId } from "@/lib/operationsScope";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { isLocalConsole } from "@/lib/localMode";
import { browserProfileCatalog, type BrowserProfileChoice } from "@/lib/browserProfileCatalog";
import { browserExecutionProfileDigest, browserReadOnlyRequested, browserReadOnlyAuthority } from "@/lib/browserExecutionProfile";
import { browserProfileWireAvailable, BROWSER_PROFILE_WIRE_MESSAGE } from "@/lib/browserProfileWireAvailability";
import { macTaskPrepareFailure } from "@/lib/macTaskPrepareFailure";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

const UNAVAILABLE = "Browser tasks are unavailable for this venture right now. Refresh to check again, or ask Charlie for manual steps.";

/** Explicit new-card preparation. It never approves, claims or starts a task. */
export function BrowserProfilePreparation({ventureId, disabled = false}: {ventureId: string; disabled?: boolean}) {
  const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
  const ownerRef = useRef({ventureId, subject, epoch});
  if (ownerRef.current.ventureId !== ventureId || ownerRef.current.subject !== subject || ownerRef.current.epoch !== epoch) {
    ownerRef.current = {ventureId, subject, epoch};
  }
  const owner = ownerRef.current;
  const disabledRef = useRef(disabled); disabledRef.current = disabled;
  const alive = useRef(true), serial = useRef(0), inFlight = useRef(false);
  const draft = useRef(""), choice = useRef<string | null>(null);
  const [instruction, setInstruction] = useState("");
  const [receipt, setReceipt] = useState<{owner: typeof owner; choice: BrowserProfileChoice} | null>(null);
  const [selected, setSelected] = useState(false), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const errOwner = useRef<typeof owner | null>(null);
  const listed = receipt?.owner === owner ? receipt.choice : null;
  const ready = Boolean(listed && current(owner));

  function current(candidate: typeof owner) {
    let routeVenture: string | null = null;
    try {
      const segment = typeof window !== "undefined" && /^\/v\/([^/]+)(?:\/|$)/.exec(window.location.pathname)?.[1];
      if (segment) routeVenture = decodeURIComponent(segment);
      else if (typeof window !== "undefined" && window.location.pathname === "/new") {
        const params = new URLSearchParams(window.location.search);
        if (params.getAll("venture").length === 1) routeVenture = ventureQueryId(params) ?? null;
      }
    } catch { /* Unknown route custody never grants preparation. */ }
    return alive.current && candidate === ownerRef.current && !!candidate.subject
      && routeVenture === candidate.ventureId
      && candidate.subject === verifiedSessionSubject() && candidate.epoch === verifiedSessionEpoch();
  }
  function clear() {
    serial.current++; inFlight.current = false; choice.current = null; draft.current = ""; errOwner.current = null;
    setReceipt(null); setSelected(false); setInstruction(""); setErr(""); setBusy(false);
  }
  async function load(captured = owner) {
    if (isLocalConsole()) return;
    if (inFlight.current) return;
    if (!current(captured)) { if (alive.current && captured === ownerRef.current) setLoading(false); return; }
    const generation = ++serial.current;
    choice.current = null; setSelected(false); setReceipt(null); setLoading(true); setErr("");
    if (!current(captured)) { setLoading(false); return; }
    try {
      const wire = await browserProfileWireAvailable();
      if (!current(captured) || generation !== serial.current) return;
      if (!wire) { showError(BROWSER_PROFILE_WIRE_MESSAGE, captured); return; }
      const result = await api(`/api/ventures/${encodeURIComponent(captured.ventureId)}/device-browser-ai-profile`);
      if (!current(captured) || generation !== serial.current) return;
      const parsed = await browserProfileCatalog(result);
      if (!current(captured) || generation !== serial.current) return;
      if (parsed) {
        const exactWire = await browserProfileWireAvailable(parsed.profile.version);
        if (!current(captured) || generation !== serial.current) return;
        if (!exactWire) { showError(BROWSER_PROFILE_WIRE_MESSAGE, captured); return; }
        setReceipt({owner: captured, choice: parsed});
      }
    } catch { /* No catalog result grants preparation or substitutes a model. */ }
    finally { if (current(captured) && generation === serial.current) setLoading(false); }
  }
  useEffect(() => {
    if (isLocalConsole()) return;
    alive.current = true; clear(); void load();
    const retired = () => {
      ownerRef.current = {ventureId: ownerRef.current.ventureId, subject: verifiedSessionSubject(), epoch: verifiedSessionEpoch()};
      clear(); void load(ownerRef.current);
    };
    window.addEventListener("supymem-session-changed", retired);
    return () => { alive.current = false; serial.current++; window.removeEventListener("supymem-session-changed", retired); };
    // Owner replacement immediately hides retained cards before this effect runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner]);

  function showError(message: string, captured = owner) {
    if (!current(captured)) return;
    errOwner.current = captured; setErr(message);
  }

  function choose(value: boolean) {
    if (!current(owner) || !listed || inFlight.current || disabledRef.current) return;
    choice.current = value ? listed.digest : null; setSelected(value);
  }
  function changeInstruction(value: string) {
    if (!current(owner) || inFlight.current || disabledRef.current) return;
    draft.current = value; setInstruction(value);
  }
  async function prepare() {
    const captured = owner, requested = instruction.trim(), digest = listed?.digest;
    if (!current(captured) || disabledRef.current || inFlight.current || !selected || !digest
        || choice.current !== digest || draft.current.trim() !== requested || !requested) return;
    inFlight.current = true; setBusy(true); setErr(""); const generation = ++serial.current;
    try {
      const refreshed = await api(`/api/ventures/${encodeURIComponent(captured.ventureId)}/device-browser-ai-profile`);
      if (!current(captured) || generation !== serial.current) return;
      const fresh = await browserProfileCatalog(refreshed);
      if (!current(captured) || generation !== serial.current) return;
      if (!fresh || fresh.digest !== digest) { choice.current = null; setSelected(false); setReceipt(null); showError("Browser task settings changed or are unavailable. Refresh and review them again; no replacement model is selected.", captured); return; }
      const readOnly = browserReadOnlyRequested(requested);
      const wire = await browserProfileWireAvailable(fresh.profile.version, readOnly);
      if (!current(captured) || generation !== serial.current) return;
      if (!wire) { choice.current = null; setSelected(false); setReceipt(null); showError(BROWSER_PROFILE_WIRE_MESSAGE, captured); return; }
      if (disabledRef.current || choice.current !== digest || draft.current.trim() !== requested) return;
      const result = await api(`/api/ventures/${encodeURIComponent(captured.ventureId)}/device-work-orders/from-chat`, {
        method: "POST", body: JSON.stringify({instruction: requested, browser_execution_profile_digest: digest}),
      });
      if (!current(captured) || generation !== serial.current) return;
      if (!result.ok) { showError(macTaskPrepareFailure(result), captured); return; }
      const saved = result.data as {id?: unknown; venture_id?: unknown; review_digest?: unknown; review_path?: unknown;
        manifest?: {actions?: unknown; general_browser_verification?: {execution_profile?: unknown}}} | null;
      const path = saved?.review_path;
      const prefix = `/v/${encodeURIComponent(captured.ventureId)}/device-work-orders/`;
      if (typeof path !== "string" || !path.startsWith(prefix) || !/^dwo_[A-Za-z0-9_-]+$/.test(path.slice(prefix.length))
          || saved?.id !== path.slice(prefix.length) || saved?.venture_id !== captured.ventureId
          || typeof saved?.review_digest !== "string" || !/^[a-f0-9]{64}$/.test(saved.review_digest)) {
        showError("The preparation response did not confirm a saved review link. Refresh saved Mac tasks before preparing another.", captured); return;
      }
      const savedDigest = await browserExecutionProfileDigest(saved.manifest?.general_browser_verification?.execution_profile);
      if (!current(captured) || generation !== serial.current) return;
      if (savedDigest !== digest) { showError("The preparation response did not confirm the selected browser profile. Check saved Mac tasks before preparing another; no other model is selected.", captured); return; }
      if (readOnly && !browserReadOnlyAuthority(saved.manifest)) {
        showError("The saved request did not confirm read-only browser access. Nothing was approved or started; check saved reviews before another request.", captured); return;
      }
      if (!routeInChat(path)) window.location.assign(path);
    } catch { if (current(captured) && generation === serial.current) showError("The preparation result is unconfirmed. Check saved Mac tasks before trying again.", captured); }
    finally { if (current(captured) && generation === serial.current) { inFlight.current = false; setBusy(false); } }
  }

  if (isLocalConsole()) return null;
  return <section aria-label="Browser tasks" style={{marginTop: 16}}>
    <h3>Browser tasks on your Mac</h3>
    <p>Let Supymem use a browser on your Mac for short tasks, like checking a competitor&apos;s pricing page. You review every task before it runs.</p>
    <p>Include the exact HTTPS websites. Say “Read only” to prohibit input, submissions, downloads and new tabs. Do not paste passwords or API keys.</p>
    {ready && listed ? <>
      <label><input type="checkbox" checked={selected && choice.current === listed.digest} disabled={busy || disabled} onChange={event => choose(event.target.checked)} />You allow only this reviewed browser task, and its page text is sent for processing. Review its website permissions before running it.</label>
      <div>{[
        "Visit https://stripe.com/pricing and read only: summarize the published pricing.",
        "Go to https://linear.app/features and read only: list the main features.",
        "Go to https://resend.com/signup and prepare the non-secret signup fields without submitting.",
      ].map(goal => <button key={goal} type="button" disabled={busy || disabled} onClick={() => changeInstruction(goal)}>{goal}</button>)}</div>
      <label style={{display: "block", marginTop: 12}}>Browser goal and exact HTTPS websites
        <textarea value={instruction} maxLength={12000} disabled={busy || disabled} onChange={event => changeInstruction(event.target.value)} style={{display: "block", width: "100%", minHeight: 80}} />
      </label>
      <p>Review the exact websites, permitted actions and time limit, then run your task.</p>
      <button type="button" disabled={busy || disabled || !selected || choice.current !== listed.digest || !instruction.trim()} onClick={() => void prepare()}>{busy ? "Preparing review…" : "Prepare browser task for review"}</button>
    </> : <p role="status">{loading ? "Checking browser task availability…" : UNAVAILABLE}</p>}
    <ErrorNotice error={current(owner) && errOwner.current === owner ? err : ""} source="browser-profile-preparation" />
    <button type="button" disabled={busy || disabled || loading || !current(owner)} onClick={() => void load()}>Check availability again</button>
  </section>;
}
