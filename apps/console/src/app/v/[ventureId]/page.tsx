"use client";

import { GodModeRequestCard } from "@/components/settings/GodModeRequestCard";
import { useContext, useEffect, useRef, useState } from "react";
import { FounderUIContext } from "@/lib/founderUI";
import Link from "@/components/chat/ChatLink";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { InlineSpinner } from "@/components/ui/InlineSpinner";
import { api, ensureSession, prefetchApi, verifiedSessionSubject, verifiedSessionEpoch } from "@/lib/client";
import { cacheGet, cachePut } from "@/lib/apiCache";
import { apiFailure } from "@/lib/apiFailure";
import { companyJourneyStep, companyWorkState, acceptedArtifactIsPublished, type PublicationReceipt } from "@/lib/companyJourney";
import { hasPageChoices, selectNextStep } from "@/lib/nextStep";
import { JourneyChoices, NextStepButton, useNextStep } from "@/components/layout/NextStepStrip";
import { OneCard, OneCardStack } from "@/components/primitives/OneCard";
import { founderProductContext } from "@/lib/gtmDiagnosisApi";
import styles from "@/components/cockpit/cockpit.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { BetaPathStrip } from "@/components/ventures/BetaPathStrip";
import { BETA_PATH, betaStageIndex } from "@/lib/betaPath";
import { FounderStageCard } from "@/components/ventures/FounderStageCard";
import type { ServerNextStep } from "@/lib/nextStep";
import { safeSiteUrl } from "@/lib/founderAssetsApi";
import { GmailReconnectBanner } from "@/components/connections/GmailReconnectBanner";

type Company = { name: string; raw_idea: string; workspace_id: string; stage: string; entry_mode?: string | null; entry_goal?: string | null; intake_path?: string | null };
type RecordRow = { id: string; state: string; name?: string; kind?: string; status?: string; version?: number; project_intent?: "startup" | "personal" };
type Snapshot = { company: Company; ideas: RecordRow[] | null; plans: RecordRow[] | null; products: RecordRow[] | null; jobs: RecordRow[] | null;
  hasAcceptedBuild: boolean; websitePublished: boolean; websiteUrl?: string; serverNextStep?: unknown; hasProductBuildAttempt: boolean; hasAcceptedProductBuild: boolean };

const cachedSnapshot = (ventureId: string) =>
  (cacheGet(verifiedSessionSubject(), `/api/ventures/${ventureId}/cockpit-snapshot`)?.data as Snapshot | undefined) ?? null;

function GodModeDisclosure({ ventureId }: { ventureId: string }) {
  const [open, setOpen] = useState(false);
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>God mode access</summary>
    {open && <GodModeRequestCard ventureId={ventureId} className={styles.pulseCell} />}
  </details>;
}

