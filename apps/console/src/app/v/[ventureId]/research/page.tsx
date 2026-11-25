"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { NextStepButton, useNextStep } from "@/components/layout/NextStepStrip";
import { CaseReferences } from "@/components/planning/CaseReferences";
import { api, downloadFile, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import { usageLimitMessage } from "@/lib/credits";
import { jobFailure, reportJobFailure } from "@/lib/durableJobs";
import { jobSucceeded, useDurableJob } from "@/hooks/useDurableJob";
import { conciseFinding, groupResearchSources, previewResearchQuote, researchTopics, researchUnknownNextTest, researchAllUnknownSummary, type ResearchVerdict } from "@/lib/researchPresentation";
import { sourceDomain } from "@/lib/researchBrief";
import { researchNextStep, researchReportState, savedPlanStatus, type SavedPlanStatus } from "@/lib/researchNextStep";
import { hasPageChoices, nextStepButton } from "@/lib/nextStep";
import { plainJob } from "@/lib/jobCopy";
import { isLocalConsole } from "@/lib/localMode";
import { loadResearchRecovery, researchEmptyMessage, unknownResearchRecovery, researchStartDecision, researchReceiptMatches, type ResearchReceiptScope, type ResearchRecovery } from "@/lib/researchRecovery";
import { OneCard, OneCardStack } from "@/components/primitives/OneCard";
import { PageChoices } from "@/components/site/PageChoices";
import { useDetailLevel } from "@/lib/detailLevel";
import { displayResearchText, Reveal, RunningShimmer, SourceChip, Stat } from "@/components/research/ResearchBrief";
import { PriorArt, type PriorArtSummary } from "@/components/research/PriorArt";
import { StartupGraveyard, type FailedStartup } from "@/components/research/StartupGraveyard";
import { PriorArtRating, type PriorArtRatings } from "@/components/research/PriorArtRating";
import styles from "./research.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { ResearchProgress } from "@/components/primitives/ResearchProgress";

type PricingPoint = { product: string; price: string; billing_period: string; source: string };
type Finding = { id: string; title: string; url: string | null; source: string; summary?: string; category: string;
  display_pricing?: PricingPoint | null; display_topic?: import("@/lib/researchPresentation").ResearchTopic | null;
  additional_excerpts?: { text?: string }[];
  evidence_kind?: string;
  observed_at?: string; search_queries?: string[]; search_intents?: string[]; license?: string | null; license_ok?: boolean | null;
  reuse_recommendation?: string; founder_flag?: "pin" | "exclude" | null };
type ReviewQuote = { source_digest: string; sufficient_now: boolean };
type ResearchReply = { job_id?: unknown; body_md?: unknown; actions?: { label?: string; href?: string }[] };
type Review = { overview?: string; observations?: { source_id: string; quote: string }[];
  assessments?: { category: string; source_ids: string[]; assessment: string }[];
  service_checks?: { source_id: string; pricing_quote?: string; display_pricing?: PricingPoint | null; limits_quote?: string; credentials_quote?: string }[];
  competitor_checks?: { source_id: string; product_quote?: string | null; customer_quote?: string | null;
    positioning_quote?: string | null; pricing_quote?: string | null; display_pricing?: PricingPoint | null }[];
  proposed_tests?: string[] };
type EvidenceDimension = { status: "unknown" | "supporting_leads" | "challenging_leads" | "mixed_leads";
  source_strength?: string; support?: { url?: string; finding?: string }[];
  challenge?: { url?: string; finding?: string }[]; unknown?: string };
type DisplayFinding = { name?: string; finding: string; quote?: string; url: string; source_id?: string };
type DisplayDimension = { support: DisplayFinding[]; challenge: DisplayFinding[] };
type Report = { id: string; kind: "deep" | "validation"; state: string; job_id?: string; revision_id: string;
  has_customers?: boolean;
  calls_used?: number; budget_calls?: number; created_at?: string; completed_at?: string;
  summary?: { coverage_gaps?: string[]; review?: Review | null; error_code?: string;
    review_next_step?: string;
    search_notice?: { state: "key_rejected" | "limited" | "unavailable" | "no_matches"; next_step: string; settings_href?: string };
    stopped_early?: boolean; next_questions?: { kind: string; text: string; action?: string }[];
    prior_art?: PriorArtSummary | null;
    startup_graveyard?: FailedStartup[] | null;
    evidence_dimensions?: Record<string, EvidenceDimension>;
    founder_report?: { findings: DisplayFinding[]; contradictions: DisplayFinding[]; assumptions: string[];
      dimensions: Record<string, DisplayDimension>; empty_message?: string | null; opening?: string };
    assumed_questions?: boolean; idea_session_id?: string; idea_revision_id?: string;
    verdict?: ResearchVerdict;
    usage?: { credits_micro?: number; has_unsettled?: boolean };
    validation?: { summary?: string; verdict?: string; findings?: { method: string; assessment: string; source_ids: string[] }[];
      proposed_tests?: { hypothesis: string; action: string; success_measure: string }[] };
    operations?: { key: string; state: string; provider?: string; credits_micro?: number; error_code?: string }[] };
  findings?: Record<string, Finding[]> };
const categories: Record<string, string> = { direct: "Direct competitors", adjacent: "Adjacent solutions", funded: "Funded competitors",
  accelerator: "Accelerator companies", emerging: "Emerging competitors", services: "Services and APIs", oss: "Open-source projects" };
const dimensionLabels: Record<string, string> = { problem_validation: "Problem validation", customer_demand: "Customer demand",
  competition: "Competition", pricing: "Pricing", willingness_to_pay: "Willingness to pay", market_size: "Market size", risks: "Risks" };
const dimensionOrder = Object.keys(dimensionLabels);
type ResearchPhase = "all" | "review" | "competitors" | "pricing" | "demand";
const phaseAction: Record<"all" | "competitors" | "pricing" | "demand", string> = {
  all: "Run research again",
  competitors: "Research competitors",
  pricing: "Investigate pricing",
  demand: "Validate customer demand",
};
const label = (value: string) => categories[value] || value.replace(/_/g, " ");
function sourceUrl(value: string | null): string | undefined {
  if (!value) return undefined;
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.hostname === "invalid" || url.hostname.endsWith(".invalid") ? "#" : url.href : undefined; }
  catch { return undefined; }
}
function when(value?: string): string {
  const date = value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? date.toLocaleString() : "Time unavailable";
}

