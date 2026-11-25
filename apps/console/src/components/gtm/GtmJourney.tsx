"use client";

import Link from "@/components/chat/ChatLink";
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { OneCard } from "@/components/primitives/OneCard";
import { KnownAnswer } from "@/components/chat/KnownAnswer";
import { RUN_STATUS_REFRESH } from "@/lib/runStatusPresentation";
import { ResearchProgress } from "@/components/primitives/ResearchProgress";
import { SimulatedReaderCheck } from "@/components/simulation/SimulatedReaderCheck";
import { ErrorNotice, useReportedError } from "@/components/primitives/ErrorNotice";
import { gtmRequestFence, type GtmOwner } from "@/lib/gtmDiagnosisIntent";
import { journeyClient, journeyInitialStage, journeyReviewRequested, journeySendReason, safeEvidenceUrl, type ContactAction, type GtmJourney as Journey, type JourneyDraft, type JourneyGeoOption, type JourneyProspect, type ProspectConfirmation, type ReplyOutcome } from "@/lib/gtmJourneyApi";
import styles from "./GtmJourney.module.css";
import { ImportContacts } from "./ImportContacts";
import { contactsImportRequested, founderAssetsClient, type ContactImport } from "@/lib/founderAssetsApi";
import { plainText } from "@/lib/researchBrief";
import { reachAction, guessedEmailWarning } from "@/lib/reachableLeads";
import type { ReachableLead } from "@/lib/gtmJourneyApi";
import { gtmProgressOwner, shareGtmProgress, subscribeGtmProgress } from "@/lib/gtmProgress";
import { verifiedSessionEpoch } from "@/lib/client";
import { editableIdeaAnswer } from "@/lib/ideaAnswer";
import { observeFounder } from "@/lib/observability";
import { CopyDraft, Draft } from "./OutreachDraft";
import { NextStepCard } from "@/components/planning/NextStepCard";
import { CallBriefView, CallNotesForm } from "./CallNotesForm";
import { isLocalConsole } from "@/lib/localMode";

function EvidenceLink({ url, children }: { url: string; children: React.ReactNode }) {
  const safe = safeEvidenceUrl(url);
  return safe ? <a href={safe} target="_blank" rel="noopener noreferrer">{children}</a> : null;
}

const contactNote = "Only contact people about their own experience; include your address and an opt-out line in emails.";
const possibleFitNote = <><h4>Possible fit, check before reaching out</h4><p>These public passages are a weaker match to your problem. Ask whether it applies before reaching out.</p></>;

function knownAnswer(text: string): string {
  const answer = editableIdeaAnswer(text);
  return /^(?:(?:i|we)(?:['’]m|['’]re| am| are)?\s+)?(?:not (?:really )?sure|unsure|(?:i\s+)?don['’]?t know|do not know|unknown|no idea|idk|n\/a|skip|pass|move on|-)[.!?]?$/i.test(answer) ? "" : answer;
}

function ManualProgress({ disabled, current, mark, outcome }: { disabled: boolean; current: () => boolean; mark: (action: "sent" | "call-booked") => void; outcome?: NonNullable<Journey["manual_outreach"]>[number] }) {
  return <div className={styles.row}><button className={styles.button} disabled={disabled || !!outcome?.sent_at} onClick={() => { if (current()) mark("sent"); }}>{outcome?.sent_at ? "Marked as sent ✓" : "Mark as sent"}</button><button className={styles.button} disabled={disabled || !!outcome?.booked_at} onClick={() => { if (current()) mark("call-booked"); }}>{outcome?.booked_at ? "Call booked ✓" : "Call booked"}</button></div>;
}

function ReachCard({ lead, disabled, current, mark, outcome }: { lead: ReachableLead; disabled: boolean; current: () => boolean; mark?: (action: "sent" | "call-booked") => void; outcome?: NonNullable<Journey["manual_outreach"]>[number] }) {
  const [status, setStatus] = useState("");
  const [reviewedEmail, setReviewedEmail] = useState("");
  return <li className={styles.prospect}>
    <strong>{lead.person || lead.name}</strong>{lead.handle && <span> {lead.handle}</span>}{lead.company && lead.company.trim().toLowerCase() !== (lead.person || lead.name).trim().toLowerCase() && <span> · {lead.company}</span>}
    {lead.lead_type === "insight" && <small> · Industry insight contact · ask about their experience</small>}
    {lead.judging_status === "pending" && <p role="status">Relevance not yet checked. Review the linked work before asking for a call; do not assume product interest.</p>}
    <p>{lead.person_role || lead.role}{lead.location && ` · ${lead.location}`}</p>
    {lead.evidence_label === "fit_only" ? <p>Why: {lead.why} <EvidenceLink url={lead.source_url}>Public source</EvidenceLink></p> :
      <p>{lead.evidence_label === "possible_fit" ? "Possible fit. Ask whether this problem applies to their experience." : <>{lead.value_exchange && "Potential value: "}{lead.value_exchange || lead.why}</>} <EvidenceLink url={lead.source_url}>Need/workflow evidence</EvidenceLink></p>}
    {(lead.display_quote ?? lead.quote) && <blockquote>{plainText(lead.display_quote ?? lead.quote)}</blockquote>}
    {lead.signals?.filter(signal => signal.confidence === "inferred").map((signal, index) => <p key={index}>Hiring observation (inferred; date not confirmed): {plainText(signal.quote)} <EvidenceLink url={signal.source_url}>Published careers page</EvidenceLink></p>)}
    {lead.draft_source === "template" && <small>Template draft · {lead.judging_status === "pending" ? "personalisation waits for relevance review" : lead.draft_status === "credits_required" ? "personalisation needs credits" : lead.draft_status === "outcome_uncertain" ? "personalisation could not be confirmed; no automatic retry" : lead.draft_status === "output_rejected" ? "the personalised draft did not pass review" : ["fit_only", "possible_fit"].includes(lead.evidence_label || "") && !lead.draft_status ? "asks whether the problem applies to them" : "personalisation could not be prepared"}. Review or edit before using.</small>}
    {lead.outreach_hint?.subscriber === "unverified_individual" && lead.outreach_hint.note && <p className={styles.muted}>{lead.outreach_hint.note}</p>}
    {lead.outreach_hint?.source_url && <EvidenceLink url={lead.outreach_hint.source_url}>Registry verification</EvidenceLink>}
    {lead.registry?.attribution && <small>{lead.registry.attribution} <EvidenceLink url={lead.registry.registry_url || lead.source_url}>Registry source</EvidenceLink></small>}
    <p className={styles.draftBody}>{lead.draft}</p>
    <div className={styles.row}>{lead.routes.map((route, index) => {
      const action = reachAction(lead, route);
      const warning = guessedEmailWarning(route);
      return <span key={`${route.kind}:${index}`}>
        {!route.email && route.kind !== "phone" && <small>{route.kind === "booking" ? "Published booking page" : route.company_contact ? "Company contact page" : route.kind === "contact_form" ? "Published contact page" : route.kind === "website" ? "Their own website, no contact details published" : "Public community profile or post"} <EvidenceLink url={route.source_url}>Contact source</EvidenceLink></small>}
        {route.email && <><small>{route.email} · {route.confidence_label || (route.email_kind === "named_person" ? "Named person" : route.email_kind === "business_role" ? "Business role address" : "Published business address (person not identified)")}{route.confidence !== undefined && ` · ${route.confidence}% confidence`} <EvidenceLink url={route.source_url}>Source</EvidenceLink></small><blockquote>{plainText(route.display_quote || route.quote)}</blockquote></>}
        {warning && <label><input type="checkbox" checked={reviewedEmail === route.email} onChange={event => setReviewedEmail(event.target.checked ? route.email! : "")} />{warning} I understand; I will review before sending.</label>}
        {route.kind === "phone" && <><small>Phone: {route.value} <EvidenceLink url={route.source_url}>Source</EvidenceLink></small><blockquote>{plainText(route.display_quote || route.quote)}</blockquote></>}
        {action && <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled || !!warning && reviewedEmail !== route.email} data-usage-control={`gtm_reach_${route.kind}`} onClick={() => {
          if (disabled || !current() || warning && reviewedEmail !== route.email) return;
          window.open(action.url, "_blank", "noopener,noreferrer");
          void navigator.clipboard.writeText(route.kind === "email" ? route.email! : action.text)
            .then(() => { if (current()) setStatus(route.kind === "email" ? "Email copied; your mail app opens with the draft. Nothing sent." : "Draft copied. Review before sending; nothing sent."); })
            .catch(() => { if (current()) setStatus("Opened the contact route, but copying failed. Select the draft text to copy it."); });
        }}>{action.label}</button>}
      </span>;
    })}<button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled} data-usage-control="gtm_reach_copy" onClick={() => {
      if (disabled || !current()) return;
      void navigator.clipboard.writeText(lead.draft).then(() => { if (current()) setStatus("Draft copied; nothing sent."); }).catch(() => { if (current()) setStatus("Copy failed; select the draft text."); });
    }}>Copy draft</button></div>{status && <p role="status">{status}</p>}
    {lead.linkedin_search && <p><EvidenceLink url={lead.linkedin_search}>Find on LinkedIn</EvidenceLink></p>}
    {!!lead.people?.length && <ul>{lead.people.map(person => <li key={person.name}>{person.name} · {person.role}{person.company && ` · ${person.company}`}{person.email && ` · ${person.email}`} <EvidenceLink url={person.source_url}>Source</EvidenceLink> · <EvidenceLink url={person.linkedin_search}>Find on LinkedIn</EvidenceLink></li>)}</ul>}
    {lead.playbook_messages?.map(message => <details key={message.playbook}><summary>{message.title}: LinkedIn note and DM</summary>
      <CopyDraft title="LinkedIn connection note" buttonLabel="Copy note" text={message.connection_note} disabled={disabled} current={current} />
      <CopyDraft title="LinkedIn follow-up DM" buttonLabel="Copy DM" text={message.linkedin_dm} disabled={disabled} current={current} /></details>)}
    {mark && <ManualProgress disabled={disabled} current={current} mark={mark} outcome={outcome} />}
  </li>;
}