export default function VenturePage() {
  const founderUI = useContext(FounderUIContext);
  const local = useLocalMode();
  const ventureId = String(useParams().ventureId || "");
  const current = useRef(ventureId); current.current = ventureId;
  const generation = useRef(0);
  const [controlsOpen, setControlsOpen] = useState(false);
  const loadOverview = !local || controlsOpen;
  const scopedStep = useNextStep(local ? undefined : ventureId);
  const [receipt, setReceipt] = useState<{ venture: string; owner: string | null; epoch: number; snapshot: Snapshot } | null>(() => {
    const snapshot = cachedSnapshot(ventureId);
    return snapshot ? { venture: ventureId, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch(), snapshot } : null;
  });
  const snapshot = receipt && receipt.venture === ventureId && receipt.owner === verifiedSessionSubject()
    && receipt.epoch === verifiedSessionEpoch() ? receipt.snapshot : null;
  const setSnapshot = (value: Snapshot | null) => setReceipt(value ? {
    venture: ventureId, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch(), snapshot: value,
  } : null);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [copyNotice, setCopyNotice] = useState("");
  const godModeScope = `${ventureId}:${verifiedSessionSubject()}:${verifiedSessionEpoch()}`;
  async function load() {
    const token = ++generation.current;
    setBusy(true);
    let owner = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const active = () => current.current === ventureId && generation.current === token
      && owner === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
    try {
    await ensureSession();
    if (current.current !== ventureId || generation.current !== token) return;
    owner = verifiedSessionSubject(); epoch = verifiedSessionEpoch();
    // A previously built snapshot paints immediately; the fresh one replaces it below.
    const snapshotPath = `/api/ventures/${ventureId}/cockpit-snapshot`;
    const scope = verifiedSessionSubject();
    const known = cacheGet(scope, snapshotPath);
    if (known) setSnapshot(known.data as Snapshot);
    // Independent reads start together; only the jobs list needs the workspace id.
    const base = ["idea/sessions", "plans", "apps"].map(path => `/api/ventures/${ventureId}/${path}`);
    const listsPending = Promise.all(base.map(path => api(path)));
    const dashboardPending = api(`/api/ventures/${ventureId}/dashboard`);
    const result = await api(`/api/ventures/${ventureId}`);
    if (!active()) return;
    if (!result.ok || !(result.data as Company)?.workspace_id) {
      if (!known) setErrors([apiFailure(result, "loading this company")]); setBusy(false); return;
    }
    const company = result.data as Company;
    const jobsPending = api(`/api/workspaces/${company.workspace_id}/jobs?${new URLSearchParams({ venture_id: ventureId, active_only: "true", limit: "10" })}`);
    const [responses, dashboard] = await Promise.all([
      Promise.all([listsPending, jobsPending]).then(([lists, jobs]) => [...lists, jobs]),
      dashboardPending,
    ]);
    if (!active()) return;
    const failed: string[] = [], labels = ["idea history", "plans", "products", "active work"];
    const lists = responses.map((response, index): RecordRow[] | null => {
      const items = (response.data as { items?: RecordRow[] })?.items;
      if (!response.ok || !Array.isArray(items)) {
        failed.push(response.ok ? `The saved ${labels[index]} could not be read.` : apiFailure(response, `loading ${labels[index]}`)); return null;
      }
      return items;
    });
    const products = lists[2] || [];
    const [buildResponses, deploymentResponses] = await Promise.all([
      Promise.all(products.map(product => api(`/api/apps/${encodeURIComponent(product.id)}/builds`))),
      Promise.all(products.map(product => api(`/api/apps/${encodeURIComponent(product.id)}/deployments`))),
    ]);
    if (!active()) return;
    let hasAcceptedBuild = false, acceptedPublished = false;
    let hasProductBuildAttempt = false, hasAcceptedProductBuild = false;
    buildResponses.forEach((response, index) => {
      const builds = (response.data as { items?: Array<{ artifact_id?: string; artifact_profile?: string; state?: string; completion_status?: string; qa_state?: string; review?: { status?: string } }> } | null)?.items;
      if (!response.ok || !Array.isArray(builds)) return;
      hasProductBuildAttempt ||= builds.some(build => build.artifact_profile === "founder_product_v1");
      hasAcceptedProductBuild ||= builds.some(build => build.artifact_profile === "founder_product_v1" && build.state === "succeeded"
        && build.completion_status === "closed" && build.qa_state === "passed" && build.review?.status === "accepted");
      const accepted = builds.find(build => build.state === "succeeded" && build.completion_status === "closed" && build.qa_state === "passed" && build.review?.status === "accepted");
      if (!accepted) return;
      hasAcceptedBuild = true;
      const deployments = deploymentResponses[index];
      const receipts = (deployments.data as { items?: PublicationReceipt[] } | null)?.items;
      if (deployments.ok && Array.isArray(receipts) && receipts.some(receipt => acceptedArtifactIsPublished(products[index].id, accepted.artifact_id, receipt))) acceptedPublished = true;
      if (!deployments.ok) failed.push(apiFailure(deployments, "loading publication status"));
    });
    const website = (dashboard.data as { tiles?: { website?: { url?: string; published?: boolean } } } | null)?.tiles?.website;
    const built: Snapshot = { company, serverNextStep: (dashboard.data as { next_step?: unknown } | null)?.next_step, ideas: lists[0], plans: lists[1], products: lists[2], jobs: lists[3],
      hasAcceptedBuild, hasProductBuildAttempt, hasAcceptedProductBuild,
      websitePublished: hasAcceptedBuild ? acceptedPublished : Boolean(dashboard.ok && website?.published && website.url),
      websiteUrl: dashboard.ok ? website?.url : undefined };
    setSnapshot(built);
    if (!failed.length) cachePut(scope, snapshotPath, built);
    setErrors(failed); setBusy(false);
    // Warm the pages the founder most likely opens next (Idea, Now, Work).
    prefetchApi([`/api/ventures/${ventureId}`, `/api/ventures/${ventureId}/tasks`]);
    } catch {
      if (current.current === ventureId && generation.current === token) setErrors(["Your company could not be loaded. Choose Refresh below to retry."]);
    } finally {
      if (current.current === ventureId && generation.current === token) setBusy(false);
    }
  }
  useEffect(() => {
    current.current = ventureId;
    const reset = () => { generation.current++; setErrors([]); setCopyNotice(""); setSnapshot(cachedSnapshot(ventureId)); setBusy(false); if (loadOverview) void load(); };
    reset(); window.addEventListener("supymem-session-changed", reset);
    return () => { generation.current++; current.current = ""; window.removeEventListener("supymem-session-changed", reset); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ventureId, loadOverview]);
  const idea = snapshot?.ideas?.[0], plan = snapshot?.plans?.[0];
  const preparingClarification = Boolean(snapshot?.jobs?.some(job => job.kind === "idea.clarify_turn" && ["pending", "scheduled", "leased", "retry_wait", "running"].includes(job.status || "")));
  const resolvedStep = snapshot ? companyJourneyStep({
    entryGoal: snapshot.company.entry_goal,
    hasActiveJobs: Boolean(snapshot.jobs?.length),
    activeWorkState: companyWorkState(snapshot.jobs),
    preparingClarification,
    ideaConfirmed: idea?.state === "confirmed",
    ideaState: idea?.state,
    projectIntent: idea?.project_intent,
    hasPlan: Boolean(plan),
    planState: plan?.state,
    productCount: snapshot.products?.length || 0,
    hasAcceptedBuild: snapshot.hasAcceptedBuild,
    websitePublished: snapshot.websitePublished,
    hasProductBuildAttempt: snapshot.hasProductBuildAttempt,
    hasAcceptedProductBuild: snapshot.hasAcceptedProductBuild,
  }) : null;
  const serverStep = scopedStep ?? (snapshot?.serverNextStep as ServerNextStep | null | undefined);
  const nextStep = snapshot ? selectNextStep(serverStep, resolvedStep,
    resolvedStep?.path === "gtm" ? `/gtm?venture=${encodeURIComponent(ventureId)}` : `/v/${ventureId}/${resolvedStep?.path}`) : null;
  const key = serverStep?.key;
  const stage = betaStageIndex(key, snapshot?.websitePublished, serverStep?.founder_stage?.stage)
    ?? (idea?.state === "confirmed" ? 1 : 0);
  const cta = nextStep && <>{scopedStep ? <><NextStepButton step={scopedStep} className="primary" />{!hasPageChoices(scopedStep) && <JourneyChoices step={scopedStep} />}</> : <Link className="primary" href={nextStep.href}>{nextStep.cta}</Link>}{preparingClarification && <Link href={`/new?venture=${encodeURIComponent(ventureId)}`}>Open conversation</Link>}</>;
  const intakePath = snapshot?.company.intake_path;
  const gtm = intakePath ? ["gtm", "landing_gtm"].includes(intakePath) : snapshot?.company.entry_goal === "gtm";
  const focusedPath = key === "venture.context" || (["research", "personal_build", "repo_build"].includes(intakePath || "")
    || (!intakePath && idea?.project_intent === "personal")) && !Object.keys(serverStep?.journey_overrides?.skipped || {}).length
      && !serverStep?.journey_overrides?.target;
  // Skip-to-GTM ventures go straight to Outreach; the page steps stay locked and never ask for a build.
  const target = serverStep?.journey_overrides?.target;
  const at = target ? ({ clarification: 0, validation: 0, research: 5, landing_page: 1, find_customers: 4 }[serverStep?.skip_step || target] ?? stage) : gtm ? 4 : stage;
  const ready = Boolean(snapshot && nextStep);
  const liveUrl = safeSiteUrl(serverStep?.site_url || (snapshot?.websitePublished ? snapshot.websiteUrl : "") || "");
  const livePage = Boolean(snapshot?.websitePublished || liveUrl);
  const summary = snapshot?.company.entry_mode === "product" ? founderProductContext(snapshot.company.raw_idea) : snapshot?.company.raw_idea;
  // Open on the step the founder is on, not the first finished one.
  useEffect(() => {
    if (ready && typeof document.getElementById === "function") document.getElementById(focusedPath ? "step-current" : `step-${BETA_PATH[at]?.key}`)?.scrollIntoView?.({ block: "start" });
  }, [ready, at, focusedPath]);
  const companyControls = snapshot && <><GodModeDisclosure key={godModeScope} ventureId={ventureId} />{verifiedSessionSubject() && <FounderStageCard key={`founder-stage:${godModeScope}`} owner={{actorId: verifiedSessionSubject()!, workspaceId: snapshot.company.workspace_id, ventureId}} stageRecord={serverStep?.founder_stage} onSaved={() => { setSnapshot(null); void load(); }} />}<button type="button" disabled={busy} onClick={() => void load()}>Refresh</button></>;
  if (local || founderUI === "home") return <AppShell officeHome title={snapshot?.company.name || "Company"} ventureId={ventureId}>
    {errors.map((error, index) => <ErrorNotice key={index} error={error} source="cockpit.load" />)}
    {errors.length > 0 && <button type="button" disabled={busy} onClick={() => void load()}>Refresh</button>}
    {loadOverview && !snapshot && !errors.length && <p role="status"><InlineSpinner /> Opening the company.</p>}
    {(local || snapshot) && <details onToggle={local ? event => setControlsOpen(event.currentTarget.open) : undefined}><summary>Company controls</summary>{loadOverview && companyControls}</details>}
  </AppShell>;
  return <AppShell title={snapshot?.company.name || "Company"} ventureId={ventureId} stage={snapshot?.company.stage}>
    <div className={styles.cockpit}>
      {snapshot && <GmailReconnectBanner workspaceId={snapshot.company.workspace_id} ventureId={ventureId} />}
      {errors.map((error, index) => <ErrorNotice key={index} error={error} source="cockpit.load" />)}
      {!snapshot && !errors.length && <OneCard title="Loading…" status="working" />}
      {!busy && (!snapshot || !nextStep) && <OneCard title="Your next step could not load" status="error"
        body="Your saved work is still here. Try loading it again."
        actions={<button type="button" onClick={() => void load()}>Try again</button>} />}
      {snapshot && verifiedSessionSubject() && <FounderStageCard key={`founder-stage:${godModeScope}`} owner={{ actorId: verifiedSessionSubject()!, workspaceId: snapshot.company.workspace_id, ventureId }} stageRecord={serverStep?.founder_stage} onSaved={() => { setSnapshot(null); void load(); }} />}
      {snapshot && nextStep && focusedPath && <OneCard id="step-current" title={nextStep.title}
        body={key === "venture.context" ? `Your current phase: ${BETA_PATH[at]?.label}. Complete your startup context to continue. ${nextStep.detail}` : nextStep.detail} status="idle" actions={cta}
        details={<>
          <GodModeRequestCard ventureId={ventureId} className={styles.pulseCell} />
          <div className={styles.controlLinks}><Link href={`/v/${ventureId}/tasks`}>Tasks</Link><Link href={`/v/${ventureId}/settings`}>Company settings</Link>
            <button type="button" className="ghost sm" disabled={busy} onClick={() => void load()}>{busy ? "Refreshing…" : "Refresh"}</button></div>
        </>} />}
      {snapshot && nextStep && !focusedPath && <OneCardStack>
        {BETA_PATH.map((step, index) => {
          const skipped = serverStep?.journey_overrides?.skipped || {};
          const skippedStep = step.key === "idea" ? (skipped.clarification ? "clarification" : skipped.validation ? "validation" : null)
            : ["page", "publish", "waitlist"].includes(step.key) ? (skipped.landing_page ? "landing_page" : null)
            : step.key === "outreach" ? (skipped.find_customers ? "find_customers" : null) : skipped.research ? "research" : null;
          const done = index < at && !(gtm && !livePage && index > 0 && index < 4)
            && (step.key !== "idea" || idea?.state === "confirmed");
          const isCurrent = index === at;
          const publishedPage = step.key === "waitlist" && livePage;
          const optional = step.key === "research";
          return <OneCard key={step.key} id={`step-${step.key}`} step={`${index + 1} of ${BETA_PATH.length}`}
            title={publishedPage ? "Your landing page is live" : isCurrent ? nextStep.title || step.title : step.label}
            body={skippedStep ? "Skipped by you. Missing checks lower confidence; you can restore this step." : publishedPage ? "Visit the landing page, copy its link, or continue to Find customers. Your waitlist does not block outreach." : isCurrent ? (nextStep.detail && nextStep.detail.length <= 140 ? nextStep.detail : step.what) : done ? "Done." : step.what}
            status={skippedStep || isCurrent || optional ? "idle" : publishedPage || done ? "done" : scopedStep ? "idle" : "locked"}
            actions={skippedStep ? scopedStep && <>{isCurrent && cta}<JourneyChoices step={scopedStep} restore={skippedStep} />
              {step.key === "idea" && skipped.clarification && skipped.validation && <JourneyChoices step={scopedStep} restore="validation" />}</> : publishedPage ? <>
              <Link className="primary" href={`/gtm?venture=${encodeURIComponent(ventureId)}`}>Find customers</Link>
              {liveUrl && <><a href={liveUrl} target="_blank" rel="noopener noreferrer">Visit landing page</a>
                <button type="button" onClick={async () => {
                  const owner = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
                  try { await navigator.clipboard.writeText(liveUrl); if (current.current === ventureId && owner === verifiedSessionSubject() && epoch === verifiedSessionEpoch()) setCopyNotice("Link copied."); }
                  catch { if (current.current === ventureId && owner === verifiedSessionSubject() && epoch === verifiedSessionEpoch()) setCopyNotice("Could not copy the link. Open the landing page to copy its address."); }
                }}>Copy link</button></>}
              {copyNotice && <span role="status">{copyNotice}</span>}
            </> : isCurrent ? cta : optional ? <Link href={step.href(ventureId)}>Open research</Link> : scopedStep && !done ?
              <><Link href={step.href(ventureId)}>Open {step.label}</Link><JourneyChoices step={{ ...scopedStep,
                skip_step: step.key === "idea" ? "clarification" : step.key === "outreach" ? "find_customers" : "landing_page" }} /></> : undefined}
            details={isCurrent ? <>
              <BetaPathStrip current={at} />
              {(summary || "No idea summary is saved; you can continue with drafts and leave missing facts unknown.").split(/\n/).map((line, index) => <p key={index}>{line.replace(/^Audience:\s*/i, "For: ")}</p>)}
              <p>{gtm ? "Next: review your go-to-market context." : snapshot.ideas === null ? "Idea state is temporarily unavailable." : idea?.state === "confirmed" ? "Idea confirmed." : "Ready for clarification."}</p>
              <div className={styles.controlLinks}><Link href={`/v/${ventureId}/tasks`}>Tasks</Link><Link href={`/gtm?venture=${ventureId}`}>Go to market</Link><Link href={`/v/${ventureId}/settings`}>Company settings</Link>
                <button type="button" className="ghost sm" data-usage-control="work_refresh" disabled={busy} onClick={() => void load()}>{busy && <InlineSpinner />}{busy ? "Refreshing…" : "Refresh"}</button></div>
            </> : undefined} />;
        })}
      </OneCardStack>}
      <GodModeDisclosure key={`god-mode:${godModeScope}`} ventureId={ventureId} />
    </div>
  </AppShell>;
}