function CompetitorQuote({ label: fieldLabel, quote }: { label: string; quote?: string | null }) {
  if (!displayResearchText(quote)) return null;
  return <div className={styles.competitorField}>
    <strong>{fieldLabel}</strong>
    {quote?.trim() ? <>
      <p>{previewResearchQuote(quote)}</p>
      <details><summary>Read full {fieldLabel.toLowerCase()} excerpt</summary>
        <blockquote>{quote}</blockquote>
      </details>
    </> : null}
  </div>;
}

export default function ResearchPage() {
  const router = useRouter();
  const ventureId = String(useParams().ventureId || "");
  const searchParams = useSearchParams();
  const selectedDossier = searchParams.get("dossier") || undefined;
  const local = isLocalConsole();
  const serverStep = useNextStep(ventureId);
  const active = useRef(ventureId); active.current = ventureId;
  const reportDetails = useRef<HTMLDetailsElement>(null);
  const generation = useRef(0);
  const pending = useRef(false);
  const [ventureName, setVentureName] = useState("");
  const [reports, setReports] = useState<Report[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [gtmOrder, setGtmOrder] = useState<"product_first" | "market_first" | null>(null);
  const [planStatus, setPlanStatus] = useState<SavedPlanStatus>("unknown");
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [setup, setSetup] = useState<{ text: string; href: string; label: string } | null>(null);
  const [syncError, setSyncError] = useState("");
  const [recovery, setRecovery] = useState<ResearchRecovery>(unknownResearchRecovery);
  const [startConfirmed, setStartConfirmed] = useState(false);
  const receipt = useRef<ResearchReceiptScope | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [stopRequestedFor, setStopRequestedFor] = useState<string | null>(null);
  const [focusToConfirm, setFocusToConfirm] = useState<"all" | "competitors" | "pricing" | "demand" | null>(null);
  function observeJob(id: string | null, revision: string | null) {
    receipt.current = id && revision ? { id, revision, venture: ventureId, owner: verifiedSessionSubject() } : null;
    setJobId(id);
  }
  const job = useDurableJob(jobId, (finished) => {
    if (!researchReceiptMatches(receipt.current, finished, active.current, verifiedSessionSubject())) return;
    observeJob(null, null);
    const succeeded = jobSucceeded(finished);
    const stopped = finished.kind === "research.deep" && finished.status === "cancelled";
    setRecovery(current => ({ ...current, jobId: null, failure: succeeded || stopped ? undefined : finished }));
    setNotice(stopped ? "Your research stopped. Check Research history for any saved sources."
      : succeeded ? "Research has finished. Checking the saved report before choosing the next step." : "");
    const completed = finished.kind === "research.deep" && succeeded ? finished.id : undefined;
    void load(completed ? report?.id : undefined, completed);
  });

  async function load(preferred = selectedDossier, completedJobId?: string) {
    const version = ++generation.current;
    let owner = verifiedSessionSubject();
    const current = () => active.current === ventureId && generation.current === version
      && (!owner || owner === verifiedSessionSubject());
    setFocusToConfirm(null);
    setStartConfirmed(false);
    setRefreshing(true);
    setError("");
    try {
      await ensureSession();
      if (!current()) return;
      owner = verifiedSessionSubject();
      const reportRead = api(`/api/ventures/${encodeURIComponent(ventureId)}/research/dossiers`).then(async result => {
        const items = (result.data as { items?: Report[] } | null)?.items;
        const completed = completedJobId && Array.isArray(items) ? items.find(item => item.job_id === completedJobId)?.id : undefined;
        const selected = completed || preferred || (!completedJobId && Array.isArray(items) ? items[0]?.id : undefined);
        const detail = current() && result.ok && selected ? await api(`/api/ventures/${encodeURIComponent(ventureId)}/research/dossiers/${encodeURIComponent(selected)}`) : null;
        return { result, detail, completed };
      });
      const ventureRead = api(`/api/ventures/${encodeURIComponent(ventureId)}`).then(async venture => ({ venture,
        restored: current() ? await loadResearchRecovery(ventureId,
          venture.ok ? (venture.data as { workspace_id?: string })?.workspace_id : undefined, { current }) : unknownResearchRecovery() }));
      const [{ result, detail, completed }, { venture, restored }, charter, plans] = await Promise.all([
        reportRead,
        ventureRead,
        api(`/api/ventures/${encodeURIComponent(ventureId)}/charter`),
        api(`/api/ventures/${encodeURIComponent(ventureId)}/plans`),
      ]);
      if (!current()) return;
      const name = venture.ok ? (venture.data as { name?: unknown })?.name : null;
      setVentureName(typeof name === "string" ? name : "");
      const explicitOrder = venture.ok ? (venture.data as { gtm_order?: unknown })?.gtm_order : null;
      const charterOrder = charter.ok ? (charter.data as { content?: { gtm_order?: unknown } })?.content?.gtm_order : null;
      const savedOrder = explicitOrder || charterOrder;
      setGtmOrder(savedOrder === "market_first" || savedOrder === "product_first" ? savedOrder : null);
      setPlanStatus(plans.ok ? savedPlanStatus(plans.data) : "unknown");
      setRecovery(restored); observeJob(restored.jobId, restored.revisionId); setSyncError(restored.error);
      if (restored.failure) reportJobFailure(restored.failure);
      if (!result.ok) { setError(apiFailure(result, "loading research")); setReady(true); return; }
      const items = (result.data as { items?: Report[] }).items;
      if (!Array.isArray(items)) { setError("The research history could not be read. Refresh to try again."); setReady(true); return; }
      setReports(items);
      if (completedJobId && !completed) {
        setNotice("Your research finished, but its report is not visible yet. Refresh Research, then check Research history.");
        if (!preferred) { setReady(true); return; }
      }
      if (!preferred && !items.length) { setReport(null); setReady(true); return; }
      if (detail?.ok) {
        const selected = detail.data as Report;
        setReport(selected);
        if (completed && selected.id === completed && completed !== selectedDossier) {
          const params = new URLSearchParams(searchParams);
          params.set("dossier", completed);
          router.replace(`${reopenPath}?${params}`, { scroll: false });
        }
        if (selected?.id && !items.some(item => item.id === selected.id)) setReports([selected, ...items]);
      } else { setReport(null); setError(detail ? apiFailure(detail, "opening the research report") : "The research report could not be read. Refresh to try again."); }
      setReady(true);
    } catch {
      if (current()) { setError("Research could not be loaded. Check your connection and refresh."); setReady(true); }
    } finally { if (current()) setRefreshing(false); }
  }
  useEffect(() => {
    active.current = ventureId;
    setRecovery(unknownResearchRecovery()); setStartConfirmed(false);
    setReports([]); setReport(null); setGtmOrder(null); setPlanStatus("unknown"); setReady(false); observeJob(null, null); setBusy(false); setNotice(""); setSetup(null); setSyncError(""); setFocusToConfirm(null);
    void load();
    return () => { generation.current++; active.current = ""; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ventureId, selectedDossier]);

  const reopenOwner = verifiedSessionSubject(), reopenEpoch = verifiedSessionEpoch(), reopenPath = usePathname();
  const reopenScope = useRef({ owner: reopenOwner, epoch: reopenEpoch, path: reopenPath, dossier: selectedDossier, active: true });
  if (reopenScope.current.owner !== reopenOwner || reopenScope.current.epoch !== reopenEpoch || reopenScope.current.path !== reopenPath
      || reopenScope.current.dossier !== selectedDossier)
    reopenScope.current = { owner: reopenOwner, epoch: reopenEpoch, path: reopenPath, dossier: selectedDossier, active: true };
  const renderedReopenScope = reopenScope.current, reopenVersion = generation.current;
  const reopenScopeCurrent = () => renderedReopenScope.active && reopenScope.current === renderedReopenScope
    && reopenOwner === verifiedSessionSubject() && reopenEpoch === verifiedSessionEpoch() && reopenPath === window.location.pathname;
  useEffect(() => {
    renderedReopenScope.active = true; pending.current = false; setBusy(false);
    return () => { renderedReopenScope.active = false; };
  }, [renderedReopenScope]);

  function showResearchReply(reply: ResearchReply | null): boolean {
    if (typeof reply?.body_md !== "string" || !reply.body_md) return false;
    const link = Array.isArray(reply.actions) ? reply.actions.find(action =>
      action?.href === "/settings/local" || action?.href === "/settings/local/capabilities") : undefined;
    setSetup(link?.href ? { text: reply.body_md, href: link.href, label: link.label || "Open Settings" } : null);
    setNotice(reply.body_md);
    return true;
  }
  async function reopenQuestions() {
    const sessionId = report?.summary?.idea_session_id, revisionId = report?.summary?.idea_revision_id;
    const current = () => reopenScopeCurrent() && active.current === ventureId && generation.current === reopenVersion;
    if (!current() || !reopenOwner || !sessionId || !revisionId || pending.current || busy || refreshing || jobId || syncError) return;
    pending.current = true; setBusy(true); setNotice("");
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/idea/sessions/${encodeURIComponent(sessionId)}/reopen-questions`, {
        method: "POST", body: JSON.stringify({ revision_id: revisionId }),
      });
      if (!current()) return;
      if (!result.ok) { setNotice(apiFailure(result, "opening your skipped questions")); return; }
      router.push(`/v/${ventureId}/idea`);
    } catch {
      if (current()) setNotice("The questions could not be opened. Refresh and try again.");
    } finally {
      if (reopenScopeCurrent()) { pending.current = false; setBusy(false); }
    }
  }

  async function startResearch() {
    if (pending.current || busy || refreshing || jobId || syncError
        || recovery.state !== "confirmed" || recovery.personal || !recovery.revisionId) return;
    const version = generation.current, owner = verifiedSessionSubject(), revision = recovery.revisionId;
    const current = () => active.current === ventureId && generation.current === version && owner === verifiedSessionSubject();
    pending.current = true; setBusy(true); setNotice("");
    try {
      const venture = await api(`/api/ventures/${encodeURIComponent(ventureId)}`);
      if (!current()) return;
      const saved = await loadResearchRecovery(ventureId, venture.ok ? (venture.data as { workspace_id?: string })?.workspace_id : undefined, { current });
      if (!current()) return;
      setRecovery(saved); observeJob(saved.jobId, saved.revisionId); setSyncError(saved.error);
      const decision = researchStartDecision(revision, saved);
      if (decision === "observe") { setStartConfirmed(false); setNotice("Research work is already saved. Checking its status without starting another run."); return; }
      if (decision !== "start") {
        setStartConfirmed(false); setNotice("The saved scope or status changed. Review it before starting research."); return;
      }
      // A fresh key per confirmed click: a replayed request returns the same job, never a second run.
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/research/dossier/regenerate`, { method: "POST", body: JSON.stringify({ expected_revision_id: revision, request_key: crypto.randomUUID() }) });
      if (!current()) return;
      if (!result.ok) { setNotice(apiFailure(result, "starting research")); return; }
      const reply = result.data as ResearchReply | null, id = reply?.job_id;
      if (typeof id !== "string" || !id) {
        if (showResearchReply(reply)) setStartConfirmed(false);
        else setSyncError("The research request did not return a job. Check saved status before trying again.");
        return;
      }
      setStartConfirmed(false); observeJob(id, saved.revisionId); setRecovery({ ...saved, jobId: id, failure: undefined });
      setNotice("Research is queued. A saved result and verified findings are still pending.");
    } catch {
      if (current()) setSyncError("The research request could not be confirmed. Refresh saved status before trying again; this will not restart it.");
    } finally { pending.current = false; if (current()) setBusy(false); }
  }
  async function rerun(phase: ResearchPhase = "all") {
    if (!reopenScopeCurrent() || active.current !== ventureId || generation.current !== reopenVersion || !reopenOwner
        || !report || pending.current || busy || refreshing || jobId || syncError) return;
    pending.current = true;
    setBusy(true); setNotice("");
    const version = generation.current, owner = verifiedSessionSubject();
    const current = () => reopenScopeCurrent() && active.current === ventureId && generation.current === version && owner === verifiedSessionSubject();
    const path = `/api/ventures/${encodeURIComponent(ventureId)}/research/dossiers/${encodeURIComponent(report.id)}`;
    let requested = false;
    try {
      let sourceDigest: string | undefined;
      if (phase === "review") {
        const quote = await api(`${path}/review-quote`);
        if (!current()) return;
        if (!quote.ok) { setNotice(apiFailure(quote, "checking your saved sources")); return; }
        const value = quote.data as ReviewQuote | null;
        if (typeof value?.source_digest !== "string" || !value.source_digest || typeof value.sufficient_now !== "boolean") {
          setNotice("Your saved sources could not be checked. Refresh research, then try again."); return;
        }
        if (!value.sufficient_now) { setNotice(apiFailure({ok: false, status: 402, data: null}, "reviewing your sources")); return; }
        sourceDigest = value.source_digest;
      }
      requested = true;
      const result = await api(`${path}/rerun`, {
        method: "POST", body: JSON.stringify({ phase, source_digest: sourceDigest, request_key: crypto.randomUUID() }),
      });
      if (!current()) return;
      if (!result.ok) {
        const message = apiFailure(result, "starting another research run");
        if (!result.status || result.status >= 500) setSyncError(message); else setNotice(message);
        return;
      }
      const reply = result.data as ResearchReply | null, id = reply?.job_id;
      if (typeof id !== "string" || !id) {
        if (!showResearchReply(reply)) setSyncError("The research request did not return a job. Refresh its status before choosing your next step.");
        return;
      }
      setFocusToConfirm(null);
      observeJob(id, report.revision_id); setNotice(phase === "review" ? "Review queued using saved sources. Searches will not run again." :
        phase === "all" ? "Research is queued. Previous reports remain available while the new run works." :
          `${phaseAction[phase]} is queued. Previous reports remain available while the new run works.`);
    } catch {
      if (current()) {
        if (requested) setSyncError("The research request could not be confirmed. Refresh saved status before choosing your next step.");
        else setNotice("Your saved sources could not be checked. Refresh research, then try again.");
      }
    } finally {
      if (reopenScopeCurrent() && active.current === ventureId) { pending.current = false; setBusy(false); }
    }
  }
  async function flag(finding: Finding, value: "pin" | "exclude" | null) {
    if (busy || pending.current || !report) return;
    pending.current = true;
    setBusy(true);
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/research/findings/${encodeURIComponent(finding.id)}`, { method: "PATCH", body: JSON.stringify({ founder_flag: value }) });
      if (active.current !== ventureId) return;
      if (result.ok) await load(report.id);
      else setNotice(apiFailure(result, "saving your research feedback"));
    } finally { pending.current = false; if (active.current === ventureId) setBusy(false); }
  }
  const review = report?.summary?.review;
  const findings = Object.values(report?.findings || {}).flat();
  const qualifiedFindings = findings.filter((finding) => finding.founder_flag !== "exclude" && !["search_lead_unverified", "name_source"].includes(finding.evidence_kind || "") && finding.category !== "review_lead");
  const groupedFindings = groupResearchSources(findings, review?.assessments || []);
  const reportState = researchReportState(report?.state || "", !!jobId, report?.revision_id, recovery.revisionId);
  const researching = !!jobId || reportState === "running";
  const canStopObservedJob = job.job?.id === jobId ? job.job?.kind === "research.deep" : recovery.state === "confirmed";
  const researchJobId = jobId ? (canStopObservedJob ? jobId : null)
    : reportState === "running" && report?.kind === "deep" ? report.job_id : null;
  async function stopResearch(id: string) {
    if (pending.current || stopRequestedFor === id) return;
    pending.current = true;
    setBusy(true);
    const version = generation.current;
    const failed = "The stop was not confirmed. On Research, choose Stop research to try again, or refresh to check its progress.";
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/research/jobs/${encodeURIComponent(id)}/stop`, { method: "POST" });
      if (!reopenScopeCurrent() || generation.current !== version) return;
      if (result.ok) {
        setStopRequestedFor(id);
        setNotice("");
        if (jobId === id) job.retry();
        else void load(report?.id);
      } else setNotice(failed);
    } catch {
      if (reopenScopeCurrent() && generation.current === version) setNotice(failed);
    } finally {
      if (reopenScopeCurrent()) { pending.current = false; setBusy(false); }
    }
  }
  const researchStatus = job.job?.status || report?.state;
  const finishedReport = reportState === "complete" || reportState === "partial";
  const searchNotice = reportState === "outdated" ? undefined : report?.summary?.search_notice;
  const showFindings = finishedReport && (!searchNotice || qualifiedFindings.length > 0);
  const verdict = finishedReport ? report?.summary?.verdict : undefined;
  const hasCustomers = report?.has_customers === true || serverStep?.has_customers === true;
  const failedCopy = recovery.failure ? plainJob(recovery.failure) : null;
  const allUnknownSummary = researchAllUnknownSummary(report?.summary?.evidence_dimensions, findings.filter(finding => finding.founder_flag !== "exclude").length, hasCustomers);
  const founderReport = report?.summary?.founder_report;
  const dimensionFindings = (key: string, field: "support" | "challenge"): DisplayFinding[] => {
    const saved = founderReport?.dimensions[key]?.[field];
    if (saved) return saved.filter(entry => sourceUrl(entry.url) && displayResearchText(entry.finding));
    if (founderReport) return [];
    const seen = new Set<string>();
    return (report?.summary?.evidence_dimensions?.[key]?.[field] || []).flatMap(lead => {
      const source = qualifiedFindings.find(finding => finding.url === lead.url);
      if (!source || !sourceUrl(source.url)) return [];
      const text = displayResearchText(lead.finding) || (field === "support" ? displayResearchText(source.summary) : "");
      const identity = key === "competition" ? new URL(source.url!).hostname.replace(/^www\./, "") : source.url!;
      if (!text || seen.has(identity)) return [];
      seen.add(identity);
      return [{ finding: key === "competition" ? `${source.title}: ${text}` : text, url: source.url! }];
    }).slice(0, 5);
  };
  const summaryFindings = founderReport?.findings || ["competition", "customer_demand", "pricing", "problem_validation"].flatMap(key => dimensionFindings(key, "support").slice(0, 1)).slice(0, 3);
  const contradictions = founderReport?.contradictions || dimensionOrder.flatMap(key => dimensionFindings(key, "challenge")).slice(0, 3);
  const populatedDimensions = dimensionOrder.filter(key => dimensionFindings(key, "support").length || dimensionFindings(key, "challenge").length);
  const evidence = (id: string) => findings.find((finding) => finding.id === id);
  const serviceChecks = (review?.service_checks || []).filter(check => sourceUrl(evidence(check.source_id)?.url || null)
    && (check.display_pricing || [check.limits_quote, check.credentials_quote].some(displayResearchText)));
  const competitorChecks = (review?.competitor_checks || []).filter(check => sourceUrl(evidence(check.source_id)?.url || null)
    && [check.product_quote, check.customer_quote, check.positioning_quote, check.pricing_quote].some(displayResearchText)).slice(0, 5);
  const renderFinding = (entry: DisplayFinding, index: number) => <li key={index}>{entry.finding} · <a href={sourceUrl(entry.url)} target="_blank" rel="noopener noreferrer">{sourceDomain(entry.url) || "Source"}</a></li>;
  const currentReport = report?.kind === "deep" && finishedReport && recovery.state === "confirmed"
    && !!recovery.revisionId && report.revision_id === recovery.revisionId && !busy && !refreshing && !syncError;
  const usableStep = currentReport && !recovery.personal && serverStep?.key !== "work.running"
    && (/^(?:site\.building|research\.running)$/.test(serverStep?.key || "")
      || !["working", "queued"].includes(serverStep?.runState || "")) ? serverStep : null;
  const pageOptional = Boolean(hasPageChoices(usableStep));
  let nextStep = report ? researchNextStep(ventureId, report.kind, reportState, gtmOrder, planStatus, pageOptional) : null;
  if (report?.kind === "deep" && recovery.personal && recovery.state === "confirmed" && serverStep && !syncError) {
    nextStep = { href: `/v/${encodeURIComponent(ventureId)}/idea`, label: "Review local build",
      explanation: "This personal project follows the reviewed local-build route." };
  } else if (report?.kind === "deep" && finishedReport && !pageOptional) {
    nextStep = usableStep ? { href: usableStep.href, label: usableStep.cta_label, explanation: usableStep.detail } : null;
  }
  if (hasCustomers && !recovery.personal && report?.kind === "deep" && currentReport && usableStep
    && !/^(?:site\.building|research\.running)$/.test(usableStep.key)) {
    nextStep = { href: `/gtm?venture=${encodeURIComponent(ventureId)}`, label: "Find customers",
      explanation: "Your paying customers are demand evidence. Test the next repeatable acquisition channel." };
  }
  const forwardRecovery = (recovery.state === "confirmed" && !!recovery.revisionId)
    || (recovery.state === "missing" && serverStep?.key === "gtm.outreach");
  const noReportStep = ready && !report && !error && !researching && !busy && !refreshing && !syncError
    && !startConfirmed && !recovery.failure && forwardRecovery && serverStep && !Boolean(hasPageChoices(serverStep))
    && serverStep.key !== "work.running" && !["working", "queued"].includes(serverStep.runState || "")
    && Boolean(nextStepButton(serverStep, reopenPath)) ? serverStep : null;
  const unknownNextTest = !hasCustomers && finishedReport && report && researchUnknownNextTest(reportState, report.summary?.evidence_dimensions, qualifiedFindings.length);
  const [detail] = useDetailLevel();
  useEffect(() => { if (detail === "detailed" && reportDetails.current) reportDetails.current.open = true; }, [detail, report?.id]);
  const evidenceLink = (id: string) => { const source = evidence(id); const url = source && sourceUrl(source.url);
    return url ? <a key={id} href={url} target="_blank" rel="noopener noreferrer">{source.title || "Source"}</a> : <span key={id}>{source?.title || "Source unavailable"}</span>; };

  async function saveUsefulness(ratings: PriorArtRatings, note: string): Promise<boolean> {
    const current = () => reopenScopeCurrent() && active.current === ventureId && generation.current === reopenVersion;
    if (!current() || !reopenOwner || !currentReport || !report?.summary?.prior_art || pending.current
      || [ratings.useful, ratings.new, ratings.actionable].some(value => !Number.isInteger(value) || value < 1 || value > 5)
      || typeof note !== "string" || note.length > 500) return false;
    pending.current = true;
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/feedback`, {
        method: "POST", _csrfRetry: true, body: JSON.stringify({ source: "founder", text: JSON.stringify({
          kind: "research_prior_art_usefulness", dossier_id: report.id, ratings, note,
        }) }),
      });
      return current() && result.ok && typeof (result.data as { id?: unknown } | null)?.id === "string";
    } catch {
      return false;
    } finally {
      if (reopenScopeCurrent()) pending.current = false;
    }
  }

  return <AppShell title="Research" ventureId={ventureId} subtitle="Evidence, alternatives and what to test next">
    <div className={styles.page}>
      <header>
      <h1 className={styles.question}>{hasCustomers ? "Grow what's already working" : "Decide what is worth building"}</h1>
      <p className={styles.lede}>{hasCustomers ? "Build on your paying customers. Research helps compare alternatives and choose the next acquisition channel."
        : "See what we found about customers, alternatives and pricing, then choose what to learn next."}</p>
      <div className={styles.toolbar}>
        <Link href={`/v/${ventureId}/idea`}>Review idea and validation</Link>
        <button type="button" disabled={busy || refreshing} onClick={() => void load(report?.id)}>Refresh research</button>
      </div>
      </header>
      <div className={styles.layout}>
      <div className={styles.main}>
      {!researching && (busy || refreshing || exporting) && <RunningShimmer>{exporting ? "Preparing the report export…" : refreshing ? "Loading research…" : "Saving your request…"}</RunningShimmer>}
      <ErrorNotice error={error} source="research.error" className={styles.error} />
      <ErrorNotice error={syncError} source="research.syncError" className={styles.fail}>
        {syncError} <button type="button" disabled={refreshing} onClick={() => void load(report?.id)}>Try again</button>
      </ErrorNotice>
      {researching && <OneCardStack flow><OneCard title="Researching…" status="working"
        body={job.error || (researchStatus === "retry_wait" ? "Retrying in a moment" : jobId ? job.label
          : ["queued", "scheduled", "pending"].includes(researchStatus || "") ? "Queued" : "Research is running")}>
        <p>{local ? "You can leave Research while Supymem is open. If you quit Supymem, reopen it and return to Research to check your saved progress."
          : "You can leave Research and return to check your saved progress."}</p>
        {job.error && <button type="button" onClick={job.retry}>Try again</button>}
        {local && researchJobId && stopRequestedFor !== researchJobId
          && !(job.job?.id === researchJobId && job.job.wait?.reason === "cancel_requested")
          && <button type="button" disabled={busy || refreshing} onClick={() => void stopResearch(researchJobId)}>Stop research</button>}
        <ResearchProgress progress={(job.job?.id === jobId ? job.job.progress : null) ?? serverStep?.researchProgress} />
      </OneCard></OneCardStack>}
      {notice && (notice === usageLimitMessage() ? <ErrorNotice error={notice} source="research.limit" />
        : <p role="status">{notice} {setup && notice === setup.text && <Link href={setup.href}>{setup.label}</Link>}</p>)}
      {!researching && recovery.failure?.kind === "research.deep" && <Link href={`/v/${ventureId}/tasks`}>Skip for now</Link>}
      {ready && !error && !researching && reports.length === 0 && <section className={styles.empty}>
        <h2>No research report yet</h2><p>{researchEmptyMessage(recovery)}</p>
        {!noReportStep && !jobId && !syncError && recovery.state === "confirmed" && !recovery.personal && recovery.revisionId &&
          <button type="button" disabled={busy || refreshing} onClick={() => void startResearch()}>{recovery.failure ? "Review a new research attempt" : "Review deep research request"}</button>}
      </section>}
      {noReportStep && <section className={styles.nextActions} aria-label="Next step">
        <h2>{noReportStep.title}</h2><p>{noReportStep.detail}</p>
        <NextStepButton step={noReportStep} className="primary" />
      </section>}
      {!researching && recovery.failure && !(reportState === "failed" && report?.job_id === recovery.failure.id) && <ErrorNotice as="section" className={styles.fail}
        error={jobFailure(recovery.failure)} source="research.saved_job_failed" intent="research_run">
        <h2>{failedCopy!.title}</h2><p>{failedCopy!.body}</p>
        {recovery.failure.kind === "research.deep" && recovery.state === "confirmed" && !recovery.personal && recovery.revisionId
          && !jobId && !syncError && !startConfirmed &&
          <button type="button" data-usage-control="research_run_again" disabled={busy || refreshing}
            onClick={() => void startResearch()}>Run research again</button>}
        <Link href={`/v/${ventureId}/tasks`}>Review saved work</Link>
      </ErrorNotice>}
      {report?.summary?.assumed_questions && <section className={styles.section}>
        <p>Some questions were skipped or used assumptions. Answer them to update the review.</p>
        <button type="button" data-usage-control="research_questions_reopen" disabled={busy || refreshing || !!jobId || !!syncError} onClick={() => void reopenQuestions()}>Answer the questions now</button>
      </section>}
      {report && !researching && <>
        <section className={styles.nextActions} aria-labelledby="research-next-title">
          <h2 id="research-next-title" className={styles.srOnly}>Research summary</h2>
          <OneCardStack flow><Reveal><OneCard step={report.kind === "validation" ? "Initial validation" : "Deep research"}
            title={searchNotice ? searchNotice.state === "no_matches" ? "No search matches" : "Search did not finish" : reportState === "outdated" ? "Research is outdated" : reportState === "failed" ? "Research didn't finish" : hasCustomers && finishedReport ? "Grow what's already working" : finishedReport ? "What we found" : "Your next step"}
            body={searchNotice?.next_step || (finishedReport && !hasCustomers && !summaryFindings.length && !contradictions.length && !populatedDimensions.length ? founderReport?.empty_message || "Your next move: share a relevant link or ask potential customers about their current workaround." : !finishedReport ? reportState === "outdated" ? "This report covers an earlier idea. Run research again for your current idea."
              : plainJob({ kind: "research.deep", status: "failed", error_code: report.summary?.error_code }).body : undefined)}
            status={finishedReport ? "idle" : "error"}>
            {searchNotice?.settings_href && <Link href={searchNotice.settings_href}>Keys and connections</Link>}
            {showFindings && <>
            <div className={styles.stats}>
              <Stat value={verdict ? verdict.source_count : qualifiedFindings.length} label={`source${(verdict ? verdict.source_count : qualifiedFindings.length) === 1 ? "" : "s"}`} />
            </div>
            {summaryFindings.length > 0 && <ul>{summaryFindings.map(renderFinding)}</ul>}
            {contradictions.length > 0 && <><h3>Market insights</h3><ul>{contradictions.map(renderFinding)}</ul></>}
            <h3>Your opening</h3>
            <p>{founderReport?.opening || "Your best opening: start with one reachable user group and one recurring task, positioned around that single useful result. Use a focused landing page to test this angle."}</p>
            </>}
          </OneCard></Reveal></OneCardStack>
          <div className={styles.actionGrid}>
            {searchNotice && !showFindings ? <button className="primary" type="button" disabled={busy || refreshing || !!syncError || startConfirmed}
                onClick={() => void (finishedReport ? rerun("all") : startResearch())}>Run research again</button>
              : usableStep && allUnknownSummary ? <Link className="primary" data-usage-control="research_customer_interviews" href={`/gtm?venture=${encodeURIComponent(ventureId)}`}>Talk to 5 potential customers</Link>
              : !finishedReport && report.kind === "deep" && recovery.state === "confirmed" && !recovery.personal && recovery.revisionId
              ? <button className="primary" type="button" disabled={busy || refreshing || !!syncError || startConfirmed}
                  onClick={() => void startResearch()}>Run research again</button>
              : nextStep?.pageChoices ? null
              : nextStep && <Link className="primary" href={nextStep.href} data-usage-control="research_next_step">{nextStep.label} →</Link>}
            {hasCustomers && nextStep?.label === "Find customers" && (usableStep?.has_website || usableStep?.key === "site.audit") &&
              <Link href={`/v/${ventureId}/site`} data-usage-control="research_next_step">Audit your website</Link>}
          </div>
          {showFindings && nextStep?.pageChoices && <div className={styles.actionGrid}>
            <PageChoices ventureId={ventureId} disabled={busy || refreshing || !!syncError} />
          </div>}
          <details className={styles.moreActions}>
            <summary>Other research options</summary>
            <div className={styles.actionGrid}>
            {report.kind === "deep" && reportState !== "outdated" ? <>
              {(["competitors", "pricing", "demand"] as const).map((phase) =>
                <button key={phase} type="button" disabled={busy || refreshing || !!jobId || !!syncError}
                  onClick={() => void rerun(phase)}>{phaseAction[phase]}</button>)}
            </> : report.kind === "validation" ? <>
              <a href="#competitor-analysis">Review competitor evidence</a>
              <a href="#pricing-evidence">Review pricing</a>
              <a href="#demand-evidence">Review customer-demand evidence</a>
            </> : null}
            <a href="#case-studies">Explore relevant case studies</a>
            <Link href={`/v/${ventureId}/idea`}>Refine this idea</Link>
            <Link href="/new">Start a different venture</Link>
            </div>
          </details>
        </section>
        <details key={report.id} ref={reportDetails} className={styles.fullReport}>
          <summary>Read the full research report · {verdict ? verdict.source_count : qualifiedFindings.length} source{(verdict ? verdict.source_count : qualifiedFindings.length) === 1 ? "" : "s"}</summary>
        {unknownNextTest && (!searchNotice || showFindings) && <section className={styles.section}>
          <h2>Try this with potential customers</h2>
          <p><strong>Hypothesis:</strong> {unknownNextTest.hypothesis}</p>
          <p><strong>Try:</strong> {unknownNextTest.action}</p>
          <p><strong>Decision rule:</strong> {unknownNextTest.decision}</p>
        </section>}
        <section className={styles.section}>
          <h2>{report.kind === "validation" ? "Initial validation" : "Deep research"}{report.summary?.stopped_early && " · partial"}</h2>
          <p>Research from {when(report.created_at)}. Findings can change; check the linked sources before adopting a service or library.</p>
          {report.summary?.review_next_step && <p>{report.summary.review_next_step} <Link href="/settings/local#providers-title">Open Your AI provider</Link></p>}
          {(report.summary?.stopped_early || report.summary?.usage?.has_unsettled) && !researching && <div>
            <p>Research stopped before the review finished. We kept {qualifiedFindings.length} saved sources and the findings already reviewed. {report.summary?.usage?.has_unsettled ? "Check your saved progress before you start again." : qualifiedFindings.length ? "Finish reviewing these sources, or run research again." : "Run research again to find sources."}</p>
            {report.summary?.usage?.has_unsettled ? <button type="button" disabled={refreshing} onClick={() => void load(report.id)}>Check saved run</button> : qualifiedFindings.length > 0 ? <button type="button" disabled={busy || !!jobId || !!syncError} onClick={() => void rerun("review")}>Finish the review</button> : <button type="button" disabled={busy || !!jobId || !!syncError} onClick={() => void startResearch()}>Run research again</button>}
            <Link href={`/v/${ventureId}/tasks`}>Skip for now</Link>
          </div>}
          <div className={styles.toolbar}>
            {report.kind === "deep" && finishedReport && <button type="button" data-usage-control="research_rerun" disabled={busy || !!jobId || !!syncError} onClick={() => void rerun("all")}>Run research again</button>}
            {report.kind === "deep" && reportState !== "outdated" && qualifiedFindings.length > 0 && !report.summary?.usage?.has_unsettled && <button type="button" data-usage-control="research_source_review_start" disabled={busy || !!jobId || !!syncError} onClick={() => void rerun("review")}>Review saved sources</button>}
            <button type="button" disabled={busy || exporting || refreshing} onClick={() => {
              setExporting(true);
              const name = ventureName.replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "").slice(0, 80) || "venture";
              const date = /^\d{4}-\d{2}-\d{2}/.exec(report.created_at || "")?.[0];
              void downloadFile(`/api/ventures/${encodeURIComponent(ventureId)}/research/dossiers/${encodeURIComponent(report.id)}/export`, `${name}-research${date ? `-${date}` : ""}.md`)
                .then((ok) => setNotice(ok ? "Export requested. Check the download or save prompt." : "The report export failed. Refresh and try again."))
                .catch(() => setNotice("The report export failed. Check your connection and try again."))
                .finally(() => setExporting(false));
            }}>Export report</button>
          </div>
        </section>
        {populatedDimensions.length > 0 && <section className={styles.section} aria-labelledby="evidence-dimensions-title">
          <h2 id="evidence-dimensions-title">Findings by topic</h2>
          <div className={styles.dimensionGrid}>
            {populatedDimensions.map((key) => {
              const support = dimensionFindings(key, "support"), challenge = dimensionFindings(key, "challenge");
              return <article key={key} className={styles.dimension}>
                <h3>{dimensionLabels[key]}</h3>
                {support.length > 0 && <ul aria-label="Findings">{support.map(renderFinding)}</ul>}
                {challenge.length > 0 && <><h4>Market insights</h4><ul>{challenge.map(renderFinding)}</ul></>}
              </article>;
            })}
          </div>
        </section>}
        {(review?.observations || []).some(observation => displayResearchText(observation.quote) && sourceUrl(evidence(observation.source_id)?.url || null)) && <section className={styles.section}>
          <h2>Quotes from the sources</h2>
          {review!.observations!.filter(observation => displayResearchText(observation.quote) && sourceUrl(evidence(observation.source_id)?.url || null)).map((observation, index) => <blockquote key={index}><p>{observation.quote}</p>{evidenceLink(observation.source_id)}</blockquote>)}
        </section>}
        {report.summary?.validation && <section className={styles.section}>
          <h2>Your idea and assumptions</h2>
          {(report.summary.validation.findings || []).filter(finding => displayResearchText(finding.assessment)).map((finding, index) => <div key={index}>
            <h3>{label(finding.method)}</h3><p>{finding.assessment}</p>
            <ul>{finding.source_ids.map((id) => <li key={id}>{evidenceLink(id)}</li>)}</ul>
          </div>)}
          {(report.summary.validation.proposed_tests || []).slice(0, 3).map((test, index) => <div key={index}>
            <h3>{test.hypothesis}</h3><p>{test.action}</p><p>Success measure: {test.success_measure}</p>
          </div>)}
          <Link href={`/v/${ventureId}/idea`}>Accept findings or change the idea</Link>
        </section>}
        {serviceChecks.length > 0 && <section id="pricing-evidence" className={styles.section}>
          <h2>Service costs and requirements</h2>
          {serviceChecks.map((check, index) => <div key={index}><h3>{evidenceLink(check.source_id)}</h3>
            {check.display_pricing && <p>{check.display_pricing.product} · {check.display_pricing.price} · {check.display_pricing.billing_period} {evidenceLink(check.source_id)}</p>}
            {displayResearchText(check.limits_quote) && <p>Limits: {check.limits_quote}</p>}
            {displayResearchText(check.credentials_quote) && <p>Credentials: {check.credentials_quote}</p>}
          </div>)}
        </section>}
        {report.kind === "deep" && competitorChecks.length > 0 && <section id="competitor-evidence" className={styles.section}>
          <h2>Competitor details from saved excerpts</h2>
          {competitorChecks.map((check) => <div key={check.source_id} className={styles.competitorCard}>
            <h3>{evidenceLink(check.source_id)}</h3>
            <CompetitorQuote label="Product" quote={check.product_quote} />
            <CompetitorQuote label="Target customer" quote={check.customer_quote} />
            <CompetitorQuote label="Positioning" quote={check.positioning_quote} />
            {check.display_pricing && <p>{check.display_pricing.product} · {check.display_pricing.price} · {check.display_pricing.billing_period} {evidenceLink(check.source_id)}</p>}
          </div>)}
        </section>}
        </details>
      </>}
      </div>
      {(reports.length > 0 || (report && !researching)) && <aside className={styles.sidebar} aria-label="Research sources and history">
        {reports.length > 0 && <section className={styles.section}>
          <h2>Research history</h2>
          <label className={styles.history}>Saved report
            <select value={report?.id || ""} disabled={busy} onChange={(event) => void load(event.target.value)}>
              {reports.map((item) => <option key={item.id} value={item.id}>{item.kind === "validation" ? "Initial validation" : "Deep research"} · {when(item.created_at)}{item.summary?.stopped_early ? " · partial" : item.state !== "partial" && item.state !== "complete" ? ` · ${label(item.state)}` : ""}</option>)}
            </select>
          </label>
        </section>}
        {report && !researching && <>
        {finishedReport && <PriorArt priorArt={report.summary?.prior_art} findings={findings} className={styles.section} />}
        {finishedReport && <StartupGraveyard startups={report.summary?.startup_graveyard} className={styles.section} />}
        {currentReport && report.summary?.prior_art && <PriorArtRating key={`${reopenOwner}:${reopenEpoch}:${report.id}`}
          className={styles.section} onSubmit={saveUsefulness} />}
        {researchTopics.map((topic) => {
          const rows = groupedFindings[topic].filter(finding => finding.display_pricing || displayResearchText(finding.summary));
          const anchor = topic === "Direct competitor evidence" ? "competitor-analysis" : topic === "Problem and demand" ? "demand-evidence" : undefined;
          if (!rows.length || topic === "Search leads to review") return null;
          return <section key={topic} id={anchor} className={styles.section}>
            <h2>{topic}</h2>
              <ul className={styles.list}>{rows.map((finding) => <li key={finding.id} className={styles.source}>
                <details><summary><strong>{finding.title}</strong><span className={styles.snippet}>{topic === "Pricing and buying" && finding.display_pricing ? `${finding.display_pricing.product} · ${finding.display_pricing.price} · ${finding.display_pricing.billing_period}` : conciseFinding(displayResearchText(finding.summary))}</span></summary>
                  {topic === "Pricing and buying" && finding.display_pricing ? <p>{finding.display_pricing.product} · {finding.display_pricing.price} · {finding.display_pricing.billing_period} <a href={sourceUrl(finding.url) || undefined}>Source</a></p> : displayResearchText(finding.summary) && <p>{finding.summary}</p>}
                  {finding.observed_at && <p className={styles.note}>Retrieved {when(finding.observed_at)}</p>}
                  {finding.license && <p>Repository license: {finding.license}. Compatibility still needs review.</p>}
                  {finding.founder_flag && <p className={styles.note}>Your assessment: {finding.founder_flag === "pin" ? "Pinned" : "Excluded"}</p>}
                  <div className={styles.toolbar}>
                    {finding.category !== "review_lead" && <button type="button" disabled={busy} onClick={() => void flag(finding, finding.founder_flag === "pin" ? null : "pin")}>{finding.founder_flag === "pin" ? "Unpin" : "Pin finding"}</button>}
                    <button type="button" disabled={busy} onClick={() => void flag(finding, finding.founder_flag === "exclude" ? null : "exclude")}>{finding.founder_flag === "exclude" ? "Restore finding" : "Exclude finding"}</button>
                  </div>
                </details>
                <SourceChip url={finding.evidence_kind === "sample" ? finding.url : sourceUrl(finding.url)} title={finding.title} />
              </li>)}</ul>
          </section>;
        })}
        <section id="case-studies" className={styles.section}><h2>Relevant case studies and playbooks</h2>
          <CaseReferences key={ventureId} ventureId={ventureId} />
        </section>
        </>}
      </aside>}
      </div>
    </div>
  </AppShell>;
}