const active = (row: Journey) => ["queued", "running"].includes(row.state);
const preparationFailed = (row: Journey) => row.state === "failed" || (!!row.error_code && !row.prospects.length && !row.reachable_leads?.length && !row.industry_experts?.length);
const issue = (err: unknown) => err instanceof Error ? err.message : "Saved work could not be verified. Refresh and retry.";
const labels = ["Audience", "Prospects", "Review", "Send", "Results"];
const permissionBlockers = new Set(["gtm_recipient_consent_required", "unknown_jurisdiction", "casl_consent_required", "casl_mined_directory_not_implied_consent", "eprivacy_opt_in_required", "dpdp_public_carveout_not_licence", "dpdp_consent_or_legitimate_use_required"]);
const prospectBlockCopy: Record<string, string> = {
  gtm_recipient_consent_required: "Record how this recipient agreed to be contacted in Review permission to contact. Cold outreach to UK sole traders, ordinary partnerships and consumers is blocked without consent.",
  unknown_jurisdiction: "The recipient country is missing or unsupported. Confirm the actual country and permission; unsupported countries remain blocked.",
  casl_consent_required: "This contact needs documented consent before outreach. Record their actual permission below.",
  casl_mined_directory_not_implied_consent: "A public directory entry does not establish consent. Record this contact’s actual consent before outreach.",
  eprivacy_opt_in_required: "This contact needs explicit opt-in before outreach. Record their actual consent below.",
  dpdp_public_carveout_not_licence: "A public address does not establish permission to email. Record actual consent or the applicable legitimate use below.",
  dpdp_consent_or_legitimate_use_required: "This contact needs documented consent or an applicable legitimate use. Record the actual basis below.",
  us_postal_address_required: "US outreach needs your business mailing address in the email footer. Save your sending address to prepare eligible drafts.",
  published_email_missing: "No email address was found at the saved source. Use Add a contact with evidence only when you have an actual address and source.",
  email_evidence_required: "This email address is guessed or lacks published evidence. Verify the actual address at its source before adding a contact with evidence.",
  source_evidence_required: "This contact lacks a valid public source. Verify the source before adding a contact with that evidence.",
  send_paused: "Sending is paused for this workspace. Keep the contact saved; the sending pause must be cleared before outreach can continue.",
  skipped: "You skipped this prospect. It will not get a draft.",
  suppressed_or_replied: "This contact is suppressed, has bounced or has already replied. Do not send further outreach; review the existing conversation in Gmail.",
  opted_out: "This contact opted out. Do not send further outreach.",
  art_21_objected: "This contact objected to outreach. Do not send further outreach.",
  aup_required: "Review and accept your workspace’s acceptable use terms before preparing outreach.",
  dpa_required: "Review and accept your workspace’s data processing agreement before preparing outreach.",
};

