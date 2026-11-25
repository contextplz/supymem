"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { NextStepButton, NextStepStrip, useNextStep } from "@/components/layout/NextStepStrip";
import { InlineSpinner } from "@/components/ui/InlineSpinner";
import { api, ensureSession } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import { jobSucceeded, useDurableJob } from "@/hooks/useDurableJob";
import { jobFailure, reportJobFailure, restoreActiveJob } from "@/lib/durableJobs";
import { FounderResourceInventory, type ResourceSnapshot } from "@/components/planning/FounderResourceInventory";
import { PlanEditSection, PlanFindingNavigator } from "@/components/planning/PlanFindingNavigator";
import { ReferenceCard, type CaseReference, type PrecedentApplication } from "@/components/planning/CaseReferences";
import { USAGE_LIMIT_MESSAGE } from "@/lib/credits";
import { planningFailureExplanation, type PlanningUsage } from "@/lib/planningFailure";
import { objectiveLabel, objectivePageCopy } from "@/lib/planObjective";
import { executionNextAction } from "@/lib/executionNextAction";
import { draftLines as lines, preparePlanEdit, describeImage, type LandingExpectations } from "@/lib/planEditing";
import { editableFindingPath, findingInSection, groupPlanFindings, planReviewAction } from "@/lib/planReview";
import { useDetailLevel } from "@/lib/detailLevel";
import { milestoneStatus, oneLine, planProgress, shortTitle } from "@/lib/planTimeline";
import styles from "./plan.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Task = { key: string; id?: string; title: string; description: string; capability_id: string; implementation: string;
  state?: string;
  landing_expectations?: LandingExpectations | null; source_ids: string[]; resource_ids: string[]; acceptance: string[]; scope?: string; reason?: string;
  founder_action: { title: string; steps: string[]; verification: string } | null };
type Milestone = { key: string; id?: string; title: string; description: string; kind: string; included: boolean;
  public_site_evidence?: { source: string; id: string; url: string; observed_at?: string | null } | null;
  depends_on: string[]; acceptance: string[]; tasks: Task[]; scope?: string; state?: string; estimate_credits?: number | null };
type Proposal = { summary: string; goal_first_paid_user: string; target_days: number | null; assumptions: string[];
  exclusions: string[]; evidence_gaps: string[]; milestones: Milestone[]; precedent_applications?: PrecedentApplication[] };
type Plan = { id: string; state: string; version?: number; edit_version: number; review_token: string; generation_state: string;
  provisional?: boolean; stale_dependencies: boolean; reviewable: boolean; newer_plan_id?: string | null; proposal?: Proposal | null; milestones?: Milestone[]; limitations?: string[];
  estimate?: { total_upfront_credits?: number | null; monthly_run_credits?: number | null; gaps?: string[] };
  usage?: PlanningUsage; sources?: { id: string; title: string; url?: string | null }[];
  review_findings?: { code: string; path: string; message: string }[];
  conversion_observation?: { submission_verified?: boolean; verified_at?: string | null; total?: number };
  precedents?: { items?: CaseReference[] } };
type Quote = { provider: string; model: string; payer: string; ceiling_micro: number; sufficient_now: boolean;
  available_micro: number | null; execution_costs_included: boolean };
const human = (value: string) => value.replace(/_/g, " ");
const canReviewPublicSite = (task: Task) => {
  const text = [task.title, task.description, ...(task.acceptance || []), ...(task.founder_action?.steps || []), task.founder_action?.verification || ""].join(" ").toLowerCase();
  return !!task.id && ["public", "url", "waitlist"].every(term => text.includes(term));
};

