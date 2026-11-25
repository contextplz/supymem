"use client";

import { acceptedArtifactIsPublished } from "@/lib/companyJourney";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { LocalSiteControls } from "./LocalSiteControls";
import { useParams, useSearchParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { NextStepStrip } from "@/components/layout/NextStepStrip";
import { OneCard, OneCardStack } from "@/components/primitives/OneCard";
import { ProvenanceChip } from "@/components/primitives/ProvenanceChip";
import { UnpublishSite } from "@/components/site/UnpublishSite";
import { PublishConceptPage } from "@/components/site/PublishConceptPage";
import { BuildPageCard } from "@/components/site/BuildPageCard";
import { BuildEvidence } from "@/components/builds/BuildEvidence";
import { VentureWaitlist } from "@/components/site/VentureWaitlist";
import type { DashboardPayload, WebsiteTileData } from "@/lib/api-types";
import { api, authenticatedFetch, ensureSession, verifiedSessionSubject, verifiedSessionEpoch } from "@/lib/client";
import { createGodModeRequestFence, sameGodModeScope, type GodModeScope } from "@/lib/godModeRequestScope";
import { ventureSitePath, scopedSiteMatches, firstlookPreviewPath } from "@/lib/firstlookPreview";
import {
  checkedReport,
  completedProofMatches,
  type QaReceipt,
  type ReviewBuild,
} from "@/lib/buildReview";
import { LandingPreviewFrame } from "@/components/cockpit/LandingPreviewFrame";
import { displayName, isLandingBuildPause, isReachableLandingUrl } from "@/lib/format";
import { errorCodeCopy } from "@/lib/errorCopy";
import { resourceReadFailure } from "@/lib/apiFailure";
import { revisionBuildHref, revisionLoadFailure, revisionRequestBody, revisionSaveFailure } from "@/lib/siteRevision";
import {
  websiteEmptyState,
  websiteHeaderStatus,
  publicationFailureMessage,
  publishedPageAuditTarget,
  websiteSurfaceState,
} from "@/lib/sitePresentation";
import styles from "./site.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { ExistingSiteCard } from "@/components/site/ExistingSiteCard";
import { checkedSiteImport, siteImportFromSearch, type ImportedSite } from "@/lib/founderAssetsApi";

const QUESTION = "What does my landing page look like, and where is it?";
const MAX_VARIANTS = 5;

type SiteMeta = {
  id?: string;
  workspace_id?: string;
  venture_id?: string | null;
  version?: number;
  stage?: string;
  template?: string;
  archetype?: string;
  path?: string;
  url?: string | null;
  screenshot_path?: string | null;
  sha256?: string;
  fallback?: boolean;
  published?: boolean;
  cost_usd?: number;
  created_at?: string;
  copy?: { headline?: string };
  brandkit?: unknown;
  interest_count?: number;
  interest?: { total?: number; by_version?: Record<string, number> };
  variant_index?: number;
};

type AcceptedBuildPreview = {
  artifactId: string;
  appId: string;
  buildId: string;
  desktopUrl: string;
  mobileUrl: string;
  passed: number;
};

type StaticDeployment = {
  id: string;
  app_id: string;
  build_artifact_id?: string | null;
  state: string;
  version: number;
  url?: string | null;
  error?: string | null;
  target?: string; live_at?: string | null;
};

type PublishedCopyAudit = {
  deployment_id: string;
  state: "clear" | "needs_review";
  findings: { path: string; categories: string[] }[];
};

type BuildWithReview = ReviewBuild & { app_id?: string };

type SiteRevisionStatus = {
  baseline: { deployment_id: string; source_sha256: string };
  requests: { id: string; state: string; deployment_id: string; instruction: string; created_at: string;
    source_sha256?: string; build_id?: string; source_artifact_id?: string }[];
};

type SiteRevisionQuote = {
  quote_id: string;
  input_digest: string;
  expires_at: string;
  ready: boolean;
  reasons: string[];
  available_micro: number | null;
  ceiling_micro: number | null;
  mode?: string;
};

type WebsiteReadScope = { current: () => boolean; signal?: AbortSignal };
function requireWebsiteScope(scope: WebsiteReadScope): void {
  if (!scope.current()) throw new Error("The Website request belongs to an earlier session.");
}
async function websiteApi(scope: WebsiteReadScope, path: string) {
  requireWebsiteScope(scope);
  const result = await api(path, { signal: scope.signal });
  requireWebsiteScope(scope);
  return result;
}
async function websiteFetch(scope: WebsiteReadScope, path: string) {
  requireWebsiteScope(scope);
  const result = await authenticatedFetch(path, { signal: scope.signal });
  requireWebsiteScope(scope);
  return result;
}

async function evidenceUrl(
  scope: WebsiteReadScope,
  base: string,
  item: { id: string; media_type: string; sha256: string; size_bytes: number },
): Promise<string> {
  if (!/^(?:desktop|mobile)-[0-9]+\.png$/.test(item.id) || item.media_type !== "image/png") {
    throw new Error("The accepted build has unsupported preview evidence.");
  }
  const response = await websiteFetch(scope, `${base}/evidence/${encodeURIComponent(item.id)}`);
  if (!response.ok) throw new Error(await resourceReadFailure(response, "Accepted build preview"));
  const bytes = await response.arrayBuffer();
  requireWebsiteScope(scope);
  const hash = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
  requireWebsiteScope(scope);
  if (bytes.byteLength !== item.size_bytes || bytes.byteLength > 10 * 1024 * 1024 || hash !== item.sha256) {
    throw new Error("The accepted build preview does not match its independent QA receipt.");
  }
  return URL.createObjectURL(new Blob([bytes], { type: "image/png" }));
}

async function liveLandingDeployment(ventureId: string, scope: WebsiteReadScope): Promise<StaticDeployment | null> {
  const result = await websiteApi(scope, `/api/ventures/${ventureId}/apps`);
  const apps = (result.data as { items?: { id?: string; kind?: string }[] } | null)?.items;
  if (!result.ok || !Array.isArray(apps)) return null;
  const deployments = await Promise.all(apps.filter(app => app.id && app.kind !== "local_harness").map(async app => {
    const response = await websiteApi(scope, `/api/apps/${encodeURIComponent(app.id!)}/deployments`);
    const items = (response.data as { items?: StaticDeployment[] } | null)?.items;
    return response.ok && Array.isArray(items) ? items.filter(item => item.app_id === app.id
      && item.target === "supymem_static" && item.state === "live" && item.build_artifact_id
      && typeof item.url === "string" && isReachableLandingUrl(item.url) && /^https:\/\//i.test(item.url)) : [];
  }));
  return deployments.flat().sort((first, second) => (second.live_at || "").localeCompare(first.live_at || "") || second.version - first.version)[0] || null;
}

async function acceptedBuildPreview(ventureId: string, scope: WebsiteReadScope, live?: StaticDeployment | null): Promise<AcceptedBuildPreview | null> {
  const appsResult = await websiteApi(scope, `/api/ventures/${ventureId}/apps`);
  const apps = (appsResult.data as { items?: { id?: string }[] } | null)?.items;
  if (!appsResult.ok || !Array.isArray(apps)) return null;
  for (const app of apps) {
    if (!app.id || live && app.id !== live.app_id) continue;
    const buildsResult = await websiteApi(scope, `/api/apps/${encodeURIComponent(app.id)}/builds`);
    const builds = (buildsResult.data as { items?: BuildWithReview[] } | null)?.items;
    if (!buildsResult.ok || !Array.isArray(builds)) continue;
    const build = builds.find(
      (item) =>
        item.runtime_version === 1 &&
        (!live || item.artifact_id === live.build_artifact_id) &&
        item.state === "succeeded" &&
        item.completion_status === "closed" &&
        item.qa_state === "passed" &&
        item.review?.status === "accepted" &&
        Boolean(item.qa_run_id) &&
        Boolean(item.artifact_id),
    );
    if (!build?.qa_run_id || !build.artifact_id) continue;
    const base = `/api/builds/${encodeURIComponent(build.id)}/qa/${encodeURIComponent(build.qa_run_id)}`;
    const qaResult = await websiteApi(scope, base);
    if (!qaResult.ok) throw new Error("The accepted build's QA receipt could not be loaded.");
    const qa = qaResult.data as QaReceipt;
    if (
      qa.id !== build.qa_run_id ||
      qa.source_artifact_id !== build.artifact_id ||
      !qa.verified_report ||
      !completedProofMatches(build, qa) ||
      qa.state !== "passed"
    ) {
      throw new Error("The accepted build is not bound to a verified QA result.");
    }
    const reportResponse = await websiteFetch(scope, `${base}/report`);
    if (!reportResponse.ok) throw new Error(await resourceReadFailure(reportResponse, "QA report"));
    const reportBytes = await reportResponse.arrayBuffer();
    requireWebsiteScope(scope);
    const report = await checkedReport(reportBytes, build, qa);
    requireWebsiteScope(scope);
    const desktop = report.evidence.find((item) => item.id.startsWith("desktop-"));
    const mobile = report.evidence.find((item) => item.id.startsWith("mobile-"));
    if (!desktop || !mobile) throw new Error("The accepted build has no desktop and mobile QA previews.");
    const desktopUrl = await evidenceUrl(scope, base, desktop);
    let mobileUrl: string | undefined;
    try { mobileUrl = await evidenceUrl(scope, base, mobile); requireWebsiteScope(scope); }
    catch (error) {
      URL.revokeObjectURL(desktopUrl);
      if (mobileUrl) URL.revokeObjectURL(mobileUrl);
      throw error;
    }
    return { artifactId: build.artifact_id, appId: app.id, buildId: build.id, desktopUrl, mobileUrl, passed: qa.passed || 0 };
  }
  return null;
}

/** A finished build whose QA review is still pending: the founder's next action is to accept it. */
async function buildAwaitingReview(ventureId: string, scope: WebsiteReadScope, live?: StaticDeployment | null): Promise<BuildWithReview | null> {
  const appsResult = await websiteApi(scope, `/api/ventures/${ventureId}/apps`);
  const apps = (appsResult.data as { items?: { id?: string }[] } | null)?.items;
  if (!appsResult.ok || !Array.isArray(apps)) return null;
  const requestedRun = new URLSearchParams(window.location.search).get("run");
  for (const app of apps) {
    if (!app.id || live && app.id !== live.app_id) continue;
    const buildsResult = await websiteApi(scope, `/api/apps/${encodeURIComponent(app.id)}/builds`);
    const builds = (buildsResult.data as { items?: BuildWithReview[] } | null)?.items;
    const build = Array.isArray(builds) ? builds.find((item) => item.runtime_version === 1 && item.artifact_profile === "static_landing_v1"
      && (live ? item.artifact_id === live.build_artifact_id : requestedRun ? item.id === requestedRun : item.landing_page?.qa_report_available && item.state === "failed"
        || item.state === "succeeded" && item.review?.status === "pending")) : null;
    if (build) {
      const result = await websiteApi(scope, `/api/builds/${encodeURIComponent(build.id)}`);
      const detail = result.data as BuildWithReview | null;
      if (result.ok && detail?.id === build.id && detail.app_id === app.id && detail.artifact_profile === "static_landing_v1"
          && (!live || detail.artifact_id === live.build_artifact_id)) return detail;
    }
  }
  return null;
}

async function auditPublishedWebsite(
  scope: WebsiteReadScope,
  ventureId: string,
  url: string,
  displayedDeployment?: { appId: string; deploymentId: string },
): Promise<PublishedCopyAudit | null> {
  if (displayedDeployment) {
    const audit = await websiteApi(scope, `/api/apps/${encodeURIComponent(displayedDeployment.appId)}/deployments/${encodeURIComponent(displayedDeployment.deploymentId)}/public-copy`);
    if (!audit.ok) throw new Error("Supymem could not check the published page’s copy right now.");
    return audit.data as PublishedCopyAudit;
  }
  if (!isReachableLandingUrl(url)) return null;
  const appsResult = await websiteApi(scope, `/api/ventures/${ventureId}/apps`);
  const apps = (appsResult.data as { items?: { id?: string }[] } | null)?.items;
  if (!appsResult.ok || !Array.isArray(apps)) return null;
  for (const app of apps) {
    if (!app.id) continue;
    const result = await websiteApi(scope, `/api/apps/${encodeURIComponent(app.id)}/deployments`);
    const items = (result.data as { items?: StaticDeployment[] } | null)?.items;
    if (!result.ok || !Array.isArray(items)) continue;
    const live = items.find((item) => item.state === "live" && item.url?.replace(/\/$/, "") === url.replace(/\/$/, ""));
    if (!live) continue;
    const audit = await websiteApi(scope, `/api/apps/${encodeURIComponent(app.id)}/deployments/${encodeURIComponent(live.id)}/public-copy`);
    if (!audit.ok) throw new Error("Supymem could not check the published page’s copy right now.");
    return audit.data as PublishedCopyAudit;
  }
  return null;
}

function errorMessage(data: unknown, fallback: string): string {
  if (!data || typeof data !== "object") return fallback;
  const known = errorCodeCopy(errorCode(data));
  if (known) return known;
  const root = data as {
    error?: { message?: string } | string;
    detail?: { error?: { message?: string }; message?: string } | string;
    message?: string;
  };
  if (typeof root.error === "object" && root.error?.message) return root.error.message;
  if (typeof root.error === "string" && root.error) return root.error;
  if (typeof root.detail === "object") {
    if (root.detail.error?.message) return root.detail.error.message;
    if (root.detail.message) return root.detail.message;
  }
  if (typeof root.detail === "string" && root.detail) return root.detail;
  if (root.message) return root.message;
  return fallback;
}

function errorCode(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const root = data as { code?: unknown; detail?: { code?: unknown } };
  if (typeof root.detail?.code === "string") return root.detail.code;
  return typeof root.code === "string" ? root.code : null;
}

function stageLabel(stage?: string | null): string | null {
  if (!stage) return null;
  if (stage === "first_look") return "First look";
  if (stage === "refined") return "Refined";
  return stage.replace(/_/g, " ");
}

export default function SitePage() {
  const local = useLocalMode();
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const search = useSearchParams();
  const selectedRun = search?.get("run");
  const buildRequested = search?.get("build") === "1";
  const liveVenture = useRef(ventureId);
  liveVenture.current = ventureId;
  const loadFence = useRef(createGodModeRequestFence());
  const pendingLoad = useRef<AbortController | null>(null);
  const liveScope = useCallback(() => ({ venture: liveVenture.current, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch() }), []);
  const renderedScope = { venture: ventureId, owner: verifiedSessionSubject(), epoch: verifiedSessionEpoch(), local };
  const ownerRef = useRef(renderedScope);
  if (ownerRef.current.venture !== renderedScope.venture || ownerRef.current.owner !== renderedScope.owner || ownerRef.current.epoch !== renderedScope.epoch || ownerRef.current.local !== local) ownerRef.current = renderedScope;
  const renderOwner = ownerRef.current;
  const renderCurrent = () => ownerRef.current === renderOwner && sameGodModeScope(renderOwner, liveScope());
  const loadedScope = useRef<GodModeScope | null>(null);
  const quoteSequence = useRef(0);
  const acceptedCustody = useRef<{scope: GodModeScope; value: AcceptedBuildPreview} | null>(null);
  const revisionCustody = useRef<{scope: GodModeScope; value: SiteRevisionStatus} | null>(null);
  const quoteCustody = useRef<{scope: GodModeScope; value: SiteRevisionQuote; appId: string; buildId: string; requestId: string} | null>(null);
  const publishApproval = useRef<{owner: GodModeScope; preview: AcceptedBuildPreview} | null>(null);
  const revisionApproval = useRef<{owner: GodModeScope; preview: AcceptedBuildPreview; quote: SiteRevisionQuote} | null>(null);
  const revisionFlight = useRef<{ owner: typeof renderOwner } | null>(null);
  const owned = <T,>(value: T | null, receipt: {scope: GodModeScope; value: T} | null) =>
    Boolean(value && receipt && receipt.value === value && sameGodModeScope(receipt.scope, liveScope()));
  const [title, setTitle] = useState("Website");
  const [stage, setStage] = useState<string | undefined>();
  const [status, setStatus] = useState<{ code?: string; label?: string; detail?: string } | null>(
    null,
  );
  const [workspaceId, setWorkspaceId] = useState("");
  const [idea, setIdea] = useState("");
  const [ventureName, setVentureName] = useState("");
  const [website, setWebsite] = useState<WebsiteTileData | null>(null);
  const [externalSite, setExternalSite] = useState<{ url: string; note: string } | null>(null);
  const [importedSite, setImportedSite] = useState<ImportedSite | null>(null);
  const importUrl = typeof window === "undefined" ? undefined : siteImportFromSearch(window.location.search);
  const hasExistingSite = Boolean(importedSite || externalSite || importUrl);
  const [site, setSite] = useState<SiteMeta | null>(null);
  const [siteMissing, setSiteMissing] = useState(false);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [device, setDevice] = useState<"desktop" | "mobile">("desktop");
  const [copied, setCopied] = useState(false);
  const [pausedReason, setPausedReason] = useState<string | null>(null);
  const [onboardingPhase, setOnboardingPhase] = useState<string | undefined>();
  const [builderFailed, setBuilderFailed] = useState<string | null>(null);
  const [acceptedPreview, setAcceptedPreview] = useState<AcceptedBuildPreview | null>(null);
  const [acceptedPreviewError, setAcceptedPreviewError] = useState("");
  const [acceptedPreviewBusy, setAcceptedPreviewBusy] = useState(false);
  const [reviewHref, setReviewHref] = useState("");
  const [reviewBuild, setReviewBuild] = useState<BuildWithReview | null>(null);
  useEffect(() => {
    if (reviewBuild && window.location.hash === "#build-review") document.getElementById("build-review")?.scrollIntoView();
  }, [reviewBuild]);
  const [sitePending, setSitePending] = useState(false);
  const [offlineUrls, setOfflineUrls] = useState<string[]>([]);
  const [deployment, setDeployment] = useState<StaticDeployment | null>(null);
  const [publishConfirm, setPublishConfirm] = useState(false);
  const [publishBusy, setPublishBusy] = useState(false);
  const [publishError, setPublishError] = useState("");
  const [localSelection, setLocalSelection] = useState<{ owner: GodModeScope; job_id: string; variant: number } | null>(null);
  const [publicCopyAudit, setPublicCopyAudit] = useState<PublishedCopyAudit | null>(null);
  const [publicCopyAuditError, setPublicCopyAuditError] = useState("");
  const [revision, setRevision] = useState<SiteRevisionStatus | null>(null);
  const [revisionLoadError, setRevisionLoadError] = useState("");
  const [revisionInstruction, setRevisionInstruction] = useState("");
  const [revisionBusy, setRevisionBusy] = useState(false);
  const [revisionError, setRevisionError] = useState("");
  const [revisionQuote, setRevisionQuote] = useState<SiteRevisionQuote | null>(null);
  const [revisionApproved, setRevisionApproved] = useState(false);
  const [revisionRun, setRevisionRun] = useState("");

  const loadRevision = useCallback((appId: string) => {
    if (!renderCurrent() || acceptedCustody.current?.value.appId !== appId || !sameGodModeScope(acceptedCustody.current.scope, liveScope())) return;
    const scope = renderOwner, preview = acceptedCustody.current.value, signal = pendingLoad.current?.signal;
    const current = () => renderCurrent() && !signal?.aborted && acceptedCustody.current?.value === preview && sameGodModeScope(scope, liveScope());
    void api(`/api/apps/${encodeURIComponent(appId)}/site/revisions`, { signal }).then((result) => {
      if (!current()) return;
      const value = result.ok ? result.data as SiteRevisionStatus : null;
      revisionCustody.current = value ? {scope, value} : null;
      setRevision(value);
      setRevisionLoadError(result.ok ? "" : revisionLoadFailure(errorCode(result.data), result.status));
    }).catch(() => {
      if (!current()) return;
      revisionCustody.current = null;
      setRevision(null);
      setRevisionLoadError(revisionLoadFailure(null, 503));
    });
  }, [liveScope, renderOwner]);

  const [hasDraft, setHasDraft] = useState(false);
  const [livePageUrl, setLivePageUrl] = useState("");
  const load = useCallback(async () => {
    if (!renderCurrent()) return;
    quoteSequence.current++;
    loadedScope.current = null; acceptedCustody.current = null; revisionCustody.current = null; quoteCustody.current = null;
    publishApproval.current = null; revisionApproval.current = null;
    setPublishConfirm(false); setPublishBusy(false); setPublishError("");
    setRevisionBusy(false); setRevisionQuote(null); setRevisionApproved(false); setRevisionError(""); setRevisionRun("");
    setLivePageUrl("");
    pendingLoad.current?.abort();
    const controller = new AbortController();
    pendingLoad.current = controller;
    setSite(null);
    setWebsite(null);
    setExternalSite(null);
    setImportedSite(null);
    setAcceptedPreview(null);
    setDeployment(null);
    setRevision(null);
    setReviewHref("");
    setReviewBuild(null); setSitePending(false); setSiteMissing(false);
    setWorkspaceId("");
    setBusy(true);
    setError("");
    setRevisionLoadError("");
    setAcceptedPreviewError("");
    setAcceptedPreviewBusy(true);
    setPublicCopyAudit(null);
    setPublicCopyAuditError("");
    await ensureSession();
    if (controller.signal.aborted || !renderCurrent()) return;
    const request = loadFence.current.begin(liveScope());
    const current = () => renderCurrent() && !controller.signal.aborted && loadFence.current.current(request, liveScope());
    const readScope = {current, signal: controller.signal};
    // Independent reads go out together. The old one-after-another chain (venture, import, dashboard,
    // publication, site record, builder choice) summed six round trips before the first card could render;
    // only the site record waits, because it needs the venture's workspace.
    const signal = controller.signal;
    const venRead = api(`/api/ventures/${ventureId}`, { signal });
    const deploymentRead = liveLandingDeployment(ventureId, readScope).then(live => {
      if (current()) { loadedScope.current = renderOwner; setDeployment(live); }
      return live;
    }).catch(() => null);
    const [ven, imported, dash, pub, siteRes, builderRes, live] = await Promise.all([
      venRead,
      api(`/api/ventures/${ventureId}/assets/site`, { signal }),
      api(`/api/ventures/${ventureId}/dashboard`, { signal }),
      // The concept page is published as a static deployment the dashboard tile can miss: read the live publication fresh.
      api(`/api/ventures/${ventureId}/site/publication`, { signal }),
      venRead.then((result) => {
        const workspace = result.ok ? (result.data as { workspace_id?: string } | null)?.workspace_id : "";
        return workspace && current() ? api(ventureSitePath(workspace, ventureId), { signal }) : null;
      }),
      renderOwner.local ? Promise.resolve(null) : api(`/api/ventures/${ventureId}/builder/choice`, { signal }),
      deploymentRead,
    ]);
    if (!current()) return;
    let wid = "";
    let ideaText = "";
    let name = "";
    if (ven.ok) {
      const v = ven.data as {
        name?: string;
        raw_idea?: string;
        stage?: string;
        status?: { code?: string; label?: string; detail?: string };
        workspace_id?: string;
      };
      name = displayName(v.name, v.raw_idea);
      setTitle(name);
      setVentureName(v.name || name);
      setStage(v.stage);
      setStatus(v.status || null);
      ideaText = v.raw_idea || "";
      setIdea(ideaText);
      wid = v.workspace_id || "";
      setWorkspaceId(wid);
    }
    if (imported.ok) {
      try { setImportedSite(checkedSiteImport(imported.data).site); }
      catch { setError("Your existing website audit could not be read. Check the website again below."); }
    }
    if (!dash.ok && !live) {
      setBusy(false);
      setAcceptedPreviewBusy(false);
      setError(errorMessage(dash.data, "Couldn’t load the website for this venture."));
      return;
    }
    loadedScope.current = renderOwner;
    const payload = (dash.ok ? dash.data : {}) as DashboardPayload;
    const run = (dash.data as {run_status?: {stage?: string; state?: string; landing_page?: {available?: boolean; live_url?: string | null}}} | null)?.run_status;
    setWebsite(payload.tiles?.website || null);
    const pubData = pub.ok ? (pub.data as { state?: string; live_url?: string | null }) : null;
    setLivePageUrl(live?.url || run?.landing_page?.live_url || (pubData?.state === "live" && pubData.live_url ? pubData.live_url : ""));
    setSitePending(Boolean(siteRes?.status === 404 && !run?.landing_page?.available && !(pubData?.state === "live" && pubData.live_url) && (pubData && ["checking", "publishing"].includes(pubData.state || "")
      || run && ["Building", "Build verification", "Testing", "Landing page", "Deploying"].includes(run.stage || "") && ["working", "queued"].includes(run.state || ""))));
    setExternalSite(payload.tiles?.external_site || null);
    setPausedReason(payload.onboarding?.paused_reason || null);
    setOnboardingPhase(payload.onboarding?.phase);

    let storedSiteUrl = "";
    if (wid && siteRes) {
      if (siteRes.ok && scopedSiteMatches(siteRes.data, wid, ventureId)) {
        const loadedSite = siteRes.data as SiteMeta;
        setSite(loadedSite);
        storedSiteUrl = loadedSite.url || "";
        setSiteMissing(false);
      } else if (siteRes.status === 404 || siteRes.ok) {
        setSite(null);
        setSiteMissing(true);
      } else {
        setSite(null);
        setSiteMissing(false);
      }
    } else {
      setSite(null);
      setSiteMissing(true);
    }

    if (builderRes?.ok && builderRes.data && typeof builderRes.data === "object") {
      const err = (builderRes.data as { error?: unknown }).error;
      setBuilderFailed(typeof err === "string" && err.trim() ? err : null);
    } else {
      setBuilderFailed(null);
    }

    // The dashboard/site record is the primary page. Verified QA evidence is a
    // secondary enhancement and can involve several larger requests. Do not
    // hold the whole Website screen behind that chain.
    setBusy(false);
    const storedPublishedUrl = (payload.tiles?.website?.url || storedSiteUrl).trim();
    const checkPublishedCopy = (live: StaticDeployment | null, appId?: string) => {
      const target = publishedPageAuditTarget(storedPublishedUrl, live, appId);
      if (!current()) return;
      void auditPublishedWebsite(
        readScope,
        ventureId,
        target.url,
        target.binding,
      )
        .then((audit) => { if (current()) setPublicCopyAudit(audit); })
        .catch(() => { if (current()) setPublicCopyAuditError("Supymem could not check the published page’s copy right now."); });
    };
    if (live) checkPublishedCopy(live, live.app_id);
    void buildAwaitingReview(ventureId, readScope, live).then((build) => {
      if (current()) { setReviewBuild(build); setReviewHref(build?.state === "succeeded" && build.review?.status === "pending" ? "#build-review" : ""); }
    }).catch(() => { if (current()) { setReviewBuild(null); setReviewHref(""); } });
    void acceptedBuildPreview(ventureId, readScope, live)
      .then((preview) => {
        if (!current()) {
          if (preview) { URL.revokeObjectURL(preview.desktopUrl); URL.revokeObjectURL(preview.mobileUrl); }
          return;
        }
        acceptedCustody.current = preview ? {scope: renderOwner, value: preview} : null;
        setAcceptedPreview((previous) => {
          if (previous) {
            URL.revokeObjectURL(previous.desktopUrl);
            URL.revokeObjectURL(previous.mobileUrl);
          }
          return preview;
        });
        if (preview && !live) {
          loadRevision(preview.appId);
          void api(`/api/apps/${encodeURIComponent(preview.appId)}/deployments`, { signal: controller.signal }).then((result) => {
            if (!current()) return;
            const items = (result.data as { items?: StaticDeployment[] } | null)?.items;
            const live = result.ok && Array.isArray(items)
              ? items.find((item) => acceptedArtifactIsPublished(preview.appId, preview.artifactId, item)) || null
              : null;
            setDeployment(live);
            checkPublishedCopy(live, preview.appId);
          }).catch(() => {
            if (!current()) return;
            setDeployment(null);
            checkPublishedCopy(null);
          });
        } else {
          setRevision(null);
          setRevisionLoadError("");
          if (!live) checkPublishedCopy(null);
        }
      })
      .catch((previewError) => {
        if (!current()) return;
        acceptedCustody.current = null; revisionCustody.current = null; quoteCustody.current = null;
        setAcceptedPreview(null);
        setRevision(null);
        setRevisionLoadError("");
        setAcceptedPreviewError(
          previewError instanceof Error
            ? previewError.message
            : "The accepted build preview could not be verified.",
        );
        if (!live) checkPublishedCopy(null);
      })
      .finally(() => { if (current()) setAcceptedPreviewBusy(false); });
  }, [ventureId, selectedRun, loadRevision, liveScope, renderOwner]);

  useEffect(() => {
    void load();
    const sessionChanged = () => { void load(); };
    window.addEventListener("supymem-session-changed", sessionChanged);
    return () => {
      window.removeEventListener("supymem-session-changed", sessionChanged);
      pendingLoad.current?.abort(); loadFence.current.invalidate();
    };
  }, [load]);

  useEffect(() => {
    if (!sitePending) return;
    const timer = window.setTimeout(() => { void load(); }, 8000);
    return () => window.clearTimeout(timer);
  }, [sitePending, load]);

  // Revision controls need a live deployment: re-check once publishing lands.
  const liveDeploymentId = deployment?.id;
  const previewAppId = acceptedPreview?.appId;
  useEffect(() => {
    if (liveDeploymentId && previewAppId) loadRevision(previewAppId);
  }, [liveDeploymentId, previewAppId, loadRevision]);

  useEffect(
    () => () => {
      if (acceptedPreview) {
        URL.revokeObjectURL(acceptedPreview.desktopUrl);
        URL.revokeObjectURL(acceptedPreview.mobileUrl);
      }
    },
    [acceptedPreview],
  );

  const deployedUrl = deployment?.state === "live" && (deployment.target === "supymem_static" || local && deployment.target === "local_static") && isReachableLandingUrl(deployment.url) ? (deployment.url || "").trim() : "";
  const url = (deployedUrl || livePageUrl || website?.url || site?.url || "").trim();
  const knownOffline = offlineUrls.some((offline) => offline.replace(/\/$/, "") === url.replace(/\/$/, ""));
  const published = !knownOffline && (Boolean(deployedUrl || livePageUrl) || (acceptedPreview
    ? Boolean(deployedUrl)
    : Boolean(website?.published || site?.published)));
  const reachable = !knownOffline && isReachableLandingUrl(url);
  const stillBuilding =
    !builderFailed &&
    !acceptedPreview &&
    !reachable &&
    (sitePending || isLandingBuildPause(pausedReason) ||
      onboardingPhase === "build" ||
      onboardingPhase === "first_look");
  const surface = websiteSurfaceState({
    acceptedPreview: Boolean(acceptedPreview),
    reachable,
    stillBuilding,
    published,
    simulated: website?.simulated === true || site?.fallback === true,
  });
  const hasPreview = surface.showHostedPreview;
  const hasSite =
    hasPreview || surface.showAcceptedPreview || (surface.displayPublished && Boolean(url));
  const displayedUrl = surface.useStoredUrl ? url : "";
  const legacyPreviewUrl = local && !deployedUrl && firstlookPreviewPath(url, ventureId) ? url : "";
  const displayedPublished = surface.displayPublished && !legacyPreviewUrl;
  const simulated = surface.displaySimulated;
  const version = website?.version ?? site?.version;
  const siteStage = stageLabel(website?.stage || site?.stage);
  const provider = website?.provider;
  const variantIndex =
    typeof site?.variant_index === "number" ? site.variant_index + 1 : undefined;
  const shellStatus = websiteHeaderStatus({
    acceptedPreview: Boolean(acceptedPreview),
    published: displayedPublished,
    ventureStatus: status,
  });
  const emptyState = websiteEmptyState({ loading: busy || sitePending, acceptedPreviewBusy, stillBuilding, reviewHref: acceptedPreview ? "" : reviewHref });
  const canCreate = !acceptedPreviewBusy && !stillBuilding && !emptyState.href && Boolean(workspaceId && idea);

  async function copyUrl() {
    if (!renderCurrent() || !sameGodModeScope(loadedScope.current, liveScope()) || !url) return;
    const scope = renderOwner;
    const current = () => renderCurrent() && sameGodModeScope(scope, liveScope());
    try {
      await navigator.clipboard.writeText(url);
      if (!current()) return;
      setCopied(true);
      window.setTimeout(() => { if (current()) setCopied(false); }, 1600);
    } catch {
      if (current()) setCopied(false);
    }
  }

  function changePublicationConfirmation(confirmed: boolean) {
    if (!renderCurrent() || !owned(acceptedPreview, acceptedCustody.current)) return;
    publishApproval.current = confirmed ? {owner: renderOwner, preview: acceptedPreview!} : null;
    setPublishConfirm(confirmed);
  }

  function changeRevisionApproval(approved: boolean) {
    if (!renderCurrent() || !owned(acceptedPreview, acceptedCustody.current) || !owned(revisionQuote, quoteCustody.current)
        || quoteCustody.current?.appId !== acceptedPreview!.appId || quoteCustody.current?.buildId !== acceptedPreview!.buildId) return;
    revisionApproval.current = approved ? {owner: renderOwner, preview: acceptedPreview!, quote: revisionQuote!} : null;
    setRevisionApproved(approved);
  }

  async function publishAcceptedBuild() {
    if (!renderCurrent() || !owned(acceptedPreview, acceptedCustody.current)) return;
    const approval = publishApproval.current;
    if (!publishConfirm || !approval || approval.owner !== renderOwner || approval.preview !== acceptedPreview
        || !sameGodModeScope(approval.owner, liveScope())) return;
    const preview = acceptedPreview!, scope = renderOwner, signal = pendingLoad.current?.signal;
    const current = () => renderCurrent() && !signal?.aborted && owned(preview, acceptedCustody.current) && sameGodModeScope(scope, liveScope());
    if (!acceptedPreview) return;
    publishApproval.current = null; // Consume synchronously before dispatch; old/double callbacks cannot reuse review.
    setPublishConfirm(false);
    setPublishBusy(true);
    setPublishError("");
    const result = await api(`/api/apps/${encodeURIComponent(preview.appId)}/deployments`, {
      signal,
      method: "POST",
      body: JSON.stringify({ build_id: preview.buildId }),
    });
    if (!current()) return;
    if (!result.ok) {
      setPublishBusy(false);
      setPublishError(publicationFailureMessage(errorCode(result.data)));
      return;
    }
    const requested = result.data as StaticDeployment;
    setDeployment(requested);
    setPublishConfirm(false);
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
      if (!current()) return;
      const latest = await api(`/api/apps/${encodeURIComponent(preview.appId)}/deployments`, {signal});
      if (!current()) return;
      const items = (latest.data as { items?: StaticDeployment[] } | null)?.items;
      const deploymentStatus = Array.isArray(items)
        ? items.find((item) => item.id === requested.id) || null
        : null;
      if (deploymentStatus) setDeployment(deploymentStatus);
      if (deploymentStatus?.state === "live") {
        setPublishBusy(false);
        return;
      }
      if (deploymentStatus?.state === "failed") {
        setPublishBusy(false);
        setPublishError(publicationFailureMessage(deploymentStatus.error));
        return;
      }
    }
    setPublishBusy(false);
    setPublishError("Publication is still running in the background. Refresh this page to check it.");
  }

  async function buildRevisionWithTool(requestId?: string) {
    if (!renderCurrent() || !owned(acceptedPreview, acceptedCustody.current)
        || !owned(revision, revisionCustody.current) || revisionFlight.current?.owner === renderOwner) return;
    const preview = acceptedPreview!, baseline = revision!.baseline, scope = renderOwner;
    const signal = pendingLoad.current?.signal, flight = { owner: renderOwner };
    const current = () => renderCurrent() && !signal?.aborted && revisionFlight.current === flight
      && owned(preview, acceptedCustody.current) && owned(revision, revisionCustody.current)
      && sameGodModeScope(scope, liveScope());
    revisionFlight.current = flight;
    setRevisionBusy(true); setRevisionError("");
    try {
      const tools = await api("/api/local-tools/coding-agents", { signal });
      if (!current()) return;
      const available = tools.ok ? (tools.data as { tools?: { name: string; connected?: boolean; coding_available?: boolean }[] } | null)?.tools : null;
      if (!Array.isArray(available) || !available.some(tool => ["claude", "codex", "cursor", "opencode"].includes(tool.name)
          && tool.connected === true && tool.coding_available === true)) {
        setRevisionError("Open Settings > Keys and connections > Coding tools, connect your coding tool, then retry your revision.");
        return;
      }
      let saved: { id?: string; deployment_id?: string; source_sha256?: string; build_id?: string; source_artifact_id?: string } | undefined;
      if (requestId) {
        saved = revision!.requests.find(request => request.id === requestId && request.state === "pending");
      } else {
        const result = await api(`/api/apps/${encodeURIComponent(preview.appId)}/site/revisions`, {
          signal, method: "POST", body: JSON.stringify(revisionRequestBody(baseline, revisionInstruction)),
        });
        if (!current()) return;
        if (!result.ok) {
          const code = errorCode(result.data);
          setRevisionError(code === "device_chat_secret_use_form"
            ? "Remove credentials from your revision. Save them in Settings > Keys and connections, then retry in Website."
            : `${revisionSaveFailure(result.status, code)} Refresh Website and retry your revision.`);
          return;
        }
        saved = result.data as typeof saved;
      }
      if (!saved?.id || saved.deployment_id !== baseline.deployment_id || saved.source_sha256 !== baseline.source_sha256
          || saved.build_id !== preview.buildId || saved.source_artifact_id !== preview.artifactId) {
        setRevisionError("Your published source changed or its revision could not be verified. Refresh Website and retry your revision.");
        return;
      }
      const result = await api(`/api/apps/${encodeURIComponent(preview.appId)}/site/revisions/estimate`, {
        signal, method: "POST", body: JSON.stringify({ revision_request_id: saved.id }),
      });
      if (!current()) return;
      if (!result.ok) {
        setRevisionError(errorMessage(result.data, "Your change brief could not be verified. Refresh Website and check Settings > Keys and connections > Coding tools before retrying."));
        return;
      }
      const value = result.data as SiteRevisionQuote | null;
      if (value?.mode !== "connected_tool" || value.ready !== true || !value.quote_id
          || !/^[a-f0-9]{64}$/.test(value.input_digest)) {
        setRevisionError(value?.mode === "connected_tool" && Array.isArray(value.reasons) && value.reasons.length
          ? value.reasons.join(" ") : "Your change brief could not be verified for your coding tool. Refresh Website and check Settings > Keys and connections > Coding tools before retrying.");
        return;
      }
      quoteCustody.current = { scope, value, appId: preview.appId, buildId: preview.buildId, requestId: saved.id };
      revisionApproval.current = { owner: scope, preview, quote: value };
      setRevisionQuote(value);
      await startRevisionBuild(value);
    } catch {
      if (current()) setRevisionError("Your revision request could not be confirmed. Refresh Website and check your saved build before retrying.");
    } finally {
      if (revisionFlight.current === flight) {
        if (current()) setRevisionBusy(false);
        revisionFlight.current = null;
      }
    }
  }

  async function saveRevisionRequest() {
    if (renderOwner.local) { await buildRevisionWithTool(); return; }
    if (!renderCurrent() || !owned(acceptedPreview, acceptedCustody.current)) return;
    const preview = acceptedPreview!, scope = renderOwner, signal = pendingLoad.current?.signal;
    const current = () => renderCurrent() && !signal?.aborted && owned(preview, acceptedCustody.current) && sameGodModeScope(scope, liveScope());
    if (!owned(revision, revisionCustody.current)) return;
    if (!acceptedPreview || !revision) return;
    setRevisionBusy(true);
    setRevisionError("");
    const result = await api(`/api/apps/${encodeURIComponent(preview.appId)}/site/revisions`, {
      signal,
      method: "POST",
      body: JSON.stringify(revisionRequestBody(revision.baseline, revisionInstruction)),
    });
    if (!current()) return;
    if (!result.ok) {
      setRevisionError(revisionSaveFailure(result.status, errorCode(result.data)));
      setRevisionBusy(false);
      return;
    }
    const refreshed = await api(`/api/apps/${encodeURIComponent(preview.appId)}/site/revisions`, {signal});
    if (!current()) return;
    if (refreshed.ok) {
      const value = refreshed.data as SiteRevisionStatus;
      revisionCustody.current = {scope, value}; setRevision(value);
    }
    setRevisionInstruction("");
    setRevisionBusy(false);
    const requestId = (result.data as { id?: string } | null)?.id;
    if (requestId) await estimateRevision(requestId);
  }

  async function estimateRevision(requestId: string) {
    if (renderOwner.local) { await buildRevisionWithTool(requestId); return; }
    if (!renderCurrent() || !owned(acceptedPreview, acceptedCustody.current)) return;
    const preview = acceptedPreview!, scope = renderOwner, signal = pendingLoad.current?.signal;
    const quoteRequest = ++quoteSequence.current;
    const current = () => quoteRequest === quoteSequence.current && renderCurrent() && !signal?.aborted && owned(preview, acceptedCustody.current) && sameGodModeScope(scope, liveScope());
    if (!acceptedPreview) return;
    setRevisionBusy(true);
    quoteCustody.current = null;
    revisionApproval.current = null;
    setRevisionQuote(null);
    setRevisionApproved(false);
    setRevisionError("");
    const result = await api(`/api/apps/${encodeURIComponent(preview.appId)}/site/revisions/estimate`, {
      signal,
      method: "POST",
      body: JSON.stringify({ revision_request_id: requestId }),
    });
    if (!current()) return;
    if (!result.ok) {
      setRevisionError(errorMessage(result.data, "The revision build could not be estimated. Review the saved request and try again."));
    } else {
      const value = result.data as SiteRevisionQuote;
      quoteCustody.current = {scope, value, appId: preview.appId, buildId: preview.buildId, requestId}; setRevisionQuote(value);
    }
    setRevisionBusy(false);
  }

  async function startRevisionBuild(prepared = revisionQuote) {
    if (renderOwner.local && prepared?.mode !== "connected_tool") {
      const request = revision?.requests.find(item => item.deployment_id === revision.baseline.deployment_id && item.state === "pending");
      if (request) await buildRevisionWithTool(request.id);
      return;
    }
    if (!renderCurrent() || !owned(acceptedPreview, acceptedCustody.current) || !prepared) return;
    const preview = acceptedPreview!, scope = renderOwner, signal = pendingLoad.current?.signal;
    const current = () => renderCurrent() && !signal?.aborted && owned(preview, acceptedCustody.current) && owned(prepared, quoteCustody.current) && sameGodModeScope(scope, liveScope());
    if (!owned(prepared, quoteCustody.current) || quoteCustody.current?.appId !== preview.appId || quoteCustody.current?.buildId !== preview.buildId) return;
    const toolBuild = renderOwner.local && prepared.mode === "connected_tool";
    if (!acceptedPreview || !prepared.ready || (!toolBuild && (!revisionApproved
        || !Number.isSafeInteger(prepared.ceiling_micro) || !prepared.ceiling_micro
        || Date.parse(prepared.expires_at) <= Date.now()))) return;
    const approval = revisionApproval.current;
    if (!approval || approval.owner !== renderOwner || approval.preview !== preview || approval.quote !== prepared
        || !sameGodModeScope(approval.owner, liveScope())) return;
    revisionApproval.current = null;
    setRevisionApproved(false);
    setRevisionBusy(true);
    setRevisionError("");
    const quote = prepared;
    const result = await api(`/api/apps/${encodeURIComponent(preview.appId)}/builds`, {
      signal,
      method: "POST",
      headers: { "Idempotency-Key": `site-revision-${quote.quote_id}` },
      body: JSON.stringify({ quote_id: quote.quote_id, input_digest: quote.input_digest,
        approve_budget_micro: quote.ceiling_micro }),
    });
    if (!current()) return;
    if (!result.ok) {
      setRevisionError(errorMessage(result.data, "The reviewed revision build did not start. Refresh its saved state before retrying."));
    } else {
      const orderId = (result.data as {device_work_order_id?: string} | null)?.device_work_order_id;
      if (toolBuild && (!orderId || !/^dwo_[A-Za-z0-9]+$/.test(orderId)
          || !["approved", "claimed"].includes((result.data as { state?: string } | null)?.state || ""))) {
        setRevisionError("Your revision start could not be verified. Refresh Website and check your saved build before retrying.");
        setRevisionBusy(false);
        return;
      }
      if (toolBuild && orderId) {
        window.location.assign(`/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(orderId)}`);
      }
      setRevisionRun(toolBuild ? orderId! : (result.data as { build_run_id?: string } | null)?.build_run_id || "");
      if (toolBuild) setRevisionInstruction("");
      quoteCustody.current = null;
      setRevisionQuote(null);
      setRevisionApproved(false);
    }
    setRevisionBusy(false);
  }


  return (
    <AppShell
      title={title}
      subtitle={QUESTION}
      ventureId={ventureId}
      stage={stage}
      status={shellStatus}
    >
      <div className={styles.page}>
        <h1 className={styles.question}>{QUESTION}</h1>
        <OneCardStack flow>
        {displayedPublished && displayedUrl && <OneCard id="site-live" title="Your landing page is live" status="done"
          body="Share your landing page and find your first customers. Your live page stays available while you work on a new version."
          actions={<><a href={displayedUrl} className="primary" target="_blank" rel="noopener noreferrer">Visit landing page</a>
            <button type="button" onClick={() => void copyUrl()}>{copied ? "Copied" : "Copy link"}</button>
            <a href={`/v/${encodeURIComponent(ventureId)}/site?build=1`}>Build a new version</a>
            <a href={`/gtm?venture=${encodeURIComponent(ventureId)}`}>Find customers</a></>}>
          <p>{displayedUrl}</p>
        </OneCard>}
        {legacyPreviewUrl && !busy && <OneCard id="site-legacy-preview" title="Your saved landing page" status="done"
          body="This preview is private. To make it public, use Publish on Website.">
          <LandingPreviewFrame src={legacyPreviewUrl} ventureId={ventureId} sandbox="" />
        </OneCard>}
        {!busy && workspaceId && <ExistingSiteCard key={`${ventureId}:${renderOwner.owner}:${renderOwner.epoch}`} owner={{ actorId: renderOwner.owner || "", workspaceId, ventureId }} site={importedSite} knownUrl={externalSite?.url || importUrl}
          current={renderCurrent} saved={value => { setImportedSite(value.site); setExternalSite(value.site ? { url: value.site.url, note: value.site.status === "verified" ? "Existing website checked" : "Existing website saved" } : null); setError(""); }} />}
        {externalSite && !importedSite && /^https?:\/\//i.test(externalSite.url) && (
          <p data-testid="external-site">
            <strong>{externalSite.note}</strong>{" "}
            <a href={externalSite.url} target="_blank" rel="noreferrer noopener">{externalSite.url}</a>
          </p>
        )}

        {busy && <OneCard id="site-loading" title="Loading your page" status="working" />}
        {error && (
          <OneCard id="site-error" title="Page did not load" status="error" actions={<button type="button" data-usage-control="website_retry" className="primary" onClick={() => void load()}>Retry</button>}>
            <ErrorNotice error={error} source="site.load" className={styles.fail} />
          </OneCard>
        )}
        {acceptedPreviewError && !displayedPublished && (
          <ErrorNotice error={acceptedPreviewError} as="div" className={styles.error} source="site.accepted_preview">
            <h2>Your landing page preview needs attention</h2>
            <p className={styles.fail}>{acceptedPreviewError}</p>
            <p>Your page is saved.</p>
            <button type="button" disabled={acceptedPreviewBusy} onClick={() => void load()}>Retry preview</button>
          </ErrorNotice>
        )}

        {!busy && !error && builderFailed && !hasSite && !hasExistingSite && (
          <section className={styles.empty}>
            <h2>The landing page did not start</h2>
            <p className={styles.fail}>{builderFailed}</p>
            <p>
              Nothing was written, so nothing is lost. Go back to the venture and try again.
            </p>
            <a href={`/v/${ventureId}`} className="primary">
              Back to the venture
            </a>
          </section>
        )}


        {/* A concept draft or unpublished first-look site never hides the one-click page build. */}
        {!busy && !error && !hasExistingSite && (displayedPublished && buildRequested || !acceptedPreviewError && !builderFailed
          && !displayedPublished && (buildRequested || !acceptedPreview && !emptyState.href && !acceptedPreviewBusy && (!stillBuilding || sitePending)))
          && <BuildPageCard ventureId={ventureId} {...(local ? { onSelection: (selection: { job_id: string; variant: number }) => {
            if (renderCurrent()) setLocalSelection(previous => previous?.owner === renderOwner && previous.job_id === selection.job_id && previous.variant === selection.variant
              ? previous : { owner: renderOwner, ...selection });
          } } : {})} />}
        {!busy && !error && !hasExistingSite && !acceptedPreviewError && !builderFailed && !hasSite && !hasDraft && (
          !emptyState.href && !acceptedPreviewBusy && !stillBuilding ? null : (
            <OneCard id="site-empty" title={emptyState.title} body={emptyState.body} status={sitePending || acceptedPreviewBusy || stillBuilding ? "working" : "needs_you"}
              actions={emptyState.href ? <a href={emptyState.href} className="primary">{emptyState.linkLabel}</a> : undefined}>
              {!emptyState.href && !sitePending && !acceptedPreviewBusy && !stillBuilding && <ProvenanceChip mode="no_data" />}
            </OneCard>
          )
        )}
        {!local && !busy && !error && !sitePending && !hasExistingSite && !acceptedPreview && !displayedPublished && <PublishConceptPage key={ventureId + ":concept"} ventureId={ventureId} onDraft={setHasDraft} />}
        {local && !busy && !error && loadedScope.current === renderOwner && <LocalSiteControls key={`${ventureId}:${renderOwner.owner}:${renderOwner.epoch}`} ventureId={ventureId}
          appId={acceptedPreview?.appId} buildId={acceptedPreview?.buildId}
          savedPage={Boolean(site)}
          selection={localSelection?.owner === renderOwner ? localSelection : undefined}
          onPublication={publication => {
            if (!renderCurrent()) return;
            if (publication.state === "offline" || publication.state === "takedown_pending") setOfflineUrls(previous => [...previous, ...(publication.url ? [publication.url] : [])]);
            else if (publication.url) {
              setOfflineUrls(previous => previous.filter(url => url !== publication.url));
              setDeployment({ id: "local", app_id: acceptedPreview?.appId || "", target: "local_static", state: publication.state, version: 1, url: publication.url });
            }
          }} />}
        {hasSite && !displayedPublished && !legacyPreviewUrl && (
          <OneCard id="site-preview" step="Preview" title={displayedUrl ? "Your page is live" : "Your page is ready"}
            actionsFirst
            status={displayedUrl ? "done" : "needs_you"}
            actions={displayedUrl
              ? <><a href={displayedUrl} className="primary" target="_blank" rel="noopener noreferrer" data-usage-control="website_open_url">Visit landing page</a>
                <button type="button" data-usage-control="website_copy_url" onClick={() => void copyUrl()}>{copied ? "Copied" : "Copy link"}</button>
                <a href={`/v/${ventureId}/site?build=1`}>Build a new version</a><a href={`/gtm?venture=${ventureId}`}>Find customers</a>
                <a href={displayedUrl} target="_blank" rel="noopener noreferrer">Open full preview</a></>
              : !local && acceptedPreview && !publishConfirm
                ? <button type="button" data-usage-control="website_publish_review" className="primary" disabled={publishBusy} onClick={() => changePublicationConfirmation(true)}>{publishBusy ? "Publishing…" : "Publish"}</button>
                : undefined}>
            {displayedUrl && <p className={styles.urlText}>{displayedUrl}</p>}
            {!displayedUrl && !acceptedPreview && <ProvenanceChip mode="no_data" />}
                  {!local && acceptedPreview && publishConfirm && (
                    <div role="alert" data-not-error className={styles.simBanner}>
                      <strong>Publish this landing page?</strong>
                      <p>
                        Anyone with the link will be able to visit this page. It is a landing page only, not a working app. You can keep it private.
                      </p>
                      <div className={styles.actions}>
                        <button
                          type="button"
                          data-usage-control="website_publish_confirm"
                          className="primary"
                          disabled={publishBusy}
                          onClick={() => void publishAcceptedBuild()}
                        >
                          {publishBusy ? "Publishing…" : "Publish landing page"}
                        </button>
                        <button type="button" data-usage-control="website_publish_cancel" disabled={publishBusy} onClick={() => changePublicationConfirmation(false)}>
                          Keep private
                        </button>
                      </div>
                    </div>
                  )}
            <ErrorNotice error={publishError} source="site.publishError" className={styles.fail} />
            {hasPreview && displayedUrl && !firstlookPreviewPath(displayedUrl, ventureId) && (
              // The public page is served from another origin whose CSP/routing can load the console shell
              // inside the frame ("Opening Supymem…" forever), so link out instead of embedding it.
              <p><a href={displayedUrl} target="_blank" rel="noopener noreferrer" data-usage-control="website_open_live">Open your live page in a new tab</a></p>
            )}
            {hasPreview && displayedUrl && !!firstlookPreviewPath(displayedUrl, ventureId) && (
              <div className={styles.frameWrap}>
                <div className={styles.toggles}>
                  <button
                    type="button"
                    className={styles.toggle}
                    aria-pressed={device === "desktop"}
                    onClick={() => setDevice("desktop")}
                  >
                    Desktop
                  </button>
                  <button
                    type="button"
                    className={styles.toggle}
                    aria-pressed={device === "mobile"}
                    onClick={() => setDevice("mobile")}
                  >
                    Mobile
                  </button>
                </div>
                <div
                  className={`${styles.frame} ${
                    device === "mobile" ? styles.frameMobile : styles.frameDesktop
                  }`}
                >
                  <LandingPreviewFrame
                    src={displayedUrl}
                    ventureId={ventureId}
                    sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox"
                  />
                </div>
              </div>
            )}

            {acceptedPreview && surface.showAcceptedPreview && (
              <div className={styles.frameWrap}>
                <div className={styles.toggles}>
                  <button
                    type="button"
                    className={styles.toggle}
                    aria-pressed={device === "desktop"}
                    onClick={() => setDevice("desktop")}
                  >
                    Desktop
                  </button>
                  <button
                    type="button"
                    className={styles.toggle}
                    aria-pressed={device === "mobile"}
                    onClick={() => setDevice("mobile")}
                  >
                    Mobile
                  </button>
                </div>
                <p className={styles.note}>
                  Private preview from the accepted build's independently verified QA evidence · {acceptedPreview.passed} checks passed.
                </p>
                <div
                  className={`${styles.frame} ${
                    device === "mobile" ? styles.frameMobile : styles.frameDesktop
                  }`}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={device === "mobile" ? acceptedPreview.mobileUrl : acceptedPreview.desktopUrl}
                    alt={`Accepted ${device} landing-page preview`}
                    style={{ display: "block", width: "100%", height: "auto" }}
                  />
                </div>
              </div>
            )}

            {siteMissing && !url && !acceptedPreview && (
              <p className={styles.note}>
                Dashboard has a website tile, but the workspace site record is missing.
              </p>
            )}
          </OneCard>
        )}
        {reviewBuild && <OneCard id="build-review" title="Landing page result" status={reviewBuild.landing_page?.available ? "done" : "needs_you"}
          body={reviewBuild.landing_page?.qa_note || undefined} actions={<><a href={`/v/${ventureId}/site?build=1`}>Build a new version</a><a href={`/gtm?venture=${ventureId}`}>Find customers</a></>}>
          <BuildEvidence key={reviewBuild.id} build={reviewBuild} onReviewed={() => void load()} />
        </OneCard>}
        {!busy && !error && (hasSite || hasDraft) && (
          <OneCard id="site-waitlist" step="Next" title="Collect signups" body="Add a waitlist so visitors can reach you.">
            <VentureWaitlist key={ventureId} ventureId={ventureId} usageSurface="website" />
          </OneCard>
        )}
        {!busy && !error && (
          <OneCard id="site-more" step="Manage" title="Manage landing page" body={displayedPublished ? "Review hosting status or take your landing page offline." : "Hosting status and offline controls become available after publishing."}>
            <details className={styles.management}>
              <summary>Landing page hosting status</summary>
              {!displayedPublished && <p className={styles.lede}>Publish a landing page first. Hosting status and offline controls will appear here afterward.</p>}
              <p className={styles.lede}>A public landing page or waitlist does not mean the product application is live.</p>
              {!local && <UnpublishSite key={ventureId + ":hosting"} ventureId={ventureId} onOfflineUrls={setOfflineUrls} />}
              {hasSite && <>
            <div className={styles.meta}>
              {version != null && <span className={styles.pill}>Version {version}</span>}
              {siteStage && <span className={styles.pill}>{siteStage}</span>}
              {variantIndex != null && (
                <span className={styles.pill}>
                  Look {variantIndex} of {MAX_VARIANTS}
                </span>
              )}
              <span className={styles.pill}>
                {displayedPublished ? "Published" : "Not on the public internet yet"}
              </span>
              {acceptedPreview && !displayedPublished && <span className={styles.pill}>Ready to publish</span>}
              {provider && <span className={styles.pill}>Provider: {provider}</span>}
              {site?.fallback === true && !acceptedPreview && (
                <span className={styles.pill}>Draft template, not AI-written yet</span>
              )}
              {acceptedPreview && !hasPreview ? null : simulated ? (
                <ProvenanceChip mode="simulated" />
              ) : (
                <ProvenanceChip mode="live" />
              )}
            </div>

            {publicCopyAudit?.state === "needs_review" && (
              <ErrorNotice error="Published page needs copy review" as="div" className={styles.simBanner} source="site.copy_audit">
                <strong>Published page needs copy review</strong>
                <p>The live page contains an unfinished setup or publication instruction. Review its public copy and publish a corrected, accepted build.</p>
              </ErrorNotice>
            )}
            {publicCopyAuditError && <p className={styles.fail} role="status">{publicCopyAuditError}</p>}

            {acceptedPreview && revision && (
              <section className={styles.section} aria-label="Landing page revision">
                <h2>Revise this published landing page</h2>
                <p className={styles.note}>
                  {local ? "Your connected coding tool makes your requested copy or layout change against this saved source. You see the checked result before choosing Publish. Your public page stays unchanged until then."
                    : "Save a specific copy or layout change against the current published source, then review the build budget. Supymem will make a new source artifact and run independent QA. The live page changes only after your review and a separate publish action."}
                </p>
                <label htmlFor="site-revision-instruction">What should change?</label>
                <textarea
                  id="site-revision-instruction"
                  className={styles.revisionInput}
                  value={revisionInstruction}
                  maxLength={2000}
                  onChange={(event) => setRevisionInstruction(event.target.value)}
                  placeholder="For example: remove the outdated setup instruction in the footer; keep the existing waitlist link."
                />
                <button type="button" data-usage-control="website_revision_request" disabled={revisionBusy || revisionInstruction.trim().length < 8} onClick={() => void saveRevisionRequest()}>
                  {revisionBusy ? local ? "Starting…" : "Saving…" : local ? "Make this change" : "Save revision request"}
                </button>
                <ErrorNotice error={revisionError} source="site.revisionError" className={styles.fail} />
                {local && revisionError.includes("Coding tools") && <a href="/settings/local">Connect your coding tool</a>}
                {revision.requests.find((item) => item.deployment_id === revision.baseline.deployment_id) && (
                  <p className={styles.note}>Latest request: {revision.requests.find((item) => item.deployment_id === revision.baseline.deployment_id)?.state}. The public page is unchanged until a new verified build is reviewed and published.</p>
                )}
                {!revisionQuote && !revisionRun && revision.requests.find((item) => item.deployment_id === revision.baseline.deployment_id && item.state === "pending") && (
                  <button type="button" data-usage-control="website_revision_estimate" disabled={revisionBusy} onClick={() => void estimateRevision(revision.requests.find((item) => item.deployment_id === revision.baseline.deployment_id && item.state === "pending")!.id)}>
                    {local ? "Retry your revision" : "Review revision build estimate"}
                  </button>
                )}
                {!local && revisionQuote && (
                  <div className={styles.revisionQuote}>
                    {revisionQuote.reasons.length > 0 && <ErrorNotice error={revisionQuote.reasons.join(", ")} source="site.revision_quote" />}
                    {!local && <label><input type="checkbox" checked={revisionApproved} disabled={!revisionQuote.ready || revisionBusy} onChange={(event) => changeRevisionApproval(event.target.checked)} /> Start this revision build</label>}
                    <button type="button" data-usage-control="website_revision_start" disabled={!revisionQuote.ready || revisionBusy || (!local && (!revisionApproved || Date.parse(revisionQuote.expires_at) <= Date.now()))} onClick={() => void startRevisionBuild()}>
                      {revisionBusy ? "Starting…" : "Start revision build"}
                    </button>
                  </div>
                )}
                {revisionRun && <p role="status">{local ? "Your change request is saved. " : "Revision build queued. "}<a href={local ? `/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(revisionRun)}` : revisionBuildHref(ventureId, acceptedPreview.appId, revisionRun)}>{local ? "Watch your change and see its checked result" : "Open its saved build and QA evidence"}</a>. Review it there before publishing.</p>}
              </section>
            )}

            {acceptedPreview && revisionLoadError && (
              <ErrorNotice error={revisionLoadError} as="div" className={styles.error} source="site.revision_load">
                <h2>Published-page revision unavailable</h2>
                <p className={styles.fail}>{revisionLoadError}</p>
                <button type="button" data-usage-control="website_retry" onClick={() => void load()}>Retry revision check</button>
              </ErrorNotice>
            )}

            {simulated && displayedPublished && (
              <div className={styles.simBanner} role="status">
                This preview is not on the public internet yet. Your custom domain stays yours.
              </div>
            )}

              </>}
            </details>
          </OneCard>
        )}
        </OneCardStack>
        <NextStepStrip ventureId={ventureId} compact />
      </div>
    </AppShell>
  );
}

const BUILD_STEPS = ["Reading your idea", "Writing honest copy", "Rendering preview"];
/** Client-only progress ticker: the create call has no progress API, so steps advance on a timer. */
function BuildTicker() {
  const [n, setN] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setN((v) => v + 1), 3500);
    return () => window.clearInterval(t);
  }, []);
  return <p role="status">{BUILD_STEPS[Math.min(n, BUILD_STEPS.length - 1)]}…</p>;
}