export function GtmJourney({ owner }: { owner: GtmOwner }) {
  const key = JSON.stringify([owner.actorId, owner.workspaceId, owner.ventureId]);
  const fence = useRef(gtmRequestFence());
  const inFlight = useRef(false);
  const loaded = useRef(false);
  const recovery = useRef("");
  const reads = useRef(0);
  const refresh = useRef<() => Promise<void>>(async () => {});
  const container = useRef<HTMLDivElement>(null);
  const addressDetails = useRef<HTMLDetailsElement>(null);
  const contactDetails = useRef<HTMLDetailsElement>(null);
  const [value, setValue] = useState<Journey | null>(null);
  const latest = useRef<Journey | null>(null);
  const [busy, setBusy] = useState(false);
  const [bounceReviewed, setBounceReviewed] = useState(false);
  const [error, setError] = useState("");
  const [audience, setAudience] = useState<string | null>(null);
  const [audienceAnswered, setAudienceAnswered] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [searches, setSearches] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [stage, setStage] = useState(0);
  const [sendUncertain, setSendUncertain] = useState(false);
  const [postal, setPostal] = useState("");
  const [policyAccepted, setPolicyAccepted] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [guessesReviewed, setGuessesReviewed] = useState("");
  const [discoveryNote, setDiscoveryNote] = useState("");
  const [resolvedFields, setResolvedFields] = useState<string[]>([]);
  useReportedError(error, "gtm.journey");
  const local = isLocalConsole();
  const connections = local ? "/settings/local" : `/connections?workspace=${encodeURIComponent(owner.workspaceId)}&venture=${encodeURIComponent(owner.ventureId)}`;
  const ownsPage = () => {
    if (!window.location) return true;
    const url = new URL(window.location.href);
    return url.pathname.replace(/\/$/, "") === "/gtm"
      && (!url.searchParams.get("venture") || url.searchParams.get("venture") === owner.ventureId)
      && (!url.searchParams.get("workspace") || url.searchParams.get("workspace") === owner.workspaceId);
  };
  const requestsImport = () => {
    if (!window.location || !ownsPage()) return false;
    const url = new URL(window.location.href);
    return url.searchParams.get("venture") === owner.ventureId && contactsImportRequested(url.search);
  };

  useEffect(() => {
    const heading = stage === 4 ? container.current?.querySelector<HTMLHeadingElement>("#gtm-journey-results h2") : null;
    (heading || container.current)?.scrollIntoView({ block: heading ? "center" : "start", behavior: "auto" });
    if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
  }, [stage, value?.venture_id]);

  const adopt = useCallback((next: Journey) => {
    if (next.research_progress?.run_id) next = { ...next, research_progress: shareGtmProgress(gtmProgressOwner(owner.actorId, verifiedSessionEpoch(), owner.ventureId), next.research_progress) };
    latest.current = next;
    setValue(next);
    if (next.manual_outreach?.some(item => item.booked_at)) window.dispatchEvent(new CustomEvent(RUN_STATUS_REFRESH, { detail: { ventureId: next.venture_id } }));
    const failed = preparationFailed(next);
    const failure = failed ? JSON.stringify([next.job_id, next.error_code, next.state]) : "";
    const search = window.location ? new URL(window.location.href).search : "";
    const reviewRequested = journeyReviewRequested(next.venture_id, search);
    if (!loaded.current && reviewRequested) {
      setStage(2);
      loaded.current = true;
    } else if (failed && failure !== recovery.current) {
      setStage(1);
      loaded.current = true;
    } else if (!loaded.current) {
      setStage(requestsImport() ? 1 : journeyInitialStage(next, search));
      loaded.current = true;
    }
    recovery.current = failure;
  }, [key]);

  useEffect(() => subscribeGtmProgress(gtmProgressOwner(owner.actorId, verifiedSessionEpoch(), owner.ventureId), progress => {
    const saved = latest.current;
    if (!saved || saved.research_progress?.run_id !== progress.run_id) return;
    latest.current = { ...saved, research_progress: progress };
    setValue(latest.current);
  }), [key]);
  const run = useCallback(async (fn: () => Promise<Journey>, saved?: () => void) => {
    const current = fence.current.capture();
    if (!current() || !ownsPage() || inFlight.current) return;
    inFlight.current = true; reads.current += 1; setBusy(true); setError("");
    try { const next = await fn(); if (current() && ownsPage()) { adopt(next); saved?.(); return true; } }
    catch (err) { if (current()) setError(issue(err)); return false; }
    finally { if (current()) { inFlight.current = false; setBusy(false); } }
  }, [key, adopt]);

  useEffect(() => {
    fence.current.activate(key);
    loaded.current = false; recovery.current = "";
    setAudience(null); setAudienceAnswered(false); setProblem(null); setSearches(null); setDiscoveryNote(""); setBounceReviewed(false); setGuessesReviewed(""); setResolvedFields([]);
    const current = fence.current.capture();
    const retire = () => { fence.current.invalidate(); latest.current = null; setValue(null); setBusy(false); inFlight.current = false; };
    window.addEventListener("supymem-session-changed", retire);
    async function load() {
      if (!current() || !ownsPage() || inFlight.current) return;
      const ticket = ++reads.current;
      try {
        if (window.location) {
          const url = new URL(window.location.href);
          if (url.searchParams.get("skip_page") === "1" && url.searchParams.get("venture") === owner.ventureId) {
            if (!current() || !ownsPage()) return;
            window.dispatchEvent(new CustomEvent("supymem:ask", { detail: { action: "journey.skip_step",
              payload: { step: "landing_page", why: "I followed the Find customers first path." } } }));
            const latestUrl = new URL(window.location.href);
            if (latestUrl.href === url.href) {
              latestUrl.searchParams.delete("skip_page");
              window.history.replaceState(window.history.state, "", latestUrl);
            }
          }
        }
        const next = await journeyClient.get(owner);
        if (!current() || !ownsPage()) return;
        if (ticket !== reads.current) return;
        adopt(next);
        setError("");
      } catch (err) { if (current()) setError(issue(err)); }
    }
    refresh.current = load;
    const focused = () => { void load(); };
    window.addEventListener("focus", focused);
    void load();
    return () => { retire(); window.removeEventListener("focus", focused); window.removeEventListener("supymem-session-changed", retire); };
  }, [key, adopt]);

  useEffect(() => {
    if (!value || !active(value)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh.current();
      if (!cancelled && latest.current && active(latest.current)) timer = setTimeout(poll, 5000);
    };
    timer = setTimeout(poll, 5000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [key, value?.state]);

  if (!value) return <OneCard title="Preparing your go-to-market workspace" status={error ? "error" : "working"} body={error || "Loading the audience, prospects and drafts saved for this venture."}
    actions={error && <button className={styles.button} onClick={() => void refresh.current()}>Retry loading</button>} />;

  const working = active(value);
  const drafts = value.drafts.filter(d => d.review_state !== "skipped");
  const batch = value.batches.find(b => b.approved_count > 0);
  const approved = batch ? value.drafts.filter(d => d.batch_id === batch.batch_id && d.review_state === "approved" && d.state === "draft" && d.channel !== "manual") : [];
  const cap = batch?.cap ?? Math.min(5, value.daily_cap);
  const followUps = drafts.filter(d => (d.day === 3 || d.day === 6) && d.channel !== "manual" && d.state === "draft" && d.review_state === "draft").length;
  const needsSupymem = value.send_blocked_reason === "outbound_execution_disabled" || !!value.send_blocked_reason?.startsWith("outbound_rate_");
  const settingsHref = local ? "/settings/local" : `/v/${encodeURIComponent(owner.ventureId)}/settings`;
  const sentContacts = value.prospects.filter(p => value.drafts.some(d => d.contact_id === p.id && d.state === "sent"));
  const preparationIssue = preparationFailed(value);
  const searchBusy = preparationIssue && value.error_code === "public_search_unavailable";
  const searchBlocked = preparationIssue && value.error_code === "public_search_blocked";
  const noCandidates = preparationIssue && value.error_code === "no_public_candidates";
  const renderedRequest = fence.current.capture();
  const currentDraft = () => renderedRequest() && ownsPage();
  const audienceValue = audience ?? knownAnswer(value.icp.who);
  const problemValue = problem ?? knownAnswer(value.icp.problem ?? "");
  const selectedAudience = knownAnswer(audienceValue);
  const selectedProblem = knownAnswer(problemValue);
  const askAudience = !audienceAnswered && (audience !== null || selectedAudience.length < 8);
  const audienceCheck = value.icp.known_checks?.audience;
  const problemCheck = value.icp.known_checks?.problem;
  const reviewAudience = audience === null && !resolvedFields.includes("audience") && (audienceCheck?.status === "confirm" || audienceCheck?.status === "conflict");
  const reviewProblem = problem === null && !resolvedFields.includes("problem") && (problemCheck?.status === "confirm" || problemCheck?.status === "conflict");
  const startIssue = reviewAudience || reviewProblem ? "Confirm or edit your saved context before continuing." : selectedAudience.length < 8 || selectedAudience.length > 600 ? "Describe who you help in 8-600 characters."
    : !askAudience && !selectedProblem ? "Tell us what problem you solve." : "";
  const audienceField = <>
    {audienceCheck && audienceCheck.status !== "missing" && audience === null && !resolvedFields.includes("audience")
      ? <KnownAnswer check={audienceCheck} field="audience" disabled={busy}
          onSelect={answer => { setAudience(answer); setAudienceAnswered(true); setResolvedFields([...resolvedFields, "audience"]); }}
          onEdit={() => { setAudience(audienceValue); setAudienceAnswered(false); setResolvedFields([...resolvedFields, "audience"]); }} /> : <>
    {!askAudience && <p><strong>{selectedAudience}</strong> <button className={`${styles.button} ${styles.secondary}`} disabled={busy} onClick={() => { setAudience(audienceValue); setAudienceAnswered(false); }}>Edit audience</button></p>}
    {(askAudience || value.discovery_started === false && !audienceAnswered) && <div className={styles.label}><label htmlFor={`gtm-audience-${owner.ventureId}`}>Who should we target?</label><textarea id={`gtm-audience-${owner.ventureId}`} aria-label={askAudience ? "Who should we target?" : undefined} className={styles.input} minLength={8} maxLength={600} value={audienceValue} placeholder={knownAnswer(value.icp.suggested_who || "") || "Tell us who your product helps."} disabled={busy} onChange={event => setAudience(event.target.value)} /></div>}
    {value.icp.fact_confirmations?.audience && <p className={styles.muted}>{value.icp.fact_confirmations.audience}</p>}
    </>}
  </>;
  const problemField = <>
    {problemCheck && problemCheck.status !== "missing" && problem === null && !resolvedFields.includes("problem")
      ? <KnownAnswer check={problemCheck} field="problem" disabled={busy}
          onSelect={answer => { setProblem(answer); setResolvedFields([...resolvedFields, "problem"]); }}
          onEdit={() => { setAudienceAnswered(true); setProblem(problemValue); setResolvedFields([...resolvedFields, "problem"]); }} /> : <>
    {!askAudience && (problem !== null || !selectedProblem) ? <label className={styles.label}>What problem do you solve?<input aria-label="What problem do you solve?" aria-describedby={startIssue ? "gtm-start-input-needed" : undefined} data-usage-control="gtm_journey_problem" className={styles.input} maxLength={2000} required value={problemValue} disabled={busy} onChange={event => setProblem(event.target.value)} /></label>
      : selectedProblem && <p>{selectedProblem} <button className={`${styles.button} ${styles.secondary}`} disabled={busy} onClick={() => { setAudienceAnswered(true); setProblem(problemValue); }}>Edit problem</button></p>}
    {value.icp.fact_confirmations?.problem && <p className={styles.muted}>{value.icp.fact_confirmations.problem}</p>}
    {problem === null && selectedProblem && value.icp.problem_basis?.startsWith("From ") && <p className={styles.muted}>{value.icp.problem_basis}</p>}
    </>}
    {startIssue && <p id="gtm-start-input-needed" role="status">{startIssue}</p>}
  </>;
  const step = Math.min(stage, 4);
  const stepLabels = value.icp.mode === "consumer" ? ["Audience", "Communities", "Review", "Send", "Results"] : labels;
  const hasManualLeads = !!(value.channels?.length || value.need_signals?.length || value.reachable_leads?.length || value.industry_experts?.length);
  const hasReachableLeads = !!(value.reachable_leads?.length || value.industry_experts?.length);
  const manualReview = value.icp.mode === "consumer" || !drafts.length && hasManualLeads;
  const experts = [...(value.industry_experts || []), ...(value.reachable_leads || []).filter(lead => lead.expert_role || lead.lead_type === "insight")];
  const people = (value.reachable_leads || []).filter(lead => !experts.includes(lead));
  const manualOutreach = value.manual_outreach || [];
  const bookedCount = manualOutreach.filter(item => item.booked_at).length;
  const showDiscoveryNotices = step === 1 && bookedCount === 0;
  const mark = (id: string, action: "sent" | "call-booked") => void run(() => journeyClient.contact(owner, id, action), () => setStage(4));
  const manualOutcome = (url: string) => manualOutreach.find(item => item.source_url === url);
  const reachList = (leads: ReachableLead[], recording = false) => <><p className={styles.muted}>{contactNote}</p>{[false, true].map(possible => {
    const group = leads.filter(lead => (lead.evidence_label === "possible_fit") === possible);
    return !!group.length && <section key={String(possible)}>{possible && possibleFitNote}<ul className={styles.prospects}>{group.map(lead => <ReachCard key={`${key}:${lead.id}`} lead={lead} disabled={busy} current={currentDraft} mark={recording ? action => mark(lead.id, action) : undefined} outcome={manualOutreach.find(item => item.id === lead.id)} />)}</ul></section>;
  })}</>;
  const communities = <>
    <h3>Where your customers gather</h3>
    {showDiscoveryNotices && value.people_search_notice && <p role="status">{value.people_search_notice}</p>}
    {!(value.channels || []).length && <p>No verified gathering places yet. Ask a current customer where they gather, or refine your audience and location.</p>}
    {value.source_access === "unavailable" && <p role="status">Sources could not be inspected. Check your search integration or use a public community you already know.</p>}
    <ul className={styles.prospects}>{(value.channels || []).map(channel => <li className={styles.prospect} key={channel.url}>
      <h3>{channel.name}</h3><EvidenceLink url={channel.url}>View community source</EvidenceLink>
      <p>{channel.channel_role === "partner/channel to approach" && "Partner/channel to approach · "}{channel.why_fits}</p>
      <p>Posting rules: <EvidenceLink url={channel.rules_url || channel.url}>Read before posting</EvidenceLink></p>
      {channel.contact_url && <EvidenceLink url={channel.contact_url}>Published organisation contact page</EvidenceLink>}
      <CopyDraft title={channel.channel_role === "partner/channel to approach" ? "Introduction draft" : "Conversation invitation draft"} text={channel.draft} disabled={busy} current={currentDraft} />
    </li>)}</ul>
    <h3>People describing this problem</h3>
    {!(value.need_signals || []).length && <p>{value.error_code === "public_search_unavailable" || value.source_access === "unavailable"
      ? "Search is temporarily unavailable, so we couldn't look for people this time. Nothing was lost. Try again in a few minutes."
      : "We didn't find people publicly describing this problem yet. Ask in the communities above, or retry later."}</p>}
    {[false, true].map(possible => {
      const group = (value.need_signals || []).filter(signal => (signal.evidence_label === "possible_fit") === possible);
      return !!group.length && <section key={String(possible)}>{possible && possibleFitNote}{group.map(signal => <article key={`${signal.source_url}:${signal.quote}`}><blockquote>{plainText(signal.quote)}</blockquote><p>{signal.author && `${/reddit\.com\//.test(signal.source_url) ? "u/" : ""}${signal.author} · `}{signal.published_at?.slice(0, 10) || "Date unknown"}{signal.date_status === "approximate" ? " (approximate)" : ""} · {signal.recency === "older" ? "Older · " : ""}{possible ? "Possible fit" : signal.confidence === "inferred" ? "Inferred" : "Observed"} signal <EvidenceLink url={signal.source_url}>View need source, read rules before replying publicly</EvidenceLink></p><CopyDraft title="Reply draft, founder sends it" buttonLabel="Draft a reply" text={signal.recency === "recent" ? signal.draft || (possible ? "Hi, does the problem I'm researching apply to your experience? If so, open to a 15-minute research call? Reply with a time. No pressure; please don't share private records or children's details." : "Hi, I saw your post and would value your perspective. What have you tried, and what has been hardest? If the rules allow, are you open to a 15-minute research call? Reply with a time. No pressure; please don't share private records or children's details.") : "Reply drafts require a verified post within the last 90 days. Use the community rules and current discussions instead."} disabled={busy || signal.recency !== "recent"} current={currentDraft} /></article>)}</section>;
    })}
  </>;
  const open = value.prospects.filter(p => !p.skipped);
  const guessedBatch = approved.filter(draft => guessedEmailWarning(draft.email_route ?? undefined)).map(draft => `${draft.doc_id}:${draft.revision}:${draft.to}:${draft.email_route?.confidence}`).join("|");
  const waiting = open.filter(p => p.blocked_reason && permissionBlockers.has(p.blocked_reason));
  const needsCountries = !preparationIssue && !drafts.length && waiting.length > 0;
  const preparationMessage = searchBusy ? "Public search is busy. Retry search in a moment."
    : searchBlocked ? "Public search is unavailable from this workspace. Add a contact you know."
    : noCandidates ? "No public company pages matched this audience. Refine the audience or add a contact you know."
    : value.state === "failed" ? "Preparation could not finish. Your saved work is kept."
    : value.message || "Preparation could not finish. Retry search to continue.";
  const canSend = !busy && (value.can_send !== false || value.send_blocked_reason === "gtm_allow_sending_required")
    && value.gmail.connected && approved.length > 0 && approved.length <= cap && (!guessedBatch || guessesReviewed === guessedBatch);
  const selfDraft = local && !!value.gmail.sender && approved.length === 1 && batch?.approved_count === 1 && approved[0].to === value.gmail.sender;
  async function send(selfOnly = false) {
    if (!batch || !canSend || (selfOnly && !selfDraft)) return;
    setSendUncertain(false);
    const ok = await run(async () => {
      const result = await journeyClient.send(owner, batch, approved.map(d => d.doc_id), cap, selfOnly);
      if (fence.current.capture()()) { setSendUncertain(false); if (result.results.sent > value!.results.sent) setStage(4); }
      return result;
    });
    if (ok === false) setSendUncertain(true);
  }

  return <div className={styles.wrap} ref={container} data-journey-step={step < 2 ? "find_customers" : step === 2 ? "review_leads" :
    value.manual_outreach?.some(item => item.booked_at) ? "call_booked" : "sent"}
    data-journey-venture={owner.ventureId} data-journey-workspace={owner.workspaceId}>
    <ol className={styles.steps} aria-label="Go-to-market progress">{stepLabels.map((label, i) => <li key={label} aria-current={i === step ? "step" : undefined}>{i + 1}. {label}</li>)}</ol>
    <ErrorNotice error={error} source="gtm.journey" as="div" className={styles.error}><p>{error}</p><button className={`${styles.button} ${styles.secondary}`} disabled={busy} onClick={() => void run(() => journeyClient.get(owner))}>Refresh saved work</button></ErrorNotice>
    {value.icp.goal === "book a first call" && <p role="status">{bookedCount > 0 ? "Goal reached: you recorded your first booked call." : "Your goal: book a first call with a potential customer or an industry person who can give insights."}</p>}
    {value.receipt && <p className={styles.receipt} role="status">{value.receipt.body_md}</p>}
    {showDiscoveryNotices && value.discovery_notice && <p role="status">{value.discovery_notice}</p>}
    {value.sending_pause && <section aria-label="Sending paused">
      <p role="alert" data-not-error>{value.sending_alert || "Workspace sending is paused."} {value.sending_pause.bounced} contacts bounced across {value.sending_pause.sent} confirmed sends in this window.</p>
      <p>Check the bounced addresses first. Resuming does not send messages; each batch still needs your approval.</p>
      <label className={styles.label}><span><input type="checkbox" disabled={busy} checked={bounceReviewed} onChange={e => setBounceReviewed(e.target.checked)} /> I checked the bounced addresses.</span></label>
      <button className={styles.button} disabled={busy || !bounceReviewed} onClick={() => void run(() => journeyClient.resumeSending(owner), () => setBounceReviewed(false))}>Resume sending</button>
    </section>}
    {value.customer_start && <OneCard title="Start from your existing customers" body="Grow what is already working. These drafts are copy-only; nothing is sent.">
      <CopyDraft title="Ask for a referral" text={value.customer_start.referral_draft} disabled={busy} current={currentDraft} />
      <CopyDraft title="Ask for an honest review or testimonial" text={value.customer_start.review_draft} disabled={busy} current={currentDraft} />
      <h3>Find lookalikes</h3><p>{value.customer_start.lookalikes_note}</p>
    </OneCard>}
    {working && !requestsImport() ? <OneCard step="Background work" title="Finding your first customers" status="working" body={value.message || "Preparing your audience, source evidence and outreach drafts. You can leave this screen and return later."}
      details={<>{value.discovery_started && value.discovery_summary && <p className={styles.muted}>{value.discovery_summary}</p>}<p className={styles.muted}>{value.estimate.basis}</p></>}>
        <ResearchProgress progress={value.research_progress} />
        {!!value.reachable_leads?.length && reachList(value.reachable_leads)}
        <button className={`${styles.button} ${styles.secondary}`} disabled={busy} onClick={() => void run(() => journeyClient.get(owner))}>Refresh saved work</button>
      </OneCard> : value.processing_blocked_reason ? <OneCard title="Review your workspace policy" body="Your hosted workspace requires this review before processing prospect information.">
        <p><a href={value.processing_blocked_reason === "aup_required" ? "https://supymem.com/terms-of-service" : "https://supymem.com/data-processing-agreement"} target="_blank" rel="noopener noreferrer">Read {value.processing_blocked_reason === "aup_required" ? "the acceptable use terms" : "the data processing agreement"}</a></p>
        <label className={styles.label}><span><input type="checkbox" checked={policyAccepted} disabled={busy} onChange={e => setPolicyAccepted(e.target.checked)} /> I have reviewed and accept this policy for my workspace.</span></label>
        <button className={styles.button} disabled={busy || !policyAccepted} onClick={() => {
          const current = fence.current.capture();
          void run(async () => {
            const next = await journeyClient.policy(owner, value.processing_blocked_reason === "aup_required" ? { aup_accepted: true } : { dpa_accepted: true });
            if (!current() || !ownsPage()) return next;
            setPolicyAccepted(false);
            return next;
          });
        }}>Save policy acceptance</button>
      </OneCard> : value.discovery_started === false && !requestsImport() ? <OneCard title={`Find customers for ${value.venture_name || "this venture"}`}
        body={value.icp.needs_input ? "You only need to add the missing details below. Nothing is sent." : value.icp.mode === "consumer" ? "You can find communities and local places where your customers gather, then review invitations you can copy. Nothing is posted or sent." : "You can find people who might need your product and check the public sources before contacting them. Nothing is sent."}>
        {audienceField}
        {problemField}
        <label className={styles.label}>Anything we should know? (existing customers, who to target or avoid)<textarea aria-label="Anything we should know? (existing customers, who to target or avoid)" className={styles.input} maxLength={2000} rows={3} value={discoveryNote} disabled={busy} onChange={e => setDiscoveryNote(e.target.value)} /></label>
        <p className={styles.muted}>Optional. {value.estimate.basis}</p>
        {value.icp.needs_input && <button className={`${styles.button} ${styles.secondary}`} disabled={busy} onClick={() => {
          if (!currentDraft()) return;
          window.dispatchEvent(new CustomEvent("supymem:ask", { detail: { action: "outreach.draft", payload: { review_only: true }, message: "Prepare editable outreach drafts from what is saved" } }));
        }}>Draft without an audience</button>}
        {askAudience && !selectedProblem ? <button className={styles.button} disabled={busy || selectedAudience.length < 8 || selectedAudience.length > 600} onClick={() => setAudienceAnswered(true)}>Continue</button> : <button className={`${styles.button} ${styles.findCustomers}`} data-usage-control="gtm_journey_find_customers" disabled={busy || !!startIssue} onClick={() => void run(() => value.icp.needs_input || audience !== null || problem !== null
          ? journeyClient.icp(owner, selectedAudience, undefined, discoveryNote.trim(), true, problem === null ? undefined : selectedProblem)
          : journeyClient.prepare(owner, false, discoveryNote.trim(), true), () => { setStage(1); setAudience(null); setAudienceAnswered(false); setProblem(null); setSearches(null); })}>Find customers</button>}
      </OneCard> : <>
      {step === 0 && <OneCard step={`1 of ${stepLabels.length} · Audience`} title="Who should hear from you?" body="Start with a specific group who feels the problem you solve."
        actions={<div className={styles.row}>{askAudience && !selectedProblem ? <button className={styles.button} disabled={busy || selectedAudience.length < 8 || selectedAudience.length > 600} onClick={() => setAudienceAnswered(true)}>Continue</button> : <button className={styles.button} disabled={busy || !!startIssue} onClick={() => void run(() => journeyClient.icp(owner, selectedAudience, undefined, undefined, false, problem === null ? undefined : selectedProblem), () => { setStage(1); setAudience(null); setAudienceAnswered(false); setProblem(null); setSearches(null); })}>{audience !== null || value.icp.needs_input ? value.prospects.length ? "Save audience and prepare" : "Save audience and search" : value.prospects.length ? "Prepare this audience" : "Search this audience"}</button>}</div>}
        details={<p className={styles.muted}>{value.icp.basis || "Suggested from your saved product context. Confirm it before treating it as your audience."}</p>}>
        {audienceField}
        {problemField}
      </OneCard>}
      {step === 1 && (value.icp.mode === "consumer" ? people : value.reachable_leads || []).length > 0 && <OneCard step="Find customers" title="Reachable leads" body="Published contact routes with evidence. Copying or opening never sends; review the draft and respect their preferences."
        actions={value.icp.mode !== "consumer" && !value.channels?.length && !value.need_signals?.length && <button className={styles.button} disabled={busy} onClick={() => setStage(2)}>Review reachable leads</button>}>
        {!!value.playbooks?.length && <><h3>Outreach playbooks for you now</h3><ul>{value.playbooks.map(playbook => <li key={playbook.key}><strong>{playbook.title}</strong>: {playbook.why}</li>)}</ul></>}
        {reachList(value.icp.mode === "consumer" ? people : value.reachable_leads || [])}
      </OneCard>}
      {step === 1 && value.icp.mode === "consumer" && <OneCard step="Find customers" title="Industry experts" body="People serving your audience who may offer insights. Published professional contact routes only; you send every message.">
        {experts.length ? reachList(experts) : <p>No public expert contact routes found yet. Refine the audience or location.</p>}
      </OneCard>}
      {step === 1 && value.icp.mode === "consumer" && <OneCard step="2 of 5 · Communities" title="Where your customers gather" body={value.message || "Public communities and anonymous need signals; nothing is posted or sent."}
        actions={<div className={styles.row}>{hasManualLeads && <button className={styles.button} disabled={busy} onClick={() => setStage(2)}>{hasReachableLeads ? "Review reachable leads" : "Review communities"}</button>}<button className={`${styles.button} ${hasManualLeads ? styles.secondary : ""}`} data-usage-control="gtm_journey_find_gatherings" disabled={busy} onClick={() => void run(() => journeyClient.prepare(owner, true, undefined, value.icp.mode === "consumer"))}>{value.icp.mode !== "consumer" || value.recruitment_opt_in ? "Retry community search" : "Find where my customers gather"}</button>{drafts.length > 0 && <button className={`${styles.button} ${styles.secondary}`} disabled={busy} onClick={() => setStage(2)}>Review drafts</button>}<button className={`${styles.button} ${styles.secondary}`} disabled={busy} onClick={() => { setAudience(value.icp.who); setStage(0); }}>Refine audience or location</button></div>}>
        {value.discovery_started && value.discovery_summary && <p className={styles.muted}>{value.discovery_summary}</p>}
        {communities}
      </OneCard>}
      {step === 1 && value.icp.mode !== "consumer" && <OneCard step="2 of 5 · Prospects" title={value.prospects.length ? `${value.prospects.length} prospect${value.prospects.length === 1 ? "" : "s"} to review` : "Find people with a reason to care"}
        status={preparationIssue && !searchBlocked && !noCandidates ? "error" : undefined}
        body={preparationIssue ? preparationMessage : needsCountries ? value.message || "Confirm each prospect’s country so we can check what outreach is allowed." : value.prospects.length ? "A source shows where a contact was found. Review fit and permission before emailing." : value.message || "Public discovery has not found eligible contacts yet. Add a contact you know, with a source and the basis for contacting them."}
        actions={<div className={styles.row}>{drafts.length > 0 && <button className={`${styles.button} ${preparationIssue ? styles.secondary : ""}`} disabled={busy} onClick={() => setStage(2)}>Review {drafts.length} draft{drafts.length === 1 ? "" : "s"}</button>}
          {hasManualLeads && <button className={styles.button} disabled={busy} onClick={() => setStage(2)}>{hasReachableLeads ? "Review reachable leads" : "Review communities"}</button>}
          {needsCountries && !confirming && <button className={styles.button} disabled={busy} data-usage-control="gtm_journey_confirm_countries" onClick={() => setConfirming(true)}>Confirm country for {waiting.length} prospect{waiting.length === 1 ? "" : "s"}</button>}
          {!searchBusy && <button className={`${styles.button} ${noCandidates ? "" : styles.secondary}`} disabled={busy} onClick={() => { setAudience(value.icp.who); setStage(0); }}>{noCandidates ? "Refine audience" : value.prospects.length ? "Edit audience and prepare" : "Edit audience and search"}</button>}
          {searchBlocked ? <button className={styles.button} data-usage-control="gtm_journey_add_known_contact" onClick={() => { if (contactDetails.current) { contactDetails.current.open = true; contactDetails.current.scrollIntoView({ block: "start", behavior: "auto" }); } }}>Add a contact you know</button>
            : <button className={`${styles.button} ${noCandidates || needsCountries || (drafts.length && !preparationIssue) ? styles.secondary : ""}`} disabled={busy} onClick={() => void run(() => journeyClient.prepare(owner, true))}>{value.prospects.length ? preparationIssue ? "Retry preparation" : value.will_search ? "Search again" : "Prepare drafts again" : preparationIssue && !noCandidates ? "Retry search" : "Search again"}</button>}</div>}>
        {confirming && <ConfirmCountries prospects={waiting} options={value.geo_options || []} disabled={busy}
          cancel={() => setConfirming(false)} save={items => run(() => journeyClient.confirmProspects(owner, items), () => setConfirming(false))} />}
        {!value.prospects.length && !people.length && <p role="status">{bookedCount > 0 ? "Your booked call is recorded. Prepare for it and record what you learn; no additional prospects were found yet." : "We found no people yet. Review the communities below, or add a contact you know with evidence."}</p>}
        {(!!value.channels?.length || !!value.need_signals?.length || !value.prospects.length) && communities}
        <p className={styles.muted}>Your audience: {value.icp.who}</p>
        {value.discovery_started && value.discovery_summary && <p className={styles.muted}>{value.discovery_summary}</p>}
        {searches === null ? <button className={`${styles.button} ${styles.secondary}`} disabled={busy} data-usage-control="gtm_journey_edit_search" onClick={() => setSearches((value.icp.search_queries || [value.icp.who]).join("\n"))}>Edit searches</button> : <>
          <label className={styles.label}>Eight signal searches (256 characters each), or one to three short phrases<textarea className={styles.input} aria-label="Search queries" disabled={busy} maxLength={2055} value={searches} onChange={e => setSearches(e.target.value)} /></label>
          <button className={`${styles.button} ${styles.secondary}`} disabled={busy || !searches.trim() || (searches.trim().split("\n").length === 8 ? searches.trim().split("\n").some(q => !q.trim() || q.trim().length > 256) : searches.trim().split("\n").length > 3 || searches.trim().split("\n").some(q => q.trim().split(/\s+/).length > 8))} data-usage-control="gtm_journey_save_search" onClick={() => void run(() => journeyClient.icp(owner, value.icp.who, searches.split("\n").map(q => q.trim()).filter(Boolean)), () => setSearches(null))}>Save searches and search</button>
        </>}
        <ul className={styles.prospects}>{(showAll ? open : open.slice(0, 3)).map(p => <li className={styles.prospect} key={p.id}>
          <strong>{p.name || p.company || "Contact"}</strong>{p.company && <span className={styles.muted}> · {p.company}</span>}
          <p>{p.why_now || p.why || "Fit needs review."}</p><span className={styles.tag}>{p.confidence ? `${p.confidence === "inferred" ? "Inferred" : "Observed"} signal` : p.evidence_label || "Unverified fit"}</span>{safeEvidenceUrl(p.source_url) && <> · <a href={safeEvidenceUrl(p.source_url)} target="_blank" rel="noopener noreferrer">View source</a></>}
          {p.signal && <blockquote>{plainText(p.signal.quote || "")}</blockquote>}
          {p.person && <p>{p.person} · {p.person_role}{safeEvidenceUrl(p.person_source_url) && <> · <a href={safeEvidenceUrl(p.person_source_url)} target="_blank" rel="noopener noreferrer">Person source</a></>}</p>}
          {p.email_status && <p className={styles.muted}>Email: {p.email_route ? `${p.email_route.confidence_label} · ${p.email} · ${p.email_route.confidence}% confidence` : p.email_status === "published" ? `Published · ${p.email}` : "None published"}{safeEvidenceUrl(p.contact_source_url) && <> · <a href={safeEvidenceUrl(p.contact_source_url)} target="_blank" rel="noopener noreferrer">Contact source</a></>}</p>}
          {p.imported && <p className={styles.muted}>Imported contact · {p.status || "New"}{p.last_contacted ? ` · Last contacted ${p.last_contacted}` : ""}. {p.continuation_action === "review_history" ? "Review the previous conversation before following up." : p.continuation_action === "wait" ? "Give the contact time to respond before following up." : p.continuation_action === "reply" || p.continuation_action === "handoff" ? "Continue in your existing conversation." : ""}</p>}
          {p.imported && <ContactHistory key={`${p.id}:${p.status}:${p.last_contacted}`} contact={p} disabled={busy} save={body => run(() => founderAssetsClient.contactHistory(owner, p.id, body))} />}
          {p.imported && p.email && p.email_check === "unknown" && <div><p className={styles.muted}>The email domain check is still unconfirmed. Check it again before preparing an email.</p><button type="button" className={`${styles.button} ${styles.secondary}`} disabled={busy} data-usage-control="gtm_contacts_verify_email" onClick={() => void run(() => founderAssetsClient.verifyContactEmail(owner, p.id))}>Check email again</button></div>}
          {p.blocked_reason && <><p className={styles.muted}>{prospectBlockCopy[p.blocked_reason] || `Outreach is blocked: ${p.blocked_reason.replaceAll("_", " ")}. Review the saved evidence before emailing.`}</p>
            {permissionBlockers.has(p.blocked_reason) && !confirming && <Eligibility disabled={busy} options={value.geo_options || []} save={body => run(() => journeyClient.eligibility(owner, p.id, body))} />}
            <button className={`${styles.button} ${styles.secondary}`} disabled={busy} data-usage-control="gtm_journey_skip_prospect" onClick={() => void run(() => journeyClient.eligibility(owner, p.id, { skip: true }))}>Skip</button>
            {p.blocked_reason === "us_postal_address_required" && <button className={`${styles.button} ${styles.secondary}`} disabled={busy} onClick={() => {
              if (addressDetails.current) { addressDetails.current.open = true; addressDetails.current.scrollIntoView({ block: "start", behavior: "auto" }); }
            }}>Add sending address</button>}
          </>}
        </li>)}</ul>
        {value.prospects.length > open.length && <button className={`${styles.button} ${styles.secondary}`} disabled={busy} data-usage-control="gtm_journey_restore_skipped" onClick={() => void run(() => journeyClient.confirmProspects(owner, value.prospects.filter(p => p.skipped).map(p => ({ contact_id: p.id, skip: false }))))}>Restore {value.prospects.length - open.length} skipped</button>}
        {open.length > 3 && <button className={`${styles.button} ${styles.secondary}`} onClick={() => setShowAll(!showAll)}>{showAll ? "Show fewer" : "Show all prospects"}</button>}
        <ImportContacts key={key} owner={owner} disabled={busy} save={async (preview, attested, basis, note) => {
          let imported: ContactImport | undefined;
          const ok = await run(async () => { imported = await founderAssetsClient.importContacts(owner, preview, attested, basis, note); return imported.journey; });
          return ok ? imported : undefined;
        }} />
        <AddContact detailsRef={contactDetails} disabled={busy} save={body => run(() => journeyClient.prospect(owner, body))} />
        <details className={styles.details} ref={addressDetails} open={value.send_blocked_reason === "gtm_sender_address_required" || undefined}><summary>Sending address for your footer</summary><p className={styles.muted}>Every outreach email needs your business mailing address. Add it here and click Save sending address; your drafts stay saved.</p><label className={styles.label}>Business mailing address<input className={styles.input} maxLength={500} value={postal || value.sender_postal_address || ""} onChange={e => setPostal(e.target.value)} /></label><button className={`${styles.button} ${styles.secondary}`} disabled={busy || !(postal || value.sender_postal_address || "").trim()} onClick={() => void run(() => journeyClient.senderAddress(owner, postal || value.sender_postal_address || ""))}>Save sending address</button></details>
      </OneCard>}
      {(step === 2 || step === 3) && manualReview && <OneCard step={`${step + 1} of ${stepLabels.length} · ${step === 2 ? "Review" : "Send"}`} title="Book your first conversation" body="Review the invitation, then copy it and send it yourself. Read community rules first; nothing is posted or sent automatically."
        actions={<div className={styles.row}>{step === 2 && <button className={styles.button} disabled={busy || !hasManualLeads && !open.length} onClick={() => {
          observeFounder({ event: "journey.step_completed", step: "review_leads", ventureId: owner.ventureId, workspaceId: owner.workspaceId });
          setStage(3);
        }}>Continue to Send</button>}<button className={`${styles.button} ${styles.secondary}`} onClick={() => setStage(1)}>Back to leads</button></div>}>
        <p>Ask for a 15-minute call with a potential customer or industry expert. {value.booking_link ? <>Share your booking link: <a href={safeEvidenceUrl(value.booking_link)} target="_blank" rel="noopener noreferrer">Choose a time</a></> : <Link href={settingsHref}>{local ? "Add your booking link in Keys and connections > Email > Booking link" : "Add your booking link in Settings"}</Link>}. You handle replies and confirm the call yourself.</p>
        {people.length > 0 && reachList(people, true)}
        {experts.length > 0 && <section aria-label="Industry experts"><h3>Industry experts</h3>{reachList(experts, true)}</section>}
        {open.length > 0 && <ul className={styles.prospects}>{open.map(contact => <li key={contact.id}><strong>{contact.person || contact.name}</strong><ManualProgress disabled={busy} current={currentDraft} mark={action => mark(contact.id, action)} outcome={manualOutreach.find(item => item.id === contact.id)} /></li>)}</ul>}
        {(value.channels || []).map(channel => <article key={channel.url}><h3>{channel.name}</h3><p>{channel.why_fits}</p><p>Posting rules: <EvidenceLink url={channel.rules_url || channel.url}>Read before posting</EvidenceLink></p><CopyDraft title={channel.channel_role === "partner/channel to approach" ? "Introduction draft" : "Conversation invitation"} text={channel.draft} disabled={busy} current={currentDraft} /><ManualProgress disabled={busy} current={currentDraft} mark={action => mark(channel.url, action)} outcome={manualOutcome(channel.source_url || channel.url)} /></article>)}
        {[false, true].map(possible => {
          const group = (value.need_signals || []).filter(signal => (signal.evidence_label === "possible_fit") === possible);
          return !!group.length && <section key={String(possible)}>{possible && possibleFitNote}{group.map(signal => <article key={`${signal.source_url}:${signal.quote}`}><CopyDraft title={signal.author ? `Reply to ${signal.author}` : "Reply to the public need"} buttonLabel="Draft a reply" text={signal.recency === "recent" ? signal.draft || (possible ? "Hi, does the problem I'm researching apply to your experience? If so, open to a 15-minute research call? Reply with a time. No pressure." : "Hi, what have you tried and what has been hardest? Open to a 15-minute research call? Reply with a time. No pressure.") : "Reply drafts require a verified post within the last 90 days. Use the community rules and current discussions instead."} disabled={busy || signal.recency !== "recent"} current={currentDraft} /><ManualProgress disabled={busy || signal.recency !== "recent"} current={currentDraft} mark={action => mark(signal.source_url, action)} outcome={manualOutcome(signal.source_url)} /></article>)}</section>;
        })}
      </OneCard>}
      {step === 2 && !manualReview && <OneCard step="3 of 5 · Review" title="Make the first message count" body="Short, honest and personal. Approve the exact message you want each person to receive."
        actions={<div className={styles.row}><button className={styles.button} disabled={busy || !approved.length} onClick={() => setStage(3)}>Review sending · {approved.length} approved</button><button className={`${styles.button} ${styles.secondary}`} onClick={() => setStage(1)}>Back to prospects</button></div>}>
        {!drafts.length && <p>No eligible drafts yet. Review contact eligibility or add an evidence-backed contact.</p>}
        {followUps > 0 && <p className={styles.receipt} role="status">{`${followUps} follow-up${followUps === 1 ? "" : "s"} ready to review.`} These are gentle reminders to people who have not replied. Nothing is sent until you approve.</p>}
        {drafts.map(d => <Draft key={value.gmail.multi_gmail_v1 === true ? `${d.doc_id}:${d.revision}` : d.doc_id} draft={d} mailboxes={value.gmail.multi_gmail_v1 === true ? value.gmail.mailboxes ?? [] : undefined} disabled={busy} current={currentDraft} save={(decision, edits, expectedRevision) => run(() => journeyClient.review(owner, { ...d, revision: expectedRevision ?? d.revision }, decision, edits))} />)}
        <SimulatedReaderCheck owner={owner} drafts={drafts.filter(d => d.state === "draft")} enabled={value.simulation_enabled === true} />
      </OneCard>}
      {step === 3 && !manualReview && <OneCard step="4 of 5 · Send" title="Send the batch you reviewed" body={value.send_blocked_reason && value.send_blocked_reason !== "gtm_allow_sending_required" ? journeySendReason(value.send_blocked_reason) : `Up to ${cap} reviewed emails in this batch. Each send is checked against your daily limit and contact preferences.`}>
        {value.send_blocked_reason && value.send_blocked_reason !== "gtm_allow_sending_required" && <ErrorNotice error={journeySendReason(value.send_blocked_reason)} source="gtm.send" />}
        {value.send_blocked_reason === "gtm_sender_address_required" && <button className={`${styles.button} ${styles.secondary}`} disabled={busy} onClick={() => setStage(1)}>Add sending address</button>}
        {needsSupymem && <button className={`${styles.button} ${styles.secondary}`} disabled={busy} data-usage-control="gtm_send_ask_supymem" onClick={() => void run(() => journeyClient.askSending(owner))}>{local ? "Turn on email sending" : "Ask Supymem to turn on sending"}</button>}
        {batch?.credit_status && batch.credit_status !== "configured" && batch.credit_status !== value.send_blocked_reason && <p className={styles.muted}>Your sending connection is not ready. <Link href={settingsHref}>{local ? "Check Keys and connections > Email, then try again" : "Check Settings, then try again"}</Link>.</p>}
        {!value.gmail.connected ? <><p className={styles.muted}>{local ? "Your email is not set up. Save your outgoing mail settings to send emails you approve. You can find customers without it." : "Gmail is not connected. Connect it to send emails you approve. You can find customers without it."}</p><Link className={styles.button} href={connections}>{local ? "Set up Email in Keys and connections" : "Connect Gmail"}</Link></> : <p className={styles.muted}>Sending from {value.gmail.sender || (local ? "your saved email account" : "your connected Gmail account")}. <Link href={connections}>Review mailbox</Link></p>}
        <ul>{approved.map(d => <li key={d.doc_id}>{d.to} · {d.subject}</li>)}</ul>
        {guessedBatch && <label><input type="checkbox" checked={guessesReviewed === guessedBatch} onChange={event => setGuessesReviewed(event.target.checked ? guessedBatch : "")} />{approved.map(draft => guessedEmailWarning(draft.email_route ?? undefined)).filter(Boolean).join(" ")} I reviewed these addresses before sending.</label>}
        {sendUncertain && <p className={styles.error} role="status">The sending response is unconfirmed. Refresh saved work before retrying this same batch.</p>}
        {value.allow_sending === true && cap > 0 && approved.length > cap && <p role="alert" data-not-error>Approve at most {cap} emails for this batch. Return to review and skip the rest.</p>}
        <div className={styles.row}><button className={`${styles.button} ${!value.gmail.connected ? styles.secondary : ""}`} disabled={!canSend} onClick={() => void send()}>{busy ? "Sending…" : `Send ${approved.length} reviewed email${approved.length === 1 ? "" : "s"}`}</button>{selfDraft && <button className={`${styles.button} ${styles.secondary}`} disabled={!canSend} onClick={() => void send(true)}>Send a test to yourself</button>}<button className={`${styles.button} ${styles.secondary}`} disabled={busy} onClick={() => setStage(2)}>Review drafts</button></div>
      </OneCard>}
      {step === 4 && <OneCard id="gtm-journey-results" step="5 of 5 · Results" title="What happened after outreach" body="Replies are evidence for your next decision. Waitlist signups remain unverified interest.">
        {manualOutreach.length > 0 && <section aria-label="Manually recorded outreach"><h3>{`${bookedCount} ${bookedCount === 1 ? "call" : "calls"} booked`}</h3><p className={styles.muted}>You recorded these outcomes. They are not email delivery receipts.</p><ul className={styles.prospects}>{manualOutreach.map(item => <li className={styles.prospect} key={item.id}><strong>{item.name}</strong><p>{item.booked_at ? "Call booked" : "Marked as sent"}</p>{item.source_url && <EvidenceLink url={item.source_url}>Contact source</EvidenceLink>}<ManualProgress disabled={busy} current={currentDraft} mark={action => mark(item.id, action)} outcome={item} /></li>)}</ul></section>}
        <div className={styles.summary}>{[[value.results.sent, "Sent"], [value.results.replied, "Replied"], [value.results.interested, "Possible interest"]].map(([n, label]) => <div key={label}><strong>{n}</strong><span className={styles.muted}>{label}</span></div>)}</div>
        {value.results.replies.length ? <ul className={styles.prospects}>{value.results.replies.map((r, i) => <li className={styles.prospect} key={r.id || i}><strong>{r.from || "Prospect reply"}</strong>{r.classification === "automated" && <span className={styles.tag}>Automated response</span>}<p>{r.body || r.classification || "Open your Gmail inbox to read this reply."}</p></li>)}</ul> : <p className={styles.muted}>{manualReview ? "You handle replies yourself. Record a booked call above when someone agrees." : "No replies recorded yet. Supymem checks your connected mailbox in the background. If someone answered somewhere else, log their reply below."}</p>}
        {sentContacts.map(c => <ContactTools key={c.id} contact={c} reply={value.results.replies.find(r => r.contact_id === c.id)} work={value.contact_work?.[c.id]} hasBookingLink={!!value.booking_link} settingsHref={settingsHref} disabled={busy}
          act={(action, body) => run(() => journeyClient.contact(owner, c.id, action, body))} />)}
        <section aria-label="What you learned"><h3>What you learned</h3>
          {value.learned?.calls ? <ul>{([["Confirmed", value.learned.pains_confirmed], ["Not confirmed", value.learned.pains_refuted], ["Objections", value.learned.objections]] as const).filter(([, items]) => items.length).map(([label, items]) => <li key={label}><strong>{label}:</strong> {items.join("; ")}</li>)}
            <li><strong>Said they would pay:</strong> {`${value.learned.would_pay} of ${value.learned.calls} ${value.learned.calls === 1 ? "call" : "calls"}`}</li></ul>
            : <p className={styles.muted}>After a call, add your notes under the person above. What you learned will appear here.</p>}
        </section>
        <p><strong>{`${value.waitlist_signups ?? 0} waitlist signup${value.waitlist_signups === 1 ? "" : "s"}`}</strong>. <a href="#waitlist" onClick={() => document.getElementById("waitlist")?.setAttribute("open", "")}>See your waitlist</a></p>
        <NextStepCard ventureId={owner.ventureId} revision={JSON.stringify([value.results, value.manual_outreach, value.contact_work])} />
        <div className={styles.row}><button className={styles.button} disabled={busy} onClick={() => void run(() => journeyClient.get(owner))}>Refresh results</button><button className={`${styles.button} ${styles.secondary}`} onClick={() => setStage(drafts.length ? 2 : 1)}>Review more outreach</button></div>
      </OneCard>}
    </>}
  </div>;
}

function ContactHistory({ contact, disabled, save }: { contact: JourneyProspect; disabled: boolean; save: (body: { status: "new" | "contacted" | "replied" | "interested"; last_contacted: string | null }) => Promise<boolean | undefined> }) {
  const [status, setStatus] = useState<"new" | "contacted" | "replied" | "interested">(["contacted", "replied", "interested"].includes(contact.status || "") ? contact.status as "contacted" | "replied" | "interested" : "new");
  const [date, setDate] = useState(contact.last_contacted?.slice(0, 10) || "");
  return <details className={styles.details}><summary data-usage-control="gtm_contacts_history_open">Update existing outreach history</summary>
    <p className={styles.muted}>Record what already happened with this person. Saving history does not send a message.</p>
    <label className={styles.label}>Outreach status<select className={styles.input} value={status} disabled={disabled} data-usage-control="gtm_contacts_history_status" onChange={event => setStatus(event.target.value as typeof status)}><option value="new">Not contacted</option><option value="contacted">Contacted</option><option value="replied">Replied</option><option value="interested">Interested</option></select></label>
    <label className={styles.label}>Last contacted, if known<input className={styles.input} type="date" value={date} max={new Date().toISOString().slice(0, 10)} disabled={disabled} data-usage-control="gtm_contacts_history_date" onChange={event => setDate(event.target.value)} /></label>
    <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled} data-usage-control="gtm_contacts_history_save" onClick={() => void save({ status, last_contacted: date || null })}>Save history</button>
  </details>;
}

function ConfirmCountries({ prospects, options, disabled, save, cancel }: { prospects: JourneyProspect[]; options: JourneyGeoOption[]; disabled: boolean; save: (items: ProspectConfirmation[]) => Promise<boolean | undefined>; cancel: () => void }) {
  type Row = { country: string; basis: string; note: string; skip: boolean };
  const byCode = new Map(options.map(o => [o.code, o]));
  const names = typeof Intl !== "undefined" && "DisplayNames" in Intl ? new Intl.DisplayNames(["en"], { type: "region" }) : null;
  const label = (code: string) => { try { return code === "EU" ? "European Union" : names?.of(code) || code; } catch { return code; } };
  const [rows, setRows] = useState<Record<string, Row>>(Object.fromEntries(prospects.map(p => [p.id, { country: p.country || "", basis: byCode.get(p.country || "")?.default_basis || "legitimate_interest", note: "", skip: false }])));
  const set = (id: string, patch: Partial<Row>) => setRows(all => ({ ...all, [id]: { ...all[id], ...patch } }));
  const row = (p: JourneyProspect): Row => rows[p.id] || { country: "", basis: "legitimate_interest", note: "", skip: false };
  const ready = (p: JourneyProspect) => { const r = row(p); return r.skip || (!!r.country && (!(r.basis === "consent" || byCode.get(r.country)?.note_required) || !!r.note.trim())); };
  const items = (): ProspectConfirmation[] => prospects.map(p => { const r = row(p); return r.skip ? { contact_id: p.id, skip: true } : { contact_id: p.id, country: r.country, lawful_basis: r.basis, lawful_basis_note: r.note }; });
  return <section className={styles.details} aria-label="Confirm prospect countries"><p className={styles.muted}>Pick the country where each company is based. A published address or phone number may have filled it in. Some countries also need a note on how the contact agreed to hear from you.</p>
    {prospects.map(p => { const r = row(p); const needsNote = r.basis === "consent" || !!byCode.get(r.country)?.note_required; return <div key={p.id} className={styles.prospect}>
      <strong>{p.company || p.name}</strong>{p.country && p.country_source && <span className={styles.muted}> · found in their {p.country_source.replace("_", " ")}</span>}
      <label className={styles.label}>Country<select className={styles.input} aria-label={`Country for ${p.company || p.name}`} disabled={disabled || r.skip} value={r.country} onChange={e => set(p.id, { country: e.target.value, basis: byCode.get(e.target.value)?.default_basis || "legitimate_interest" })}><option value="">Choose a country</option>{options.map(o => <option key={o.code} value={o.code}>{label(o.code)}</option>)}</select></label>
      {r.country && !r.skip && <label className={styles.label}>Basis for contacting<select className={styles.input} disabled={disabled} value={r.basis} onChange={e => set(p.id, { basis: e.target.value })}><option value="legitimate_interest">Business outreach to a company contact</option><option value="consent">They agreed to be contacted</option></select></label>}
      {needsNote && !r.skip && <label className={styles.label}>How did they agree to hear from you?<textarea className={styles.input} maxLength={1000} disabled={disabled} value={r.note} onChange={e => set(p.id, { note: e.target.value })} /></label>}
      <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled} data-usage-control="gtm_journey_skip_prospect" onClick={() => set(p.id, { skip: !r.skip })}>{r.skip ? "Undo skip" : "Skip"}</button>
    </div>; })}
    <div className={styles.row}><button type="button" className={styles.button} disabled={disabled || !prospects.every(ready)} data-usage-control="gtm_journey_save_countries" onClick={() => void save(items())}>Save and check eligibility</button><button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled} onClick={cancel}>Cancel</button></div></section>;
}