export default function PlanPage() {
  const ventureId = String(useParams().ventureId || "");
  const serverStep = useNextStep(ventureId);
  const active = useRef(ventureId); active.current = ventureId;
  const generation = useRef(0);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [history, setHistory] = useState<{ id: string; version: number; state: string }[]>([]);
  const [resources, setResources] = useState<ResourceSnapshot | null>(null);
  const planningWithAssumptions = resources?.execution_budget?.status === "local" || !!Object.keys(serverStep?.journey_overrides?.skipped || {}).length;
  const [request, setRequest] = useState("");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [quoteBody, setQuoteBody] = useState<Record<string, string>>({});
  const [edit, setEdit] = useState<Proposal | null>(null);
  const [editNavigationNote, setEditNavigationNote] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [resourceDirty, setResourceDirty] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [syncError, setSyncError] = useState("");
  const job = useDurableJob(jobId, finished => {
    setJobId(null); setMessage(jobSucceeded(finished) ? "Your plan is ready for review." : jobFailure(finished)); void load();
  });
  const blocked = busy || !!jobId || !!syncError;

  async function preparePublicSiteReview(task: Task) {
    if (!task.id || blocked) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await api(`/api/ventures/${ventureId}/plan-tasks/${encodeURIComponent(task.id)}/public-site-review`, { method: "POST" });
      if (!result.ok) { setError(apiFailure(result, "preparing this background verification")); return; }
      const path = (result.data as { review_path?: unknown }).review_path;
      if (typeof path !== "string" || !path.startsWith(`/v/${ventureId}/device-work-orders/`)) {
        setError("The saved Mac review link could not be verified."); return;
      }
      window.location.assign(path);
    } catch { setError("Could not confirm the saved Mac review. Refresh before trying again."); }
    finally { setBusy(false); }
  }

  async function load(selected?: string) {
    const token = ++generation.current;
    const current = () => active.current === ventureId && generation.current === token;
    await ensureSession(); if (!current()) return;
    const [list, inventory, restored] = await Promise.all([
      api(`/api/ventures/${ventureId}/plans`), api(`/api/ventures/${ventureId}/resources`),
      restoreActiveJob(ventureId, ["plan.compile"], undefined, true, (job) => { if (current()) reportJobFailure(job); }),
    ]);
    if (!current()) return;
    setJobId(restored.id); setSyncError(restored.error); setError(""); setQuote(null); setEdit(null);
    if (restored.lastFailure) setMessage(jobFailure(restored.lastFailure));
    if (inventory.ok && (inventory.data as ResourceSnapshot)?.inventory) {
      setResources(inventory.data as ResourceSnapshot); setResourceDirty(false);
    } else { setResources(null); setError(apiFailure(inventory, "loading founder resources")); }
    if (!list.ok) { setError(apiFailure(list, "loading plans")); setReady(true); return; }
    const items = (list.data as { items?: typeof history }).items;
    if (!Array.isArray(items)) { setError("The plan history could not be read. Refresh to try again."); setReady(true); return; }
    setHistory(items);
    const latest = items.find(item => item.id === selected) || items[0];
    if (!latest) { setPlan(null); setReady(true); return; }
    const detail = await api(`/api/ventures/${ventureId}/plans/${latest.id}`);
    if (!current()) return;
    if (detail.ok) setPlan(detail.data as Plan);
    else { setPlan(null); setError(apiFailure(detail, "opening this plan")); }
    setReady(true);
  }
  // Deep links like #plan-resources point at content that renders after the async load; the browser's own anchor scroll has already run by then.
  useEffect(() => {
    if (resources && window.location.hash) document.getElementById(window.location.hash.slice(1))?.scrollIntoView();
  }, [resources]);
  useEffect(() => {
    setPlan(null); setResources(null); setHistory([]); setQuote(null); setEdit(null); setReady(false); setError("");
    setMessage(""); setJobId(null); setSyncError(""); setRequest(""); setBusy(false); setResourceDirty(false);
    void load();
    return () => { generation.current++; active.current = ""; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ventureId]);

  async function estimate(revise: boolean) {
    if (blocked || resourceDirty || (!resources?.inventory.complete && !planningWithAssumptions)) return;
    setBusy(true); setQuote(null); setMessage("");
    const body: Record<string, string> = revise && plan ? { base_plan_id: plan.id, instruction: request.trim() } : {};
    const result = await api(`/api/ventures/${ventureId}/plans/estimate-generation`, { method: "POST", body: JSON.stringify(body) });
    if (active.current !== ventureId) return;
    setBusy(false);
    if (!result.ok) { setMessage(apiFailure(result, "estimating plan generation")); return; }
    setQuote(result.data as Quote); setQuoteBody(body);
    if ((result.data as Quote).sufficient_now) await generate(result.data as Quote, body);
  }
  async function generate(checked = quote, body = quoteBody) {
    if (!checked?.sufficient_now || blocked || resourceDirty || active.current !== ventureId) return;
    setBusy(true);
    const result = await api(`/api/ventures/${ventureId}/plans/compile`, { method: "POST", body: JSON.stringify(body) });
    if (active.current !== ventureId) return;
    setBusy(false); setQuote(null);
    if (!result.ok) { setMessage(apiFailure(result, "generating your plan")); return; }
    const id = (result.data as { job_id?: string }).job_id;
    if (id) { setJobId(id); setRequest(""); setMessage("Your plan is queued. Keep the app open while it runs."); }
    else setMessage("The request did not return a job. Refresh its status before trying again.");
  }
  async function approve() {
    if (!plan || blocked || edit || resourceDirty) return;
    setBusy(true);
    const result = await api(`/api/ventures/${ventureId}/plans/${plan.id}/approve`, { method: "POST", body: JSON.stringify({ review_token: plan.review_token }) });
    if (active.current !== ventureId) return;
    setBusy(false);
    if (!result.ok) { setMessage(apiFailure(result, "approving this plan")); return; }
    setMessage("Plan approved. Execution requires its own capability and budget checks."); await load(plan.id);
  }
  async function saveEdit() {
    if (!plan || !edit || blocked || resourceDirty) return;
    setBusy(true);
    const result = await api(`/api/ventures/${ventureId}/plans/${plan.id}`, { method: "PATCH", body: JSON.stringify({ edit_version: plan.edit_version, patch: preparePlanEdit(edit) }) });
    if (active.current !== ventureId) return;
    setBusy(false);
    if (!result.ok) { setMessage(apiFailure(result, "saving plan edits")); return; }
    setMessage("Plan edits saved. Review the updated milestones before approval."); await load(plan.id);
  }
  function editMilestone(index: number, patch: Partial<Milestone>) {
    setEdit(current => current ? { ...current, milestones: current.milestones.map((item, i) => i === index ? { ...item, ...patch } : item) } : current);
  }
  function editHandoff(milestoneIndex: number, taskIndex: number, patch: Partial<Task>) {
    setEdit(current => current ? { ...current, milestones: current.milestones.map((milestone, index) => index === milestoneIndex
      ? { ...milestone, tasks: milestone.tasks.map((task, position) => position === taskIndex && task.implementation === "founder_action" ? { ...task, ...patch } : task) }
      : milestone) } : current);
  }
  function editTask(milestoneIndex: number, taskIndex: number, patch: Partial<Task>) {
    setEdit(current => current ? { ...current, milestones: current.milestones.map((milestone, index) => index === milestoneIndex
      ? { ...milestone, tasks: milestone.tasks.map((task, position) => position === taskIndex ? { ...task, ...patch } : task) }
      : milestone) } : current);
  }
  function editLanding(milestoneIndex: number, taskIndex: number, landing_expectations: LandingExpectations) {
    setEdit(current => current ? { ...current, milestones: current.milestones.map((milestone, index) => index === milestoneIndex
      ? { ...milestone, tasks: milestone.tasks.map((task, position) => position === taskIndex && ["build.run", "coding.subscription_static"].includes(task.capability_id) && task.implementation === "new_code" ? { ...task, landing_expectations } : task) }
      : milestone) } : current);
  }
  function evidence(id: string) {
    const source = plan?.sources?.find(item => item.id === id);
    let url: URL | null = null;
    try { url = source?.url ? new URL(source.url) : null; } catch { /* Show the source title without a link. */ }
    return url && ["http:", "https:"].includes(url.protocol) ? <a href={url.href} target="_blank" rel="noopener noreferrer">{source?.title || "Evidence"}</a> : <span>{source?.title || "Stored evidence"}</span>;
  }
  const failureExplanation = planningFailureExplanation(plan);
  const objectiveCopy = objectivePageCopy(edit ?? plan?.proposal);
  const reviewAction = plan ? planReviewAction(plan) : null;
  const groupedFindings = groupPlanFindings(plan?.review_findings || []);
  const findingPaths = plan?.review_findings?.map(item => item.path) || [];
  const hasFinding = (section: string) => findingPaths.some(path => findingInSection(path, section));
  function jumpToFinding(path: string) {
    const fields = [...document.querySelectorAll<HTMLElement>("#plan-edit [data-plan-path]")];
    const targetPath = editableFindingPath(path, fields.map(field => field.dataset.planPath || ""));
    const target = fields.find(field => field.dataset.planPath === targetPath);
    if (!target) {
      setEditNavigationNote(`The flagged location ${path} has no direct text field. Review its milestone or request a plan revision.`);
      return;
    }
    setEditNavigationNote("");
    for (let parent = target.parentElement; parent; parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) parent.open = true;
    }
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.querySelector<HTMLElement>("input, textarea")?.focus({ preventScroll: true });
  }
  const [level] = useDetailLevel();
  const detailed = level === "detailed";
  const nextPlanTask = plan?.milestones?.filter(milestone => milestone.included)
    .flatMap(milestone => milestone.tasks)
    .find(task => task.state !== "done") || null;
  const nextAction = executionNextAction({
    ventureId, ready, busy, jobId, error, syncError, resourceDirty,
    resourcesComplete: !!resources?.inventory.complete || planningWithAssumptions, quoteReady: !!quote,
    hasProposal: !!plan?.proposal, staleDependencies: !!plan?.stale_dependencies,
    findingCount: plan?.review_findings?.length || 0, planState: plan?.state || null,
    planReviewable: !!plan?.reviewable, generationState: plan?.generation_state || null,
    newerPlanId: plan?.newer_plan_id, editing: !!edit,
    nextTask: nextPlanTask ? { id: nextPlanTask.id, title: nextPlanTask.title,
      capabilityId: nextPlanTask.capability_id, implementation: nextPlanTask.implementation,
      scope: nextPlanTask.scope, reason: nextPlanTask.reason } : null,
  });
  return <AppShell title="Execution" ventureId={ventureId} subtitle={objectiveCopy.subtitle}>
    <div className={styles.page}>
      <h1>{objectiveCopy.heading}</h1>
      <div className={styles.topControls}><Link href={`/v/${ventureId}/idea`}>Idea</Link><Link href={`/v/${ventureId}/research`}>Research</Link>
        <button type="button" data-usage-control="plan_refresh" disabled={busy || !!edit || resourceDirty} onClick={() => void load(plan?.id)}>Refresh plan</button></div>
      <section className={styles.guide} aria-label="Next execution step">
        <span className={styles.guideIcon} aria-hidden="true">{!ready || busy || jobId ? <InlineSpinner /> : "↗"}</span>
        <div><h2>{nextAction.label}</h2><p>{nextAction.detail}</p></div>
        {!ready || busy || jobId ? <span className={styles.nextDisabled} aria-disabled="true">{nextAction.label}</span>
          : syncError || error ? <button type="button" className={styles.nextButton} onClick={() => void load(plan?.id)}>Refresh saved status</button>
          : "intent" in nextAction && nextAction.intent === "edit" && plan?.proposal
            ? <button type="button" className={styles.nextButton} data-usage-control="plan_edit" onClick={() => { setEdit(structuredClone(plan.proposal!)); setQuote(null); requestAnimationFrame(() => document.getElementById("plan-edit")?.scrollIntoView()); }}>{nextAction.label}</button>
          : "intent" in nextAction && nextAction.intent === "latest" && plan?.newer_plan_id
            ? <button type="button" className={styles.nextButton} onClick={() => void load(plan.newer_plan_id!)}>{nextAction.label}</button>
          : <Link className={styles.nextButton} href={nextAction.href}>{nextAction.label}</Link>}
      </section>
      {(!ready || busy || jobId || syncError || error) && <section id="execution-progress" className={styles.progress} role="status" aria-live="polite">
        {(!ready || busy || jobId) && <InlineSpinner />}<span>{jobId ? job.error || job.label || "Plan generation is running in the background." : syncError || error || (busy ? "Updating your plan…" : "Loading plan and resources…")}</span>
        {(jobId || syncError || error) && <button type="button" disabled={busy} onClick={() => jobId && job.error ? job.retry() : void load(plan?.id)}>{job.error || syncError || error ? "Try again" : "Refresh saved status"}</button>}
      </section>}
      <ErrorNotice error={error} source="plan.error" /><ErrorNotice error={syncError} source="plan.syncError" />
      {message && <p role="status">{message}</p>}
      {plan?.proposal && !edit && <section id="plan-review" className={`${styles.card} ${styles.reviewCard}`} style={{ scrollMarginTop: 160 }} aria-labelledby="plan-review-title">
        <p className={styles.meta}>Plan version {plan.version} · {human(plan.state)}</p>
        <h2 id="plan-review-title">Review this execution plan</h2>
        <p className={styles.shortSummary}>{plan.provisional && <strong>Provisional plan: </strong>}{plan.proposal.summary}</p>
        <p className={styles.meta}>{plan.milestones?.length ?? 0} milestones · {plan.proposal.target_days ? `Initial target ${plan.proposal.target_days} days` : "Timeline needs evidence"}</p>
        <ErrorNotice error={failureExplanation} source="plan.failureExplanation" />
        {plan.newer_plan_id && <ErrorNotice error="A newer plan is available. Review it before deciding on this version." source="plan.a_newer_plan_is" />}
        {plan.stale_dependencies && <ErrorNotice error="Your idea, research or resources changed. Generate a fresh plan before approving it." source="plan.your_idea_research_or" />}
        {!!plan.review_findings?.length && <ErrorNotice error="Correct these claims before approval" as="div" id="plan-findings" className={styles.findings} source="plan.review_findings">
          <h3>Correct these claims before approval</h3>
          <p>The saved draft has {groupedFindings.length} distinct {groupedFindings.length === 1 ? "issue" : "issues"} ({plan.review_findings.length} flagged {plan.review_findings.length === 1 ? "reference" : "references"}). Correct them without another generation, then review the whole plan.</p>
          <ul>{groupedFindings.map(item => <li key={`${item.code}-${item.message}`}>{item.message}{item.paths.length > 1 && <details><summary>{item.paths.length} locations in this plan</summary><ul>{item.paths.map(path => <li key={path}>{path}</li>)}</ul></details>}</li>)}</ul>
        </ErrorNotice>}
        {(() => {
          const gaps = [...plan.proposal.evidence_gaps, ...(plan.estimate?.gaps || []), ...(plan.limitations || [])];
          const first = (plan.milestones || []).find(m => m.included);
          const test = first?.acceptance?.[0] || first?.description;
          return <div className={styles.openQuestions}>
            <h3>Before you approve</h3>
            <ul>
              {gaps[0] && <li>Biggest risk: {gaps[0]}</li>}
              {first && <li>First milestone, {first.title}{test ? `, tests: ${test}` : ""}</li>}
            </ul>
            {!!plan.milestones?.length && <ol>{plan.milestones.map(m => <li key={m.key}>{m.title}{!m.included ? " (excluded)" : ""}</li>)}</ol>}
            {gaps.length > 1 && <details><summary>More details</summary><ul>{gaps.slice(1).map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
          </div>;
        })()}
        {plan.conversion_observation && <p className={styles.meta}>Waitlist snapshot: {plan.conversion_observation.submission_verified && plan.conversion_observation.verified_at
          ? `submission and storage verified at ${plan.conversion_observation.verified_at}` : "submission verification was not current"}; {plan.conversion_observation.total ?? 0} real interest records. Recheck Website before publication.</p>}
        <p className={styles.reviewHint}>Full task details, assumptions and costs are below.</p>
        <div className={styles.actions}>
          {reviewAction === "review" && <button type="button" data-usage-control="plan_approve" disabled={blocked || resourceDirty || !plan.reviewable || plan.generation_state !== "completed" || plan.stale_dependencies || !!plan.review_findings?.length} onClick={() => void approve()}>Approve this plan</button>}
          {reviewAction === "revise" && !!plan.review_findings?.length && plan.reviewable && !plan.newer_plan_id && !plan.stale_dependencies && plan.generation_state === "completed" && <button type="button" className={styles.primaryAction} data-usage-control="plan_edit" disabled={blocked || resourceDirty} onClick={() => { setEdit(structuredClone(plan.proposal!)); setQuote(null); }}>Edit plan manually</button>}
          {reviewAction === "revise" && plan.newer_plan_id && <button type="button" className={styles.primaryAction} disabled={blocked || resourceDirty} onClick={() => void load(plan.newer_plan_id!)}>Review the latest plan</button>}
          {reviewAction === "revise" && !plan.newer_plan_id && (plan.stale_dependencies || !plan.reviewable || plan.generation_state !== "completed") && <a className={styles.primaryAction} href="#plan-create">Create a current plan</a>}
          {reviewAction === "approved" && <span role="status">This plan is approved. Follow the next step above.</span>}
        </div>
        <details className={styles.revisionOptions}>
          <summary>Change or regenerate this plan</summary>
          <label id="plan-revision" className={styles.field}>Change the plan<textarea maxLength={4000} value={request} placeholder="For example, test the waitlist before building onboarding" disabled={blocked} onChange={event => { setRequest(event.target.value); setQuote(null); }} /></label>
          <div className={styles.actions}>
            <button type="button" disabled={blocked || resourceDirty || !request.trim()} onClick={() => void estimate(true)}>Estimate this revision</button>
            {!(reviewAction === "revise" && !!plan.review_findings?.length) && <button type="button" data-usage-control="plan_edit" disabled={blocked || resourceDirty || !plan.reviewable} onClick={() => { setEdit(structuredClone(plan.proposal!)); setQuote(null); }}>Edit plan manually</button>}
          </div>
        </details>
        <p className={styles.meta}>Approval records this reviewed plan. Execution has separate capability and budget checks.</p>
      </section>}
      {resources && <div id="plan-resources" className={styles.card} style={{ scrollMarginTop: 160 }}><FounderResourceInventory key={ventureId} ventureId={ventureId} snapshot={resources} disabled={blocked || !!edit}
        onDirty={() => { setResourceDirty(true); setQuote(null); }} onSaved={() => void load(plan?.id)} /></div>}
      {resourceDirty && <p>Save your resource changes before estimating, editing or approving a plan.</p>}
      {ready && !error && !blocked && !resourceDirty && !plan && serverStep?.researchReportReady
        && !["working", "queued", "paused"].includes(serverStep.runState || "") &&
        <NextStepButton step={{ ...serverStep, key: "plan.generate", chat_action: "plan.generate", payload: null,
          href: `/v/${ventureId}/plan`, cta_label: "Write my plan" }} className={styles.primaryAction} />}
      {ready && !jobId && <details id="plan-create" className={styles.card} open={!plan?.proposal || !!plan.stale_dependencies}>
        <summary>{plan ? "Create a newer plan" : "Create your execution plan"}</summary>
        <p>{planningWithAssumptions
          ? "Make a plan from saved context. Missing research and resources are labelled as gaps to check before execution."
          : "Confirm your idea, review its research, and save your resource inventory first."}</p>
        <button type="button" data-usage-control="plan_estimate" disabled={blocked || resourceDirty || !!edit || (!resources?.inventory.complete && !planningWithAssumptions)} onClick={() => void estimate(false)}>Generate plan</button>
      </details>}
      {quote && !quoteBody.base_plan_id && <div id="plan-estimate"><PlanQuote quote={quote} blocked={blocked} resourceDirty={resourceDirty}
        onGenerate={() => void generate()} onCancel={() => setQuote(null)} /></div>}
      {history.length > 1 && <label className={styles.field}>Plan history<select data-usage-control="plan_history_select" value={plan?.id || ""} disabled={blocked || !!edit || resourceDirty} onChange={event => void load(event.target.value)}>
        {history.map(item => <option key={item.id} value={item.id}>Version {item.version} · {human(item.state)}</option>)}
      </select></label>}
      {plan && <>
        {plan.proposal && !edit && <details className={styles.card}><summary>Full goal, assumptions and evidence gaps</summary><h2>{objectiveLabel(plan.proposal)}</h2><p>{plan.proposal.goal_first_paid_user}</p>
          <h3>How we’ll get there</h3><p>{plan.provisional && <strong>Provisional plan: </strong>}{plan.proposal.summary}</p>
          <p>{plan.proposal.target_days ? `Initial target: ${plan.proposal.target_days} days; review as evidence changes.` : "Timeline needs further evidence."}</p>
          {(["assumptions", "exclusions", "evidence_gaps"] as const).map(key => plan.proposal![key].length > 0 && <div key={key}><h3>{human(key)}</h3><ul>{plan.proposal![key].map((item, index) => <li key={index}>{item}</li>)}</ul></div>)}
        </details>}
      {!edit && !!plan?.proposal?.precedent_applications?.length && <details className={styles.card}>
        <summary>Case studies and experiment rationale</summary>
        <h2>Why these experiments are worth considering</h2>
        <p>Review the historical comparison and confirm the stated assumptions before approving the plan. Use the plan revision request to correct any mismatch.</p>
        {plan.proposal.precedent_applications.map(application => {
          const reference = plan.precedents?.items?.find(item => item.id === application.record_id);
          return reference ? <ReferenceCard key={application.record_id} item={reference} application={application} />
            : <p key={application.record_id}>This plan reference is unavailable. Regenerate the plan before relying on it.</p>;
        })}
      </details>}
        {edit && <section id="plan-edit" className={styles.card} style={{ scrollMarginTop: 160 }}><h2>Edit the plan</h2>
          <p>Open a section to review all fields. Sections with findings start open; your unsaved changes stay in place while you navigate.</p>
          <PlanFindingNavigator findings={plan.review_findings || []} note={editNavigationNote} className={styles.editFindings} onJump={jumpToFinding} />
          <details className={styles.editSection} open={findingPaths.some(path => !path.startsWith("proposal.milestones[") && !path.startsWith("proposal.precedent_applications["))}>
            <summary>Goal, assumptions and evidence gaps{findingPaths.some(path => !path.startsWith("proposal.milestones[") && !path.startsWith("proposal.precedent_applications[")) ? " · Needs review" : ""}</summary>
          <label data-plan-path="proposal.summary" className={`${styles.field} ${hasFinding("proposal.summary") ? styles.flaggedField : ""}`}>Summary<textarea value={edit.summary} onChange={event => setEdit({ ...edit, summary: event.target.value })} /></label>
          <label data-plan-path="proposal.goal_first_paid_user" className={`${styles.field} ${hasFinding("proposal.goal_first_paid_user") ? styles.flaggedField : ""}`}>{objectiveLabel(edit)}<textarea value={edit.goal_first_paid_user} onChange={event => setEdit({ ...edit, goal_first_paid_user: event.target.value })} /></label>
          <label data-plan-path="proposal.target_days" className={styles.field}>Initial target in days<input type="number" min={1} max={365} value={edit.target_days ?? ""} onChange={event => setEdit({ ...edit, target_days: event.target.value ? Number(event.target.value) : null })} /></label>
          {(["assumptions", "exclusions", "evidence_gaps"] as const).map(key => <label key={key} data-plan-path={`proposal.${key}`} className={`${styles.field} ${hasFinding(`proposal.${key}`) ? styles.flaggedField : ""}`}>{human(key)}, one per line<textarea disabled={busy} value={edit[key].join("\n")} onChange={event => setEdit({ ...edit, [key]: lines(event.target.value) })} /></label>)}
          </details>
          {edit.precedent_applications?.map((application, index) => <details key={application.record_id} className={styles.editSection} open={hasFinding(`proposal.precedent_applications[${index}]`)}><summary>Case interpretation: {plan.precedents?.items?.find(item => item.id === application.record_id)?.title || "Reference unavailable"}{hasFinding(`proposal.precedent_applications[${index}]`) ? " · Needs review" : ""}</summary><fieldset disabled={busy}>
            <p>The workbook source and citations stay fixed. You can correct how this plan applies the reference, or remove the analogy.</p>
            {(["proposed_application", "important_difference", "local_test", "stop_condition", "context_to_confirm"] as const).map(key => <label key={key} data-plan-path={`proposal.precedent_applications[${index}].${key}`} className={`${styles.field} ${hasFinding(`proposal.precedent_applications[${index}].${key}`) ? styles.flaggedField : ""}`}>{human(key)}<textarea value={application[key]} onChange={event => setEdit({ ...edit, precedent_applications: edit.precedent_applications?.map((item, position) => position === index ? { ...item, [key]: event.target.value } : item) })} /></label>)}
            <button type="button" onClick={() => setEdit({ ...edit, precedent_applications: edit.precedent_applications?.filter((_, position) => position !== index) })}>Remove this analogy</button>
          </fieldset></details>)}
          {edit.milestones.map((milestone, index) => <PlanEditSection key={milestone.key} path={`proposal.milestones[${index}]`} title={`Milestone ${index + 1}: ${milestone.title}`} findingPaths={findingPaths} className={styles.editSection}><fieldset disabled={busy}>
            <label data-plan-path={`proposal.milestones[${index}].title`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].title`) ? styles.flaggedField : ""}`}>Title<input value={milestone.title} onChange={event => editMilestone(index, { title: event.target.value })} /></label>
            <label data-plan-path={`proposal.milestones[${index}].description`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].description`) ? styles.flaggedField : ""}`}>Description<textarea value={milestone.description} onChange={event => editMilestone(index, { description: event.target.value })} /></label>
            <label data-plan-path={`proposal.milestones[${index}].acceptance`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].acceptance`) ? styles.flaggedField : ""}`}>Acceptance criteria, one per line<textarea value={milestone.acceptance.join("\n")} onChange={event => editMilestone(index, { acceptance: lines(event.target.value) })} /></label>
            <label><input type="checkbox" checked={milestone.included} onChange={event => editMilestone(index, { included: event.target.checked })} /> Include this milestone</label>
            {milestone.tasks.map((task, taskIndex) => <PlanEditSection key={task.key} path={`proposal.milestones[${index}].tasks[${taskIndex}]`} title={`${task.implementation === "founder_action" ? "Founder handoff" : "Task"}: ${task.title}`} findingPaths={findingPaths} className={styles.editSection}><fieldset>
              <label data-plan-path={`proposal.milestones[${index}].tasks[${taskIndex}].title`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].tasks[${taskIndex}].title`) ? styles.flaggedField : ""}`}>Task title<input maxLength={200} value={task.title} onChange={event => editTask(index, taskIndex, { title: event.target.value })} /></label>
              <label data-plan-path={`proposal.milestones[${index}].tasks[${taskIndex}].description`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].tasks[${taskIndex}].description`) ? styles.flaggedField : ""}`}>Task description<textarea value={task.description} onChange={event => editTask(index, taskIndex, { description: event.target.value })} /></label>
              <label data-plan-path={`proposal.milestones[${index}].tasks[${taskIndex}].acceptance`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].tasks[${taskIndex}].acceptance`) ? styles.flaggedField : ""}`}>Task acceptance criteria, one per line<textarea value={task.acceptance.join("\n")} onChange={event => editTask(index, taskIndex, { acceptance: lines(event.target.value) })} /></label>
              {task.founder_action && <>
                <label data-plan-path={`proposal.milestones[${index}].tasks[${taskIndex}].founder_action.title`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].tasks[${taskIndex}].founder_action.title`) ? styles.flaggedField : ""}`}>Handoff title<input maxLength={200} value={task.founder_action.title} onChange={event => editHandoff(index, taskIndex, { founder_action: { ...task.founder_action!, title: event.target.value } })} /></label>
                <label data-plan-path={`proposal.milestones[${index}].tasks[${taskIndex}].founder_action.steps`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].tasks[${taskIndex}].founder_action.steps`) ? styles.flaggedField : ""}`}>Human steps, one per line<textarea value={task.founder_action.steps.join("\n")} onChange={event => editHandoff(index, taskIndex, { founder_action: { ...task.founder_action!, steps: lines(event.target.value) } })} /></label>
                <label data-plan-path={`proposal.milestones[${index}].tasks[${taskIndex}].founder_action.verification`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].tasks[${taskIndex}].founder_action.verification`) ? styles.flaggedField : ""}`}>Completion evidence<textarea value={task.founder_action.verification} onChange={event => editHandoff(index, taskIndex, { founder_action: { ...task.founder_action!, verification: event.target.value } })} /></label>
              </>}
            </fieldset></PlanEditSection>)}
            {milestone.tasks.map((task, taskIndex) => task.landing_expectations && ["build.run", "coding.subscription_static"].includes(task.capability_id) && task.implementation === "new_code" && <details key={`landing-${task.key}`} className={styles.editSection} open={hasFinding(`proposal.milestones[${index}].tasks[${taskIndex}].landing_expectations`)}><summary>Landing page: {task.title}{hasFinding(`proposal.milestones[${index}].tasks[${taskIndex}].landing_expectations`) ? " · Needs review" : ""}</summary><fieldset disabled={busy}>
              <p>Review visible copy separately from image descriptions. Saving requires fresh plan approval; previous build evidence stays unchanged.</p>
              {task.landing_expectations.contract.required_text.map((text, textIndex) => <div key={textIndex}>
                <label data-plan-path={`proposal.milestones[${index}].tasks[${taskIndex}].landing_expectations.contract.required_text[${textIndex}]`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].tasks[${taskIndex}].landing_expectations.contract.required_text[${textIndex}]`) ? styles.flaggedField : ""}`}>Visible copy {textIndex + 1}<textarea maxLength={500} value={text} onChange={event => {
                  const next = structuredClone(task.landing_expectations!); next.contract.required_text[textIndex] = event.target.value; editLanding(index, taskIndex, next);
                }} /></label>
                <button type="button" disabled={text.length < 12 || text.length > 160 || task.landing_expectations!.contract.required_text.length <= 1 || (task.landing_expectations!.contract.visuals?.length ?? 0) >= 6}
                  onClick={() => editLanding(index, taskIndex, describeImage(task.landing_expectations!, textIndex))}>Use as image description</button>
              </div>)}
              {task.landing_expectations.contract.visuals?.map((visual, visualIndex) => <label key={visual.id} data-plan-path={`proposal.milestones[${index}].tasks[${taskIndex}].landing_expectations.contract.visuals[${visualIndex}].name`} className={`${styles.field} ${hasFinding(`proposal.milestones[${index}].tasks[${taskIndex}].landing_expectations.contract.visuals[${visualIndex}].name`) ? styles.flaggedField : ""}`}>Image description {visualIndex + 1}<textarea minLength={12} maxLength={160} value={visual.name} onChange={event => {
                const next = structuredClone(task.landing_expectations!); next.contract.visuals![visualIndex].name = event.target.value; editLanding(index, taskIndex, next);
              }} /></label>)}
            </fieldset></details>)}
          </fieldset></PlanEditSection>)}
          <div className={`${styles.actions} ${styles.editActions}`}><button type="button" data-usage-control="plan_save_edit" disabled={blocked || resourceDirty} onClick={() => void saveEdit()}>Save plan edits</button><button type="button" data-usage-control="plan_cancel_edit" disabled={busy} onClick={() => setEdit(null)}>Cancel edits</button></div>
        </section>}
        {!edit && <span id="plan-milestones" />}
        {!edit && !!plan.milestones?.length && <div className={styles.timelineHead}>
          <div className={styles.bar} role="progressbar" aria-label="Plan progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={planProgress(plan.milestones)}><span style={{ width: `${planProgress(plan.milestones)}%` }} /></div>
          <span className={styles.meta}>{planProgress(plan.milestones)}% of tasks done</span>
        </div>}
        {!edit && <ol className={styles.timeline}>{(plan.milestones || []).map((milestone, index) => {
          const running = ["approved", "executing"].includes(plan.state);
          const status = milestoneStatus(milestone, nextPlanTask?.id);
          const current = running && status === "active";
          return <li key={milestone.key} className={`${styles.step} ${styles[status]} ${current ? styles.current : ""}`} style={{ animationDelay: `${Math.min(index, 8) * 70}ms` }}>
          <span className={styles.node} aria-hidden="true">{status === "done" ? "✓" : index + 1}</span>
          <details className={styles.mCard} open={current ? true : undefined}>
          <summary title={milestone.title}>
            <span className={styles.mTitle}>{shortTitle(milestone.title)}</span>
            <span className={styles.mSummary}>{oneLine(milestone.description)}</span>
            <span className={styles.chips}>
              {status === "done" && <span className={`${styles.pill} ${styles.pillOk}`}>Done</span>}
              {current && <span className={`${styles.pill} ${styles.pillActive}`}>In progress</span>}
              {!milestone.included && <span className={styles.pill}>Excluded</span>}
              <span className={styles.chip}>{milestone.tasks.length} {milestone.tasks.length === 1 ? "task" : "tasks"}</span>
            </span>
          </summary>
          {detailed && <p className={styles.full}>{milestone.title}: {milestone.description}</p>}
          {milestone.public_site_evidence && <p>Public site published: <a href={milestone.public_site_evidence.url} target="_blank" rel="noopener noreferrer">{milestone.public_site_evidence.url}</a>. Publication is observed; each task still needs its own acceptance.</p>}
          {detailed && <><p className={styles.meta}>Scope: {human(milestone.scope || "unverified")}{milestone.depends_on.length ? ` · Depends on ${milestone.depends_on.map(key => plan.milestones?.find(item => item.key === key)?.title || key).join(", ")}` : ""}</p>
          <h3>Acceptance criteria</h3><ul>{milestone.acceptance.map((item, i) => <li key={i}>{item}</li>)}</ul></>}
          <div className={styles.tasks}>{milestone.tasks.map(task => {
            const isNext = running && task.id === nextPlanTask?.id;
            return <details key={task.key} id={task.id ? `plan-task-${task.id}` : undefined}
            className={`${styles.task} ${isNext ? styles.nextTask : ""}`} open={task.id === nextPlanTask?.id && (task.implementation === "founder_action" || task.scope === "out") ? true : undefined}
            style={{ scrollMarginTop: 160 }}><summary>
              <span className={styles.dot} aria-hidden="true" />
              <span className={styles.taskTitle}>{task.title}</span>
              <span className={`${styles.pill} ${task.state === "done" ? styles.pillOk : ""}`}>{task.state ? human(task.state) : "state unavailable"}</span>
              <span className={styles.chip}>{task.scope === "in" ? "Supymem capability available" : task.scope === "out" ? "Execution unavailable" : "Founder action or verification required"}</span>
            </summary>
            {detailed && <><p>{task.description}</p><p>{task.reason}</p><p>{task.implementation}</p>
            <ul>{task.acceptance.map((item, i) => <li key={i}>{item}</li>)}</ul>
            {task.source_ids.length > 0 && <ul>{task.source_ids.map(id => <li key={id}>{evidence(id)}</li>)}</ul>}</>}
            {!detailed && <p className={styles.meta}>{oneLine(task.description)}</p>}
            {task.founder_action && <div><h4>You need to: {task.founder_action.title}</h4><ol>{task.founder_action.steps.map((step, i) => <li key={i}>{step}</li>)}</ol><p>Ready when: {task.founder_action.verification}</p></div>}
            {canReviewPublicSite(task) && <button type="button" disabled={blocked} onClick={() => void preparePublicSiteReview(task)}>Prepare Supymem background verification</button>}
          </details>;
          })}</div>
        </details></li>;
        })}</ol>}
        {plan.proposal && <details className={styles.card}><summary>Limitations</summary><h2>Limitations</h2>
          <ul>{[...(plan.estimate?.gaps || []), ...(plan.limitations || [])].map((item, i) => <li key={i}>{item}</li>)}</ul>
        </details>}
      </>}
      {quote && quoteBody.base_plan_id && <div id="plan-estimate"><PlanQuote quote={quote} revision blocked={blocked}
        resourceDirty={resourceDirty} onGenerate={() => void generate()} onCancel={() => setQuote(null)} /></div>}
      <NextStepStrip ventureId={ventureId} compact />
    </div>
  </AppShell>;
}

function PlanQuote({ quote, revision = false, blocked, resourceDirty, onGenerate, onCancel }: {
  quote: Quote; revision?: boolean; blocked: boolean; resourceDirty: boolean; onGenerate: () => void; onCancel: () => void;
}) {
  return <section className={styles.card}><h2>{revision ? "Plan revision" : "Plan generation"}</h2>
    <p>This covers planning only. Building and hosting are planned separately.</p>
    {!quote.sufficient_now && <ErrorNotice error={USAGE_LIMIT_MESSAGE} source="plan.your_available_credits_cannot" />}
    <div className={styles.actions}><button type="button" data-usage-control="plan_generate" disabled={blocked || resourceDirty || !quote.sufficient_now} onClick={onGenerate}>Generate plan</button><button type="button" data-usage-control="plan_cancel_generate" disabled={blocked} onClick={onCancel}>Cancel</button></div>
  </section>;
}