function Eligibility({ disabled, save, options = [] }: { disabled: boolean; options?: JourneyGeoOption[]; save: (body: { country: string; lawful_basis: string; lawful_basis_note: string }) => Promise<boolean | undefined> }) {
  const [country, setCountry] = useState(""); const [basis, setBasis] = useState("consent"); const [note, setNote] = useState("");
  const rule = options.find(o => o.code === country);
  const needsNote = basis === "consent" || (rule ? rule.note_required : true);
  return <details className={styles.details}><summary>Review permission to contact</summary><label className={styles.label}>Country code<input className={styles.input} maxLength={2} placeholder="US" value={country} onChange={e => { const code = e.target.value.toUpperCase(); setCountry(code); const next = options.find(o => o.code === code); if (next) setBasis(next.default_basis); }} /></label>
    <label className={styles.label}>Basis for contacting<select className={styles.input} value={basis} onChange={e => setBasis(e.target.value)}><option value="consent">They agreed to be contacted</option><option value="legitimate_interest">Documented legitimate interest</option></select></label>
    {needsNote && <label className={styles.label}>Evidence of permission<textarea className={styles.input} maxLength={1000} value={note} onChange={e => setNote(e.target.value)} /></label>}
    <button className={`${styles.button} ${styles.secondary}`} disabled={disabled || country.length !== 2 || (needsNote && !note.trim())} onClick={() => void save({ country, lawful_basis: basis, lawful_basis_note: note })}>Save and check eligibility</button></details>;
}

function AddContact({ disabled, save, detailsRef }: { disabled: boolean; detailsRef?: RefObject<HTMLDetailsElement | null>; save: (body: { name: string; email: string; company: string; source_url: string; country: string; lawful_basis: string; lawful_basis_note: string }) => Promise<boolean | undefined> }) {
  const [form, setForm] = useState({ name: "", email: "", company: "", source_url: "", country: "", lawful_basis: "consent", lawful_basis_note: "" });
  return <details className={styles.details} ref={detailsRef}><summary>Add a contact with evidence</summary><p className={styles.muted}>Use a real source and the actual basis for contacting this person. Do not infer permission from a public email address.</p>
    <form onSubmit={e => { e.preventDefault(); if (!disabled) void save(form); }}>
      {([["name", "Name", "text"], ["email", "Email", "email"], ["company", "Company", "text"], ["source_url", "Source URL", "url"], ["country", "Country code", "text"], ["lawful_basis_note", "Evidence of permission", "text"]] as const).map(([field, label, type]) => <label className={styles.label} key={field}>{label}<input className={styles.input} type={type} required={field !== "company"} maxLength={field === "country" ? 2 : field === "name" || field === "company" ? 150 : field === "email" ? 254 : field === "lawful_basis_note" ? 1000 : 2000} value={form[field]} onChange={e => setForm({ ...form, [field]: field === "country" ? e.target.value.toUpperCase() : e.target.value })} /></label>)}
      <label className={styles.label}>Basis for contacting<select className={styles.input} value={form.lawful_basis} onChange={e => setForm({ ...form, lawful_basis: e.target.value })}><option value="consent">They agreed to be contacted</option><option value="legitimate_interest">Documented legitimate interest</option></select></label>
      <button className={`${styles.button} ${styles.secondary}`} disabled={disabled} type="submit">Add contact and prepare draft</button>
    </form></details>;
}

const outcomeLabels: Record<ReplyOutcome, string> = { interested: "Interested", not_now: "Not now", not_a_fit: "Not a fit", opted_out: "Opted out", wrong_person: "Wrong person", question: "Has a question", escalate: "Angry or legal objection" };

/** Everything a founder does with one contacted person after the send: log a reply, answer it, prepare for and learn from a call. */
function ContactTools({ contact, reply, work, hasBookingLink, settingsHref, disabled, act }: { contact: JourneyProspect; reply?: Journey["results"]["replies"][number]; work?: NonNullable<Journey["contact_work"]>[string]; hasBookingLink: boolean; settingsHref: string; disabled: boolean; act: (action: ContactAction, body?: { text?: string; outcome?: ReplyOutcome }) => Promise<boolean | undefined> }) {
  const [text, setText] = useState("");
  const [outcome, setOutcome] = useState<ReplyOutcome>("interested");
  const interested = reply?.classification === "human_positive";
  return <article className={styles.draft}><strong>{contact.name}{contact.company ? ` · ${contact.company}` : ""}</strong>
    {work?.reply_attention && <p role="alert" data-not-error>{work.reply_attention} <a href="https://mail.google.com" target="_blank" rel="noopener noreferrer">Review in Gmail</a></p>}
    {work?.follow_up && <p role="status">{work.follow_up}</p>}
    {reply?.outcome === "question" && <p>Answer their question yourself in <a href="https://mail.google.com" target="_blank" rel="noopener noreferrer">Gmail</a>; nothing is sent automatically.</p>}
    {reply?.outcome === "wrong_person" && <><pre className={styles.draftBody}>{work?.referral_ask}</pre><button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled} onClick={() => void act("reply-draft")}>Prepare referral ask for review</button></>}
    <details className={styles.details}><summary data-usage-control="gtm_reply_open">Log a reply</summary>
      <p className={styles.muted}>Paste what they wrote, from any channel. Opt-outs stop all further outreach to them.</p>
      <label className={styles.label}>Their reply<textarea className={styles.input} maxLength={2000} value={text} disabled={disabled} onChange={e => setText(e.target.value)} /></label>
      <label className={styles.label}>How did they respond?<select className={styles.input} value={outcome} disabled={disabled} onChange={e => setOutcome(e.target.value as ReplyOutcome)}>{(Object.keys(outcomeLabels) as ReplyOutcome[]).map(k => <option key={k} value={k}>{outcomeLabels[k]}</option>)}</select></label>
      <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled || !text.trim()} data-usage-control="gtm_reply_save" onClick={() => void act("reply", { text, outcome }).then(ok => { if (ok) setText(""); })}>Save reply</button>
    </details>
    {interested && <>
      <div className={styles.row}>
        <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled || !hasBookingLink} data-usage-control="gtm_reply_draft" onClick={() => void act("reply-draft")}>Draft a reply</button>
        <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled} data-usage-control="gtm_call_brief" onClick={() => void act("brief")}>{work?.brief ? "Refresh call brief" : "Prepare for the call"}</button>
      </div>
      {!hasBookingLink && <p className={styles.muted}>To draft a reply, first add your booking link in <Link href={settingsHref}>{isLocalConsole() ? "Keys and connections > Email > Booking link" : "Settings"}</Link>.</p>}
      <p className={styles.muted}>A drafted reply appears in Review for you to edit, approve and send. Nothing is sent automatically.</p>
      {work?.brief && <CallBriefView text={work.brief} />}
      <details className={styles.details}><summary data-usage-control="gtm_call_notes_open">Add call notes</summary>
        <p className={styles.muted}>Write or dictate what they said. Supymem pulls out what you learned, using only what the notes say.</p>
        <CallNotesForm contactId={contact.id} initial={work?.call_notes || ""} disabled={disabled} save={text => act("call-notes", { text })} />
        {work?.learned && <p className={styles.muted} role="status">Learnings saved. See “What you learned” below.</p>}
      </details>
    </>}
  </article>;
}
