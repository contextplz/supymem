"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, usePathname, useSearchParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { api, apiSwr, ensureSession, openIdeaSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { cacheGet, cachePut } from "@/lib/apiCache";
import { apiErrorCode, apiFailure, isRetryableNativeOpenFailure, isTransientNativeIdentityFence } from "@/lib/apiFailure";
import { jobSucceeded, useDurableJob } from "@/hooks/useDurableJob";
import { canConfirmValidatedIdea, founderReviewText, GSTACK_ABOUT, GSTACK_HEADING, GSTACK_SKIP, latestValidationRuns, validationMethods, type ForcingQuestion } from "@/lib/validationCopy";
import { jobFailure, reportJobFailure, restoreIdeaSessionJob, type DurableJob } from "@/lib/durableJobs";
import { ideaJourneyActivity } from "@/lib/ideaJourneyActivity";
import { pendingJourneyQuestion } from "@/lib/journeyQuestion";
import { DictateButton } from "@/components/chat/DictateButton";
import { clearIdeaDraft, readPersistentIdeaDraft, writeIdeaDraft } from "@/lib/ideaDraft";
import { clearManualIdeaDraft, readManualIdeaDraft, writeManualIdeaDraft, type ManualIdeaDraft } from "@/lib/manualIdeaDraft";
import { clearIdeaCorrectionDraft, readIdeaCorrectionDraft, writeIdeaCorrectionDraft, type IdeaCorrectionScope } from "@/lib/ideaCorrectionDraft";
import { IDEA_ANSWER_MAX_CHARS, canSendIdeaAnswer, composeIdeaAnswer, currentQuestionAnswer, editableIdeaAnswer, founderChoiceWords, openPreviousAnswerReview, previousIdeaAnswers, questionSelectionMode, savedIdeaChoiceAnswer, savedIdeaOptionLabels, savedIdeaQuestionOptions, shortOptionLabel, toggleIdeaChoice } from "@/lib/ideaAnswer";
import { currentPlanState, ideaJourneyProgress, JOURNEY_OUTCOME } from "@/lib/companyJourney";
import { ideaObservationStep } from "@/lib/observability";
import { founderExperimentContext, personalProjectReview } from "@/lib/validationNextTest";
import { activeMacTaskReviewPath } from "@/lib/macTaskPrepareFailure";
import { useDetailLevel } from "@/lib/detailLevel";
import styles from "./idea.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Turn = {
  id: string; role: string; kind: string; text: string;
  options?: { why?: string; field?: string; prefill?: string; options?: string[]; option_labels?: string[]; allow_free_text?: boolean; selection_mode?: "single" | "multiple"; recommended_option?: string; recommendation_reason?: string; phase?: string; gstack_name?: string; index?: number; total?: number } | string[];
};
type Validation = {
  id: string; revision_id?: string; method: string; state: string; verdict: string;
  founder_decision?: string | null;
  report: { summary?: string; gaps?: string[]; questions?: string[];
    presentation?: { findings: { assessment: string; source_ids?: string[] }[]; opening: string; options: string[] };
    strengths?: string[]; biggest_risk?: string; forcing_questions?: ForcingQuestion[];
    findings?: { method: string; assessment: string; source_ids?: string[] }[];
    proposed_tests?: { hypothesis: string; action: string; success_measure: string }[] };
};
type Session = {
  id: string; state: string; case: string; turns: Turn[]; idea_summary?: string;
  credits_continue_job_id?: string | null; credits_continue_kind?: string | null;
  project_intent?: "startup" | "personal"; question_budget?: number; questions_asked?: number;
  current_revision?: { id?: string; summary?: string; structured?: Record<string, unknown> } | null;
  validation_runs?: Validation[];
  gstack_answers?: { assumed?: boolean }[];
  initial_job_id?: string | null;
  next_href?: string;
  initialization_error?: string | null;
  clarification_retries_remaining?: number;
  clarification_invalid_output_seen?: boolean;
  manual_review_available?: boolean;
  clarification_retry_available?: boolean;
};
type VentureLite = { workspace_id?: string; name?: string; raw_idea?: string; idea_summary?: string };

const startupSteps = ["Clarify", "Validate", "Research", "Plan"];
const personalSteps = ["Clarify", "Review", "Build"];
const editableFields = ["problem", "customer", "offer", "first_user_path", "pricing_hypothesis", "workflow", "desired_outcome", "requirements", "constraints"];
const listFields = new Set(["requirements", "constraints"]);

function ChoiceText({ option }: { option: string; label: string }) {
  return <span className={styles.choiceText}><span className={styles.choiceLabel}>{option}</span></span>;
}

export default function IdeaCanvasPage() {
  const local = useLocalMode();
  const [detailLevel] = useDetailLevel();
  const detailed = detailLevel === "detailed";
  const params = useParams();
  const search = useSearchParams();
  const chatFirstEntry = search.get("chat") === "1";
  const editPrevious = search.get("edit_previous") === "1";
  const previousOpened = useRef("");
  const ventureId = String(params.ventureId || "");
  const activeVenture = useRef(ventureId);
  const loadVersion = useRef(0);
  const pendingAction = useRef(false);
  const hydrating = useRef(false); // true while a cached session is shown and the live one is loading
  const answerReview = useRef<HTMLDetailsElement>(null);
  const pendingCaseRef = useRef<boolean | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [venture, setVenture] = useState<VentureLite | null>(null);
  const [planState, setPlanState] = useState<string | undefined>();
  const [answer, setAnswer] = useState("");
  const [manualDraft, setManualDraft] = useState<ManualIdeaDraft>({ problem: "", customer: "", offer: "" });
  const [selectedOptions, setSelectedOptions] = useState<string[]>([]);
  const [pendingCase, setPendingCase] = useState<boolean | null>(null);
  const [directionChange, setDirectionChange] = useState<boolean | null>(null);
  const [editingQuestionId, setEditingQuestionId] = useState<string | null>(null);
  const [correctionChoices, setCorrectionChoices] = useState<string[]>([]);
  const [editingField, setEditingField] = useState<string | null>(null);
  const [revisionEdit, setRevisionEdit] = useState("");
  const [busy, setBusy] = useState(false);
  const [startedTests, setStartedTests] = useState<Record<string, boolean>>({});
  const [ready, setReady] = useState(false);
  const [checkingStatus, setCheckingStatus] = useState(false);
  const [reconciling, setReconciling] = useState(false);
  const [statusCheckedAt, setStatusCheckedAt] = useState("");
  const [notice, setNotice] = useState("");
  const [failedJob, setFailedJob] = useState<DurableJob | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [syncError, setSyncError] = useState("");

  const job = useDurableJob(jobId, (finished) => {
    pendingAction.current = false;
    setJobId(null);
    if (jobSucceeded(finished)) {
      setNotice(""); setFailedJob(null);
      void load().then(() => {
        const desired = pendingCaseRef.current;
        if (desired !== null) {
          pendingCaseRef.current = null;
          setPendingCase(null);
          void setCase(desired);
        }
      });
    } else {
      setNotice(jobFailure(finished)); setFailedJob(finished);
      const desired = pendingCaseRef.current;
      if (desired !== null) {
        pendingCaseRef.current = null;
        setPendingCase(null);
        void setCase(desired);
      } else {
        void load();
      }
    }
  });

  async function startTest(test: { hypothesis: string; action: string; success_measure: string }) {
    setNotice("");
    const result = await api(`/api/ventures/${ventureId}/experiments/start`, { method: "POST", body: JSON.stringify(test) });
    if (result.ok) setStartedTests((current) => ({ ...current, [test.hypothesis]: true }));
    else setNotice(apiFailure(result, "starting this test"));
  }

  async function load(retryTransientFence = true) {
    const version = ++loadVersion.current;
    let owner = verifiedSessionSubject();
    const current = () => activeVenture.current === ventureId && version === loadVersion.current
      && (!owner || owner === verifiedSessionSubject());
    setReconciling(true);
    try {
      const auth = await ensureSession();
      if (!current()) return;
      owner = verifiedSessionSubject();
      if (!auth.ok && retryTransientFence && isTransientNativeIdentityFence(auth)) {
        await load(false); return;
      }
      // Show the last saved session at once; the live open below replaces it.
      const openPath = `/api/ventures/${ventureId}/idea/session-open`;
      const scope = verifiedSessionSubject();
      const known = cacheGet(scope, openPath);
      if (known) { hydrating.current = true; setSession(known.data as Session); setReady(true); }
      // The session-open request runs alone first: as a concurrent native IPC call beside two
      // GETs it failed before reaching the server in the Mac app. It is idempotent, so one
      // retry after a pre-response native failure is safe. Company and plans load next, in
      // parallel, so the heading is set even when the open fails.
      // Concurrent loaders share it; fresh saved sessions are read without another POST.
      const open = () => openIdeaSession(ventureId);
      let ensured = await open();
      if (!ensured.ok && isRetryableNativeOpenFailure(ensured)) {
        await new Promise((r) => setTimeout(r, 400));
        if (!current()) return;
        ensured = await open();
      }
      // Save at once: leaving the page mid-load must not lose the session for the next visit.
      if (ensured.ok) cachePut(scope, openPath, ensured.data);
      const [ven, plans] = await Promise.all([
        apiSwr(`/api/ventures/${ventureId}`, (r) => { if (current() && r.ok) setVenture(r.data as VentureLite); }),
        api(`/api/ventures/${ventureId}/plans`),
      ]);
      hydrating.current = false;
      if (!current()) return;
      if (!ven.ok && retryTransientFence && isTransientNativeIdentityFence(ven)) {
        await load(false); return;
      }
      // A native access-token refresh can finish between two page-loader calls.
      // The server-side open-session operation is idempotent, so this exact
      // identity fence is safe to reconcile once. Do not replay answers, plan
      // changes or any other founder mutation.
      if (!ensured.ok && retryTransientFence && isTransientNativeIdentityFence(ensured)) {
        await load(false); return;
      }
      if (!ensured.ok) {
        setNotice(apiFailure(ensured, "opening your guided journey")); setReady(true); return;
      }
      const saved = ensured.data as Session;
      setSession(saved);
      setPlanState(saved.state !== "confirmed" && saved.current_revision?.structured?._corrects_revision_id
        ? undefined : plans.ok ? currentPlanState(plans.data) : undefined);
      if (saved.initialization_error) {
        setNotice("Supymem could not start clarification. Your idea is saved; refresh once to try again.");
        setReady(true);
        return;
      }
      const restored = await restoreIdeaSessionJob(ventureId, saved, (ven.data as VentureLite)?.workspace_id,
          (job) => { if (current() && job.revision_id === saved.current_revision?.id) reportJobFailure(job); });
      if (!current()) return;
      setJobId(restored.id); setSyncError(restored.error);
      if (restored.lastFailure && restored.lastFailure.revision_id === saved.current_revision?.id) {
        setNotice(jobFailure(restored.lastFailure)); setFailedJob(restored.lastFailure);
      }
      setReady(true);
    } finally {
      if (current()) setReconciling(false);
    }
  }

  async function checkSavedStatus() {
    if (checkingStatus) return;
    setCheckingStatus(true);
    setNotice("");
    try {
      // Keep the last saved stage visible while its durable status is read.
      // This is a reconciliation, not another paid journey submission.
      await load();
      setStatusCheckedAt(new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
    } finally {
      setCheckingStatus(false);
    }
  }

  useEffect(() => {
    activeVenture.current = ventureId;
    pendingAction.current = false;
    setBusy(false);
    pendingCaseRef.current = null; setPendingCase(null);
    setDirectionChange(null);
    setSession(null); setVenture(null); setPlanState(undefined); setReady(false); setCheckingStatus(false); setStatusCheckedAt(""); setNotice(""); setFailedJob(null); setJobId(null); setAnswer(""); setSelectedOptions([]); setSyncError("");
    setEditingQuestionId(null); setEditingField(null); setCorrectionChoices([]); setRevisionEdit("");
    // Paint the last saved session and company synchronously; load() then revalidates.
    const scope = verifiedSessionSubject();
    const knownSession = cacheGet(scope, `/api/ventures/${ventureId}/idea/session-open`);
    if (knownSession) { hydrating.current = true; setSession(knownSession.data as Session); setReady(true); }
    const knownVenture = cacheGet(scope, `/api/ventures/${ventureId}`);
    if (knownVenture) setVenture(knownVenture.data as VentureLite);
    void load();
    return () => { loadVersion.current++; activeVenture.current = ""; };
    // The journey must reload only when the selected company changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ventureId]);

  async function send(text: string, correctionQuestionId?: string) {
    if (!session || !canSendIdeaAnswer(text, Boolean(busy || pendingAction.current || hydrating.current || jobId || syncError))) return false;
    pendingAction.current = true;
    setBusy(true); setNotice("");
    const result = await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/messages`, {
      method: "POST", body: JSON.stringify({ text: text.trim(), ...(correctionQuestionId ? { correction_question_id: correctionQuestionId } : {}) }),
    });
    if (activeVenture.current !== ventureId) return false;
    setBusy(false);
    if (!result.ok) { pendingAction.current = false; setNotice(apiFailure(result, "saving your answer")); return false; }
    if (!correctionQuestionId && lastQuestion?.id) {
      clearIdeaDraft(localStorage, ventureId, lastQuestion.id);
      clearIdeaDraft(sessionStorage, ventureId, lastQuestion.id);
    }
    setAnswer("");
    setSelectedOptions([]);
    const next = result.data as { job_id?: string; session?: Session };
    setSession(next.session || session); setJobId(next.job_id || null);
    if (!next.job_id) pendingAction.current = false;
    return true;
  }

  const reopenOwner = verifiedSessionSubject(), reopenEpoch = verifiedSessionEpoch(), reopenPath = usePathname();
  const reopenScope = useRef({ owner: reopenOwner, epoch: reopenEpoch, path: reopenPath, active: true });
  if (reopenScope.current.owner !== reopenOwner || reopenScope.current.epoch !== reopenEpoch || reopenScope.current.path !== reopenPath)
    reopenScope.current = { owner: reopenOwner, epoch: reopenEpoch, path: reopenPath, active: true };
  const renderedReopenScope = reopenScope.current, reopenVersion = loadVersion.current;
  const reopenScopeCurrent = () => renderedReopenScope.active && reopenScope.current === renderedReopenScope
    && reopenOwner === verifiedSessionSubject() && reopenEpoch === verifiedSessionEpoch() && reopenPath === window.location.pathname;
  useEffect(() => {
    renderedReopenScope.active = true; pendingAction.current = false; setBusy(false);
    return () => { renderedReopenScope.active = false; };
  }, [renderedReopenScope]);
  async function reopenQuestions() {
    const sessionId = session?.id, revisionId = session?.current_revision?.id;
    const current = () => reopenScopeCurrent() && activeVenture.current === ventureId && loadVersion.current === reopenVersion;
    if (!current() || !reopenOwner || !sessionId || !revisionId || busy || pendingAction.current || jobId || syncError || reconciling || hydrating.current) return;
    pendingAction.current = true; setBusy(true); setNotice("");
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/idea/sessions/${encodeURIComponent(sessionId)}/reopen-questions`, {
        method: "POST", body: JSON.stringify({ revision_id: revisionId }),
      });
      if (!current()) return;
      if (!result.ok) { setNotice(apiFailure(result, "opening your skipped questions")); return; }
      setSession(result.data as Session); setPlanState(undefined); setAnswer(""); setSelectedOptions([]);
    } catch {
      if (current()) setNotice("The questions could not be opened. Refresh and try again.");
    } finally {
      if (reopenScopeCurrent()) { pendingAction.current = false; setBusy(false); }
    }
  }

  async function setCase(known: boolean) {
    if (!session || busy || pendingAction.current || syncError) return;
    pendingAction.current = true;
    setBusy(true); setNotice("");
    const result = await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/case`, {
      method: "POST", body: JSON.stringify({ case: known ? "know" : "explore" }),
    });
    if (activeVenture.current !== ventureId) return;
    setBusy(false);
    pendingAction.current = false;
    if (!result.ok) { setNotice(apiFailure(result, "saving your preference")); return; }
    setSession(result.data as Session);
  }

  async function beginDirectionCorrection() {
    if (!session || session.state !== "confirmed" || directionChange === null || !session.current_revision?.id || busy || pendingAction.current || syncError) return;
    pendingAction.current = true;
    setBusy(true); setNotice("");
    const result = await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/correct-direction`, {
      method: "POST", body: JSON.stringify({ case: directionChange ? "know" : "explore", revision_id: session.current_revision.id }),
    });
    if (activeVenture.current !== ventureId) return;
    pendingAction.current = false;
    setBusy(false);
    if (!result.ok) { setNotice(apiFailure(result, "starting a direction correction")); return; }
    setDirectionChange(null);
    setJobId(null);
    setFailedJob(null);
    setSession(result.data as Session);
    setPlanState(undefined);
  }

  async function continueToResearch(startResearch = true) {
    if (!session || busy || pendingAction.current || (jobId && !canConfirmPersonalWithValidation) || syncError) return;
    pendingAction.current = true;
    setBusy(true); setNotice("");
    if (startResearch && session.case === "explore" && !personalProject) {
      const current = latestValidationRuns((session.validation_runs || []).filter((run) => run.revision_id === session.current_revision?.id && run.state === "complete"));
      if (current.length !== validationMethods.length) {
        pendingAction.current = false; setBusy(false);
        setNotice("Validation is still incomplete. Check the saved job status before continuing.");
        return;
      }
      for (const run of current.filter((item) => item.founder_decision !== "accept")) {
        const decision = await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/validations/${run.id}/decide`, {
          method: "POST", body: JSON.stringify({ decision: "accept" }),
        });
        if (activeVenture.current !== ventureId) return;
        if (!decision.ok) {
          pendingAction.current = false; setBusy(false);
          setNotice(apiFailure(decision, "accepting validation findings"));
          return;
        }
        setSession(decision.data as Session);
      }
    }
    const result = await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/confirm`, {
      method: "POST", body: JSON.stringify({ revision_id: session.current_revision?.id,
        ...(!startResearch ? { start_research: false, accept_validation: true } : {}) }),
    });
    if (activeVenture.current !== ventureId) return;
    setBusy(false);
    if (!result.ok) { pendingAction.current = false; setNotice(apiFailure(result, !startResearch ? "confirming your idea" : personalProject ? "confirming the project" : "continuing to research")); return; }
    const next = result.data as { dossier_job_id?: string; session?: Session; next_href?: string };
    setSession(next.session || session); setJobId(next.dossier_job_id || null);
    if (!next.dossier_job_id) pendingAction.current = false;
    if (next.next_href === `/v/${ventureId}/context`) window.location.assign(next.next_href);
    else if (!startResearch) window.location.assign(`/v/${encodeURIComponent(ventureId)}/site`);
  }

  async function preparePersonalBuild() {
    if (!local || session?.state !== "confirmed" || session.project_intent !== "personal" || busy || syncError || pendingAction.current) return;
    const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    if (!subject) { setNotice("Restart your local app, reopen Idea, and select Build your project again."); return; }
    const current = () => activeVenture.current === ventureId && subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
    pendingAction.current = true;
    setBusy(true); setNotice("");
    const unconfirmed = "Your build could not be confirmed. Open Builds and refresh your saved work before trying again.";
    try {
      await ensureSession();
      if (!current()) return;
      const result = await api("/api/coding/dispatch", {
        method: "POST",
        body: JSON.stringify({ venture_id: ventureId, kind: "product", prompt: "Build the smallest useful local version of my confirmed personal project for my own use. Keep it private, with no market research, pricing, customer outreach, fundraising, or publishing." }),
      });
      if (!current()) return;
      if (!result.ok) {
        const existing = activeMacTaskReviewPath(result);
        if (existing?.startsWith(`/v/${encodeURIComponent(ventureId)}/device-work-orders/`)) {
          window.location.assign(existing);
          return;
        }
        const code = apiErrorCode(result);
        setNotice(code === "build_tool_needs_setup"
          ? "Open Coding tools below, connect your tool and allow Charlie to use it, then return to Idea to build your project."
          : code === "venture_paused" ? "Open All ventures, select Manage Ventures, and resume this venture before building."
          : code === "device_task_has_active_order" ? "Open Builds to review your active build before starting another."
          : result.status === 0 || result.status >= 500 ? unconfirmed : apiFailure(result, "starting your personal build"));
        return;
      }
      const order = result.data as { id?: unknown; venture_id?: string } | null;
      if (order?.venture_id !== ventureId || typeof order.id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(order.id)) {
        setNotice(unconfirmed);
        return;
      }
      window.location.assign(`/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(order.id)}`);
    } catch {
      if (current()) setNotice(unconfirmed);
    } finally {
      if (current()) { pendingAction.current = false; setBusy(false); }
    }
  }

  async function retryValidation() {
    if (!session || busy || pendingAction.current || jobId || syncError || failedJob?.kind !== "idea.validate") return;
    pendingAction.current = true;
    setBusy(true); setNotice(""); setFailedJob(null);
    const result = await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/validate`, {
      method: "POST", body: JSON.stringify({ revision_id: session.current_revision?.id }),
    });
    if (activeVenture.current !== ventureId) return;
    setBusy(false);
    if (!result.ok) { pendingAction.current = false; setNotice(apiFailure(result, "retrying validation")); return; }
    const next = result.data as { job_id?: string; session?: Session };
    setSession(next.session || session); setJobId(next.job_id || null);
    if (!next.job_id) pendingAction.current = false;
  }

  async function validateCurrentRevision() {
    if (!session || busy || pendingAction.current || jobId || syncError || !session.current_revision?.id) return;
    pendingAction.current = true;
    setBusy(true); setNotice("");
    const result = await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/validate`, {
      method: "POST", body: JSON.stringify({ revision_id: session.current_revision.id }),
    });
    if (activeVenture.current !== ventureId) return;
    setBusy(false);
    if (!result.ok) { pendingAction.current = false; setNotice(apiFailure(result, "validating your correction")); return; }
    const next = result.data as { job_id?: string; session?: Session };
    setSession(next.session || session); setJobId(next.job_id || null);
    if (!next.job_id) pendingAction.current = false;
  }

  async function retryClarification() {
    if (!session || busy || pendingAction.current || jobId || syncError || !(session.clarification_retry_available || session.credits_continue_job_id)) return;
    pendingAction.current = true;
    setBusy(true);
    const research = session.credits_continue_kind === "research.deep";
    const path = research ? `/api/ventures/${ventureId}/research/dossier/regenerate`
      : `/api/ventures/${ventureId}/idea/sessions/${session.id}/retry-clarification`;
    const result = await api(path, { method: "POST", body: JSON.stringify({ failed_job_id: session.credits_continue_job_id || undefined }) });
    if (activeVenture.current !== ventureId) return;
    setBusy(false);
    if (!result.ok) { pendingAction.current = false; setNotice(apiFailure(result, "retrying clarification")); return; }
    const next = result.data as { job_id?: string; session?: Session };
    setNotice(""); setFailedJob(null);
    setSession(next.session || { ...session, credits_continue_job_id: null, credits_continue_kind: null }); setJobId(next.job_id || null);
    if (!next.job_id) pendingAction.current = false;
  }

  async function reviewManually(patch: { problem: string; customer: string; offer: string }) {
    if (!session?.current_revision?.id || busy || pendingAction.current || jobId || syncError || !session.manual_review_available) return;
    pendingAction.current = true;
    setBusy(true); setNotice("");
    const result = await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/manual-review`, {
      method: "POST", body: JSON.stringify({ revision_id: session.current_revision.id, patch }),
    });
    if (activeVenture.current !== ventureId) return;
    pendingAction.current = false;
    setBusy(false);
    if (!result.ok) { setNotice(apiFailure(result, "saving your manual idea review")); return; }
    clearManualIdeaDraft(localStorage, ventureId, session.current_revision.id);
    setFailedJob(null);
    setSession(result.data as Session);
  }

  async function saveRevision(field: string) {
    if (!session || busy || pendingAction.current || jobId || syncError) return;
    pendingAction.current = true;
    setBusy(true); setNotice("");
    const value = listFields.has(field) ? revisionEdit.split("\n").map((line) => line.trim()).filter(Boolean) : revisionEdit.trim();
    const result = await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/revision`, {
      method: "PATCH", body: JSON.stringify({ patch: { [field]: value } }),
    });
    if (activeVenture.current !== ventureId) return;
    setBusy(false); pendingAction.current = false;
    if (!result.ok) { setNotice(apiFailure(result, "saving your correction")); return; }
    const scope = correctionScope("field", field);
    if (scope) clearIdeaCorrectionDraft(localStorage, scope);
    setEditingField(null);
    void load();
  }

  function correctionScope(target: "question" | "field", targetId: string): IdeaCorrectionScope | null {
    const workspaceId = venture?.workspace_id;
    const revisionId = session?.current_revision?.id;
    return workspaceId && revisionId ? { workspaceId, ventureId, revisionId, target, targetId } : null;
  }

  function cancelCorrection(target: "question" | "field", targetId: string) {
    const scope = correctionScope(target, targetId);
    if (scope) clearIdeaCorrectionDraft(localStorage, scope);
    setEditingQuestionId(null); setEditingField(null);
    setCorrectionChoices([]); setRevisionEdit("");
  }

  function openCorrection(field: string, value: unknown) {
    const scope = correctionScope("field", field);
    const saved = scope ? readIdeaCorrectionDraft(localStorage, scope) : null;
    setEditingQuestionId(null);
    setEditingField(field);
    setRevisionEdit(saved?.answer ?? editableIdeaAnswer(Array.isArray(value) ? value.join("\n") : typeof value === "string" ? value : ""));
    requestAnimationFrame(() => document.getElementById(`idea-field-${field}`)?.scrollIntoView({ behavior: "smooth", block: "center" }));
  }

  function changeCase(known: boolean) {
    if (!session || reconciling || session.state === "confirmed") return;
    // An in-flight local save has no durable job completion callback to drain
    // another queued toggle. Let that short mutation finish first.
    if (busy && !jobId) return;
    if (isWorking) {
      pendingCaseRef.current = known;
      setPendingCase(known);
      return;
    }
    void setCase(known);
  }

  function openQuestionCorrection(question: Turn, reply: Turn, field: string) {
    const options = savedIdeaQuestionOptions(question.options);
    const restored = savedIdeaChoiceAnswer(currentQuestionAnswer(reply.text, question.id, session?.current_revision?.structured), options);
    const scope = correctionScope("question", question.id);
    const saved = scope ? readIdeaCorrectionDraft(localStorage, scope) : null;
    setEditingField(null);
    setEditingQuestionId(question.id);
    setCorrectionChoices(saved?.selectedOptions ?? restored.selected);
    setRevisionEdit(saved?.answer ?? restored.freeText);
  }

  function updateCorrection(target: "question" | "field", targetId: string, answer: string, selectedOptions: string[]) {
    setRevisionEdit(answer);
    setCorrectionChoices(selectedOptions);
    const scope = correctionScope(target, targetId);
    if (scope) writeIdeaCorrectionDraft(localStorage, scope, { answer, selectedOptions });
  }

  async function saveQuestionCorrection(field: string, questionId: string) {
    const text = composeIdeaAnswer(correctionChoices, revisionEdit);
    if (!text.trim()) return;
    if (field === "proposed_options" && session?.state === "confirmed") {
      if (!session.current_revision?.id || busy || pendingAction.current || jobId || syncError) return;
      pendingAction.current = true; setBusy(true); setNotice("");
      const result = await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/correct-direction`, {
        method: "POST", body: JSON.stringify({ case: session.case, revision_id: session.current_revision.id }),
      });
      if (activeVenture.current !== ventureId) return;
      pendingAction.current = false; setBusy(false);
      if (!result.ok) { setNotice(apiFailure(result, "starting a direction review")); return; }
      const next = result.data as Session;
      const pending = pendingJourneyQuestion(next.state, next.turns);
      if (pending?.id) {
        writeIdeaDraft(localStorage, ventureId, pending.id, { answer: text, selectedOptions: [] });
        writeIdeaDraft(sessionStorage, ventureId, pending.id, { answer: text, selectedOptions: [] });
      }
      const scope = correctionScope("question", questionId);
      if (scope) clearIdeaCorrectionDraft(localStorage, scope);
      setEditingQuestionId(null); setSession(next); setAnswer(text); setSelectedOptions([]); setPlanState(undefined);
      requestAnimationFrame(() => document.getElementById("journey-action")?.scrollIntoView({ behavior: "smooth", block: "start" }));
      return;
    }
    if (field === "proposed_options") {
      if (await send(text, questionId)) {
        const scope = correctionScope("question", questionId);
        if (scope) clearIdeaCorrectionDraft(localStorage, scope);
        setEditingQuestionId(null);
      }
      return;
    }
    if (!editableFields.includes(field)) return;
    if (!session || busy || pendingAction.current || jobId || syncError) return;
    pendingAction.current = true; setBusy(true); setNotice("");
    const value = listFields.has(field)
      ? [...correctionChoices, ...revisionEdit.split("\n").map((line) => line.trim()).filter(Boolean)]
      : text;
    const result = session.state === "confirmed"
      ? await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/correct-answer`, {
        method: "POST", body: JSON.stringify({ revision_id: session.current_revision?.id, question_id: questionId, text, patch: { [field]: value } }),
      })
      : await api(`/api/ventures/${ventureId}/idea/sessions/${session.id}/revision`, {
        method: "PATCH", body: JSON.stringify({ patch: { [field]: value }, answer_correction: { question_id: questionId, text } }),
      });
    if (activeVenture.current !== ventureId) return;
    setBusy(false); pendingAction.current = false;
    if (!result.ok) { setNotice(apiFailure(result, "saving your correction")); return; }
    const scope = correctionScope("question", questionId);
    if (scope) clearIdeaCorrectionDraft(localStorage, scope);
    setEditingQuestionId(null); setEditingField(null); void load();
  }

  const idea = session?.idea_summary?.trim() || venture?.idea_summary?.trim() || session?.current_revision?.summary?.trim() || venture?.raw_idea?.trim();
  const displayNotice = session?.credits_continue_job_id ? "You can continue from your saved work. Select Continue." : notice;
  const lastQuestion = session && canConfirmValidatedIdea(session) ? undefined : pendingJourneyQuestion(session?.state || "", session?.turns || []);
  useEffect(() => {
    if (!lastQuestion?.id) return;
    const saved = readPersistentIdeaDraft(localStorage, sessionStorage, ventureId, lastQuestion.id);
    setAnswer(saved.answer);
    setSelectedOptions(saved.selectedOptions);
  }, [ventureId, lastQuestion?.id]);
  useEffect(() => {
    if (!session?.manual_review_available || !session.current_revision?.id) return;
    const saved = readManualIdeaDraft(localStorage, ventureId, session.current_revision.id);
    setManualDraft(saved || {
      problem: String(session.current_revision.structured?.problem || idea || ""),
      customer: String(session.current_revision.structured?.customer || ""),
      offer: String(session.current_revision.structured?.offer || ""),
    });
  }, [ventureId, session?.manual_review_available, session?.current_revision?.id, idea]);
  function updateManualDraft(field: keyof ManualIdeaDraft, value: string) {
    const next = { ...manualDraft, [field]: value };
    setManualDraft(next);
    if (session?.current_revision?.id) writeManualIdeaDraft(localStorage, ventureId, session.current_revision.id, next);
  }
  const questionOptions = lastQuestion?.options && !Array.isArray(lastQuestion.options) ? lastQuestion.options : null;
  const selectionMode = questionSelectionMode(questionOptions?.selection_mode);
  const composedAnswer = composeIdeaAnswer(selectedOptions, answer);
  const recommendedAnswer = questionOptions?.recommended_option ? composeIdeaAnswer([questionOptions.recommended_option], answer, true) : "";
  const previousAnswers = previousIdeaAnswers(session?.turns || []);
  useEffect(() => {
    const previous = previousAnswers.at(-1);
    if (!editPrevious || !previous?.field || previousOpened.current === previous.question.id) return;
    previousOpened.current = previous.question.id;
    openPreviousAnswerReview(answerReview.current);
    openQuestionCorrection(previous.question, previous.reply, previous.field);
  }, [editPrevious, session?.current_revision?.id, session?.turns.length]);
  const currentValidations = (session?.validation_runs || []).filter((run) => run.revision_id === session?.current_revision?.id && run.state === "complete");
  const reviewValidations = latestValidationRuns(currentValidations);
  const suggestedExperiment = founderExperimentContext(session?.current_revision?.structured);
  const activity = ideaJourneyActivity({ jobId, busy, reconciling,
    hasNextStep: Boolean(lastQuestion || session?.state === "reviewing" || (session?.state === "validating" && reviewValidations.length === validationMethods.length)) });
  const isWorking = activity !== "idle";
  const skipQuestions = () => send(questionOptions?.phase === "gstack" ? GSTACK_SKIP : "Not sure");
  useEffect(() => {
    if (!lastQuestion || chatFirstEntry || isWorking) return;
    const frame = requestAnimationFrame(() => document.getElementById("idea-question")?.scrollIntoView({ block: "start" }));
    return () => cancelAnimationFrame(frame);
  }, [ventureId, lastQuestion?.id, chatFirstEntry, isWorking, ready, displayNotice]);
  const researching = !busy && job.job?.kind === "research.deep";
  const personalProject = session?.project_intent === "personal";
  const personalReview = personalProjectReview(session?.current_revision?.structured, idea);
  const ideaStatus = { state: isWorking ? "working" : displayNotice ? "failed" : session ? "needs_you" : "working",
    activity: isWorking ? jobId && job.needsRecovery ? job.delayed && !job.error && !job.stalled ? "Background work is taking longer" : "Background status unavailable" : activity === "checking" ? "Checking your saved journey" : busy ? "Saving your answer" : researching ? "Researching your market" : session?.state === "validating" ? "Reviewing your saved idea" : "Preparing the next question or review"
      : displayNotice || (lastQuestion ? "Answer the next question about your idea" : session?.next_href === `/v/${ventureId}/context` && (session.state === "confirmed" || reviewValidations.length === validationMethods.length) ? "Review your startup and priorities" : session?.state === "confirmed" ? personalProject ? "Review a local build" : "Review your saved research" : session?.state === "reviewing" || reviewValidations.length ? "Review your saved idea" : "Preparing the next question or review"),
    action: session?.credits_continue_job_id ? { label: "Continue", onClick: () => void retryClarification(), disabled: busy || checkingStatus || reconciling || !!jobId || !!syncError } : undefined };
  const canConfirmPersonalWithValidation = personalProject && session?.state === "validating" && job.job?.kind === "idea.validate";
  const steps = personalProject ? personalSteps : startupSteps;
  const questionBudget = Math.max(1, session?.question_budget || 1);
  const questionsAsked = Math.min(questionBudget, Math.max(0, session?.questions_asked || 0));
  const currentStep = ideaJourneyProgress(session?.state,
    Boolean(session?.validation_runs?.some((run) => run.revision_id === session.current_revision?.id && run.state === "complete")), planState);

  return (
    <AppShell title="Your journey" ventureId={ventureId} ideaStatus={ideaStatus} clarificationSkip={lastQuestion && !chatFirstEntry ? {
      skip: skipQuestions, disabled: busy || isWorking || !!syncError || reconciling || hydrating.current,
    } : null}>
      <main className={styles.page} data-journey-step={session ? ideaObservationStep(session.state) || undefined : "idea"} data-journey-venture={ventureId}>
        {chatFirstEntry && <div className={styles.chatFirstIntro}>
          <p className={styles.eyebrow}>Your conversation</p>
          <h2>Continue in chat</h2>
          <p>Your idea and next step are saved in the conversation. The guided details are available here when you need them.</p>
        </div>}
        <details className={styles.journeyDetails} open={!chatFirstEntry}>
          <summary className={chatFirstEntry ? undefined : styles.hiddenSummary}>View guided details</summary>
        <header className={styles.header}>
          <p className={styles.eyebrow}>Supymem journey</p>
          <h1>{venture?.name || (personalProject ? "Your project" : "Your company")}</h1>
          <p>{personalProject ? "We’ll clarify the smallest useful version of your project and what it needs to work." : `We will shape your idea into a focused, testable plan for ${JOURNEY_OUTCOME}.`}</p>
          {lastQuestion && <p className={styles.chatHelp}>Answer the guided question below to define this idea.</p>}
        </header>
        <ol className={styles.progress} aria-label="Journey progress">
          {steps.map((step, index) => <li key={step} className={index < currentStep ? styles.complete : index === currentStep ? styles.current : ""}>{step}</li>)}
        </ol>
        {session && currentStep === 0 && (lastQuestion || previousAnswers.length > 0) && <div className={styles.questionProgress} role="status">
          <strong>Help me understand your idea better</strong>
          <span>{previousAnswers.length} {previousAnswers.length === 1 ? "answer saved" : "answers saved"} in your idea phase.</span>
          <span>I may need only a few questions when your idea is clear. Your answer is saved before I prepare the next one.</span>
        </div>}

        {idea && <section className={styles.ideaCard}>
          <p className={styles.label}>{personalProject ? "Your project" : "Your idea"}</p>{idea.split(/\r?\n/).map((line, index) => {
            const row = /^([^:]+):\s*(.*)$/.exec(line);
            return <p key={index}>{row ? <><strong>{row[1]}:</strong> {row[2]}</> : line}</p>;
          })}
          <details className={styles.details}><summary>Advanced</summary>
          <label className={styles.preference}>
            <input type="checkbox" checked={directionChange ?? pendingCase ?? session?.case !== "explore"} disabled={!session || reconciling || !!syncError || (busy && !jobId)}
              onChange={(event) => session?.state === "confirmed" ? setDirectionChange(event.target.checked) : changeCase(event.target.checked)} />
            I already have a defined product direction
          </label>
          </details>
          {pendingCase !== null && <p role="status">Your direction change is saved on this screen and will apply as soon as the current step finishes.</p>}
          {session?.state === "confirmed" && directionChange !== null && <div className={styles.notice}>
          <p>{personalProject ? "Changing direction starts a new idea review. Confirm the new direction before preparing another local build. Work already running keeps its saved scope." : "Changing direction starts a new idea review. Your confirmed idea, research, and plans remain in history. They still describe the previous direction; new research and a new plan will be needed after you confirm the change. Work already running will continue with its saved inputs."}</p>
            <button type="button" disabled={busy} onClick={() => void beginDirectionCorrection()}>Start new direction review</button>
            <button type="button" disabled={busy} onClick={() => setDirectionChange(null)}>Keep current direction</button>
          </div>}
          {session?.state !== "confirmed" && Boolean(session?.current_revision?.structured?._corrects_revision_id) &&
          <p role="status">{personalProject ? "You are reviewing a new direction. Confirm this revision before preparing a local build." : "You are reviewing a new direction. Earlier research and plans remain in history and apply to the previously confirmed idea. Confirm this revision to start fresh research before making a new plan."}</p>}
        </section>}

        {session?.gstack_answers?.some(answer => answer.assumed) && !lastQuestion && !isWorking && <section className={styles.notice}>
          <p>Some questions were skipped or used assumptions. You can answer them now.</p>
          <button type="button" data-usage-control="idea_gstack_reopen" disabled={busy || !!syncError} onClick={() => void reopenQuestions()}>Answer the questions now</button>
        </section>}
        {session && <section role="log" aria-label="Idea conversation" aria-live="polite">
          {(session.turns || []).filter(turn => turn.kind !== "opening" && turn.id !== lastQuestion?.id).map(turn =>
            <article key={turn.id}><strong>{turn.role === "founder" ? "You" : "Supymem"}</strong><p>{turn.role === "founder" ? founderChoiceWords(turn.text) : founderReviewText(turn.text)}</p></article>)}
        </section>}
        <div id="journey-action" tabIndex={-1} />
        {!ready && <section className={styles.focus} role="status"><h2><span className={styles.spinner} aria-hidden="true" />Preparing your journey</h2><p>Connecting your company and its saved idea.</p></section>}
        {syncError && <ErrorNotice error={syncError} as="section" className={styles.notice} source="idea.sync"><p>{syncError}</p>
          <button type="button" disabled={checkingStatus} onClick={() => void checkSavedStatus()}>Try again</button></ErrorNotice>}
        {statusCheckedAt && !checkingStatus && !syncError && <section className={styles.notice} role="status"><p>Checked at {statusCheckedAt}. This is the latest saved status.</p></section>}
        {displayNotice && <ErrorNotice error={displayNotice} as="section" className={styles.notice} source="idea.notice"><h2>Your work is safe</h2><p>{displayNotice}</p>
          {session?.credits_continue_job_id && <button className="primary" type="button" data-usage-control="idea_credits_continue" disabled={busy || checkingStatus || reconciling || !!jobId || !!syncError} onClick={() => void retryClarification()}>Continue</button>}
          {session && !jobId && <button type="button" data-usage-control="idea_status_check" disabled={busy || checkingStatus} onClick={() => void checkSavedStatus()}>{checkingStatus ? "Checking saved status…" : session.state === "validating" ? "Check saved validation status" : "Check saved journey status"}</button>}
          {!personalProject && failedJob?.kind === "idea.validate" && ["research_output_truncated", "research_invalid_output"].includes(failedJob.error_code || "") &&
            <button type="button" data-usage-control="idea_validation_retry" onClick={() => void retryValidation()}>Retry validation with saved answers</button>}
          {!personalProject && failedJob?.kind === "idea.validate" && failedJob.error_code === "research_outcome_uncertain" &&
            <><p>Your earlier work remains saved. Starting again makes one new attempt using those answers.</p>
              <button type="button" data-usage-control="idea_validation_retry" disabled={busy} onClick={() => void retryValidation()}>{busy ? "Starting validation…" : "Start a new validation attempt"}</button></>}
          {personalProject && session?.state === "validating" && failedJob?.kind === "idea.validate" &&
            <><p>The earlier validation attempt and any usage remain saved. A personal project can continue from its saved idea without another market validation call.</p>
              <button type="button" onClick={() => { setNotice(""); setFailedJob(null); }}>Review personal project</button></>}
          {failedJob?.kind === "research.deep" &&
            <Link href={`/v/${ventureId}/research`}>Review research and recovery options</Link>}
        </ErrorNotice>}
        {ready && session?.manual_review_available && !isWorking && !syncError && <section className={styles.focus}>
          <h2>Continue with your saved answers</h2>
          <p>{session.clarification_invalid_output_seen
            ? `Your saved answers are unchanged. ${session.clarification_retry_available ? "Select Retry once with saved answers to retry." : "Complete the review form below and select Save for review without a model call."}`
            : "The model service was offline. Review your saved idea yourself to continue."} You can enter the idea yourself without another model call.</p>
          {session.clarification_retry_available && <button type="button" data-usage-control="idea_clarification_retry" onClick={() => void retryClarification()}>Retry once with saved answers</button>}
          <form className={styles.composer} key={session.current_revision?.id} onSubmit={(event) => {
            event.preventDefault();
            const fields = new FormData(event.currentTarget);
            void reviewManually({ problem: String(fields.get("problem") || "").trim(), customer: String(fields.get("customer") || "").trim(), offer: String(fields.get("offer") || "").trim() });
          }}>
            <h3>Review your saved idea yourself</h3>
            <label htmlFor="manual-problem">Problem or starting idea</label>
            <textarea id="manual-problem" name="problem" required maxLength={2000} value={manualDraft.problem} onChange={(event) => updateManualDraft("problem", event.target.value)} />
            <label htmlFor="manual-customer">{personalProject ? "Who will use it" : "First customer"}</label>
            <textarea id="manual-customer" name="customer" required maxLength={2000} value={manualDraft.customer} onChange={(event) => updateManualDraft("customer", event.target.value)} />
            <label htmlFor="manual-offer">{personalProject ? "Smallest useful version" : "Initial offer"}</label>
            <textarea id="manual-offer" name="offer" required maxLength={2000} value={manualDraft.offer} onChange={(event) => updateManualDraft("offer", event.target.value)} />
            <button type="submit">Save for review without a model call</button>
          </form>
        </section>}
        {ready && isWorking && <section className={styles.focus} role={jobId && job.needsRecovery ? "alert" : "status"}><h2>{!(jobId && job.needsRecovery) && <span className={styles.spinner} aria-hidden="true" />}{ideaStatus.activity}</h2>
          {jobId && job.needsRecovery ? <><p>{job.label} Your saved work may still be running.</p>
            {job.error ? <button type="button" onClick={job.retry}>Try again</button>
              : <button type="button" data-usage-control="idea_status_check" disabled={checkingStatus} onClick={() => { pendingAction.current = false; setJobId(null); void checkSavedStatus(); }}>{checkingStatus ? "Checking saved status…" : "Check saved status"}</button>}</>
            : <><p>{activity === "checking" ? "Reading your saved answers and background status before showing the next step." : busy ? "Saving what you chose. Keep this page open until the response is recorded." : researching ? "Supymem is searching for competitors, sources and evidence. You can leave this page; the research page shows the result when it finishes." : "Your answer is saved. Supymem is using it with your earlier answers; you can leave this page while the hosted task continues."}</p>
              {jobId && <p className={styles.workDetail}>{job.label}</p>}</>}
          {session && !researching && activity !== "checking" && <p className={styles.workDetail}>Reviewing your answers.</p>}
          {canConfirmPersonalWithValidation && <div>
            <p>Your personal project does not need this older market review. You can confirm the saved direction now. The earlier job and any provider usage remain recorded; confirming will not start another validation or research request.</p>
            <button className="primary" type="button" data-usage-control="idea_confirm_personal" disabled={busy || !!syncError} onClick={() => void continueToResearch()}>Confirm personal project</button>
          </div>}
        </section>}

        {ready && session && !isWorking && !displayNotice && !session.manual_review_available && (
          <>
            {session.state === "reviewing" && session.current_revision?.structured?._manual_recovery ? <section className={styles.focus}>
              <p className={styles.label}>Manual idea review</p><h2>Your saved answers are ready to review</h2>
              <p>Review or edit the fields below. {session.case === "explore" && !personalProject ? "Validate this direction before confirming it." : "Confirm this direction when it reflects what you want to pursue."}</p>
              <button className="primary" type="button" data-usage-control={session.case === "explore" && !personalProject ? "idea_validate_revision" : personalProject ? "idea_confirm_personal" : "idea_continue_research"} onClick={() => void (session.case === "explore" && !personalProject ? validateCurrentRevision() : continueToResearch())}>
                {session.case === "explore" && !personalProject ? "Validate this direction" : personalProject ? "Confirm personal project" : "Confirm and continue to research"}
              </button>
            </section> : personalProject && ["reviewing", "validating"].includes(session.state) ? <section className={styles.focus}>
              <p className={styles.label}>Project review</p><h2>Review your personal project</h2>
              <p>This review uses your saved answers for a project you want to use yourself. It is not a market verdict.</p>
              <p><strong>Your goal:</strong> {personalReview.goal}</p>
              {personalReview.firstVersion && <p><strong>Smallest first version:</strong> {personalReview.firstVersion}</p>}
              <p><strong>Next test:</strong> Try the smallest working flow on one of your real tasks. Record whether it works and what you would change.</p>
              <p>Confirming saves this direction and opens a reviewed local build next. It will not start market research or repeat an earlier paid assessment.</p>
              <button className="primary" type="button" data-usage-control="idea_confirm_personal" onClick={() => void continueToResearch()}>Confirm personal project</button>
            </section> : session.state === "reviewing" && !reviewValidations.length ? <section className={styles.focus}>
              <p className={styles.label}>Idea review</p><h2>Review your saved idea</h2>
              <p>Check your idea and saved answers before continuing. Validation has not run for this direction yet.</p>
              <button className="primary" type="button" data-usage-control="idea_validate_revision" onClick={() => void validateCurrentRevision()}>Validate this direction</button>
            </section> : lastQuestion ? <section id="idea-question" className={styles.focus}>
              {questionOptions?.phase === "gstack" ? <>
                <p className={styles.label}>{GSTACK_HEADING}</p>
                <p className={styles.explanation}>{GSTACK_ABOUT}</p>
                <p className={styles.workDetail}>Question {questionOptions.index} of {questionOptions.total} · {founderReviewText(questionOptions.gstack_name || "")}</p>
              </> : <p className={styles.label}>Question {Math.max(1, questionsAsked)}</p>}
              <h2>{lastQuestion.text}</h2>
              {questionOptions?.why && <p className={styles.explanation}>{founderReviewText(questionOptions.why)}</p>}
              <form className={styles.composer} onSubmit={(event) => { event.preventDefault(); void send(composedAnswer); }}>
                {questionOptions?.options && questionOptions.options.length > 0 && <fieldset className={styles.options} data-detailed={detailed || undefined}>
                  <legend>{selectionMode === "multiple" ? "Select all that apply" : "Select one option"}</legend>
                  {questionOptions.options.map((option, i) => <label key={option} className={styles.option}>
                    <input type={selectionMode === "multiple" ? "checkbox" : "radio"} name="idea-choice"
                      checked={selectedOptions.includes(option)} onChange={() => setSelectedOptions((selected) => {
                        const next = toggleIdeaChoice(selected, option, selectionMode);
                        if (lastQuestion?.id) {
                          writeIdeaDraft(localStorage, ventureId, lastQuestion.id, { answer, selectedOptions: next });
                          writeIdeaDraft(sessionStorage, ventureId, lastQuestion.id, { answer, selectedOptions: next });
                        }
                        return next;
                      })} />
                    <span className={styles.choiceText}>
                      <ChoiceText option={option} label={option} />
                      {selectedOptions.includes(option) && <span className={styles.selected}><span aria-hidden="true">✓ </span>Selected</span>}
                      {option === questionOptions.recommended_option && <span className={styles.choiceDetail}><span className={styles.recommended}>Recommended</span>{questionOptions.recommendation_reason ? `: ${questionOptions.recommendation_reason}` : ""}</span>}
                    </span>
                  </label>)}
                </fieldset>}
                {questionOptions?.prefill && <button type="button" className="ghost" disabled={busy || Boolean(jobId)} onClick={() => {
                  const next = questionOptions.prefill || "";
                  setAnswer(next);
                  if (lastQuestion?.id) {
                    writeIdeaDraft(localStorage, ventureId, lastQuestion.id, { answer: next, selectedOptions });
                    writeIdeaDraft(sessionStorage, ventureId, lastQuestion.id, { answer: next, selectedOptions });
                  }
                }}>Use: {questionOptions.prefill}</button>}
                {(!questionOptions || questionOptions.allow_free_text !== false) && <>
                  <label htmlFor="idea-answer">{questionOptions?.options?.length ? "Add context or answer in your own words" : "Your answer"}</label>
                  <textarea id="idea-answer" value={answer} maxLength={IDEA_ANSWER_MAX_CHARS} onChange={(event) => {
                    const next = event.target.value;
                    setAnswer(next);
                    if (lastQuestion?.id) {
                      writeIdeaDraft(localStorage, ventureId, lastQuestion.id, { answer: next, selectedOptions });
                      writeIdeaDraft(sessionStorage, ventureId, lastQuestion.id, { answer: next, selectedOptions });
                    }
                  }} placeholder="Answer in your own words" />
                </>}
                {composedAnswer.trim().length > IDEA_ANSWER_MAX_CHARS
                  ? <p role="status">That answer is too long. Shorten your text or choose fewer options.</p>
                  : recommendedAnswer.trim().length > IDEA_ANSWER_MAX_CHARS && <p role="status">To use the recommended option, shorten your text.</p>}
                <DictateButton scope={`${ventureId}:${lastQuestion?.id || ""}`} value={answer} onChange={(value) => {
                  setAnswer(value);
                  if (lastQuestion?.id) writeIdeaDraft(localStorage, ventureId, lastQuestion.id, { answer: value, selectedOptions });
                }} disabled={busy || Boolean(jobId)} />
                <div className={styles.answerActions}>
                  {questionOptions && ["offer", "customer"].includes(questionOptions.field || "") && <button type="button" className="ghost" disabled={busy || Boolean(jobId)} onClick={() => void send([composedAnswer.trim(), composedAnswer.trim() ? "Skip this question" : "Not sure"].filter(Boolean).join("\n"))}>{composedAnswer.trim() ? "I'm not sure, continue with what I shared" : "Not sure, suggest some"}</button>}
                  {questionOptions?.recommended_option && <button type="button" className="ghost" data-usage-control="idea_use_recommended" disabled={!canSendIdeaAnswer(recommendedAnswer, false)} onClick={() => void send(recommendedAnswer)}>I'm not sure, suggest one for me</button>}
                  {previousAnswers.length > 0 && <button type="button" className="ghost" data-usage-control="idea_previous_answer" onClick={() => openPreviousAnswerReview(answerReview.current)}>Back to previous answers</button>}
                  {questionOptions?.phase === "gstack" && <button type="button" data-usage-control="idea_gstack_skip" onClick={() => void skipQuestions()}>Skip the questions</button>}
                  <button className="primary" type="submit" data-usage-control="idea_options_submit" disabled={!canSendIdeaAnswer(composedAnswer, false)}>Submit</button>
                </div>
              </form>
            </section> : currentValidations.length > 0 && session.state !== "confirmed" ? <section className={styles.focus}>
              <p className={styles.label}>Validation complete</p>
              <h2>Your idea review</h2>
              <h3>What we found</h3>
              <p>From your saved answers and notes:</p>
              {[...new Set(reviewValidations.flatMap(run => (run.report.presentation?.findings || []).map(finding => finding.assessment)))].map(text =>
                <p key={text}>{text}</p>)}
              <h3>Your opening</h3>
              <p>{reviewValidations[0]?.report.presentation?.opening || "Your best opening: start with one reachable user group and one recurring task, positioned around that single useful result. Use a focused landing page to test this angle."}</p>
              {reviewValidations[0]?.report.presentation?.options.length ? <div>
                <h3>Options to explore</h3>
                <ul>{reviewValidations[0].report.presentation.options.map(option => <li key={option}>{option}</li>)}</ul>
                <p>Choose a direction in your saved answers below.</p>
              </div> : null}
              <h3>What I'd do next</h3>
              {reviewValidations[0]?.report.proposed_tests?.length ? <div>
                {reviewValidations[0].report.proposed_tests.map((test, index) => <div key={`${index}:${test.hypothesis}`}>
                  <p><strong>Hypothesis:</strong> {test.hypothesis}</p>
                  <p><strong>Try:</strong> {test.action}</p>
                  <p><strong>Signal to look for:</strong> {test.success_measure}</p>
                  {startedTests[test.hypothesis] ? <p role="status">Started. Track it on the <Link href={`/v/${ventureId}/experiments`}>Experiments</Link> page.</p>
                    : <button type="button" onClick={() => void startTest(test)}>Start this test</button>}
                </div>)}
              </div> : suggestedExperiment ? <div>
                <p>Start with a small experiment based on your saved answers.</p>
                <p><strong>{personalProject ? "Who uses it" : "Your stated customer"}:</strong> {suggestedExperiment.customer}</p>
                <p><strong>Your stated problem:</strong> {suggestedExperiment.problem}</p>
                <p><strong>Your stated offer:</strong> {suggestedExperiment.offer}</p>
                <p><strong>Try:</strong> {personalProject ? "Build or sketch the smallest working flow for your own use, then try it once and record what breaks or feels useful." : "Ask one person matching that customer description about a recent instance of the problem. Show the offer, then ask whether they would commit to a pilot and pay if it is a paid product."}</p>
                <p><strong>Signal to look for:</strong> {personalProject ? "Record whether the flow works for your task and what you would change. This is a usability check, not market validation." : "Record the actual example and a concrete commitment or refusal. Neither response alone proves the whole market."}</p>
              </div> : <p>I can research the closest alternatives and their pricing, then help you prepare a focused landing page.</p>}
              {session.case === "explore" && <p>You can keep this opening or choose a different direction in your saved answers below.</p>}
              <p>{personalProject ? "Confirming this project saves your direction. You can then build it with your connected coding tool without running market research." : session.next_href === `/v/${ventureId}/context` ? "Review your saved idea and answers, choose your priorities, then book a first call. Nothing starts automatically." : "Continuing starts a research run. It does not build or connect external services yet."}</p>
              <button className="primary" type="button" disabled={busy || !!jobId || !!syncError} data-usage-control={personalProject ? "idea_confirm_personal" : "idea_continue_research"} onClick={() => void continueToResearch(session.next_href !== `/v/${ventureId}/context`)}>{personalProject ? "Confirm personal project" : session.next_href === `/v/${ventureId}/context` ? "Confirm your idea" : session.current_revision?.structured?._gstack_reopened_from ? "Run research again" : session.case === "explore" ? "Accept findings and continue to research" : "Continue to research"}</button>
              {!personalProject && session.next_href !== `/v/${ventureId}/context` && canConfirmValidatedIdea(session) && <>
                <p>Research is optional. Accept these findings without starting research. Build your page separately on Website when ready.</p>
                <button className="secondary" type="button" disabled={busy || !!jobId || !!syncError} data-usage-control="idea_confirm_for_website" onClick={() => void continueToResearch(false)}>Build my landing page now</button>
              </>}
            </section> : session.state === "confirmed" ? <section className={styles.focus}>
              <p className={styles.label}>Idea confirmed</p><h2>Your idea is saved for the next stages.</h2>
              {personalProject ? <>
                {local ? <>
                  <p>Your connected coding tool builds the smallest useful version for your own use. Your project stays private.</p>
                  <button className="primary" type="button" data-usage-control="idea_review_local_build" disabled={busy || !!syncError} onClick={() => void preparePersonalBuild()}>{busy ? "Starting your build…" : "Build your project"}</button>
                  <p><Link href="/settings/local#coding-tools">Open Coding tools</Link> to connect your tool or check its status.</p>
                </> : <p>Open this venture in your local app and connect your coding tool under Settings &gt; Keys and connections &gt; Coding tools to build your project.</p>}
              </> : <>
                <p>Your saved validation and research reflect the evidence available when they ran. Improvements to Supymem apply to this venture, but they do not rewrite earlier findings automatically.</p>
                <p>Review the saved evidence or choose a new, quoted research run if you want an updated assessment of this direction.</p>
                {session?.next_href === `/v/${ventureId}/context` && <Link href={session.next_href}>Review answers and priorities</Link>}
                <Link href={`/v/${ventureId}/research`}>Review or refresh research</Link>
                <Link href={`/v/${ventureId}/site`}>Build my landing page now</Link>
                {planState && <p><Link href={`/v/${ventureId}/plan`}>View saved plan</Link></p>}
              </>}
            </section> : session.current_revision?.structured?._corrects_revision_id && session.current_revision?.id ? <section className={styles.focus}>
              <p className={styles.label}>Answer correction saved</p><h2>Review your updated idea</h2>
              <p>{personalProject ? "Review the corrected personal project, then confirm it to prepare another local build. Your edit did not start market validation or research." : "Your earlier validation, research, and plans describe the previous confirmed revision. Starting validation creates a new assessment for this revision. No validation or research started when you saved the answer."}</p>
              <button className="primary" type="button" data-usage-control={personalProject ? "idea_confirm_personal" : "idea_validate_revision"} onClick={() => void (personalProject ? continueToResearch() : validateCurrentRevision())}>{personalProject ? "Confirm personal project" : "Validate updated idea"}</button>
            </section> : (session.validation_runs || []).length > 0 && session.current_revision?.id ? <section className={styles.focus}>
              <p className={styles.label}>Correction saved</p><h2>{personalProject ? "Review your updated project" : "Validate your updated idea"}</h2>
              <p>{personalProject ? "Your edit is saved. Confirm the corrected project to continue toward a reviewed local build." : "Your earlier validation belongs to a previous revision. Run validation for this correction before continuing."}</p>
              <button className="primary" type="button" data-usage-control={personalProject ? "idea_confirm_personal" : "idea_validate_revision"} onClick={() => void (personalProject ? continueToResearch() : validateCurrentRevision())}>{personalProject ? "Confirm personal project" : "Validate updated idea"}</button>
            </section> : <p role="status">{lastQuestion ? "Your next question is waiting in the chat." : "Preparing your next step…"}</p>}
          </>
        )}

        {session && session.current_revision?.structured && <details ref={answerReview} className={styles.details}>
          <summary>Review and revise previous answers</summary>
          <p>{session.state === "confirmed"
            ? personalProject
              ? "Saving a revised answer starts a new project review. Your confirmed idea and earlier work stay in history. confirm the revision before preparing another local build."
              : "Saving a revised answer starts a new idea review. Your confirmed idea, research, and plans stay in history for the earlier revision. validation and new research run only when you choose them."
            : personalProject
              ? "Corrections update the current project review without starting market validation. Earlier conversation messages remain in history."
              : "Corrections update the current idea revision. Earlier conversation messages remain in the history. Earlier validation belongs to the previous revision; run it again before relying on its findings."}</p>
          {previousAnswers.map(({ question, reply, field }) => <div key={question.id} className={styles.revisionField}>
            <strong>{question.text}</strong>
            <p>Your answer: {currentQuestionAnswer(reply.text, question.id, session.current_revision?.structured)}</p>
            {currentQuestionAnswer(reply.text, question.id, session.current_revision?.structured) !== reply.text && <p>Original answer: {reply.text}</p>}
            {savedIdeaQuestionOptions(question.options).length > 0 && <p>Original choices: {savedIdeaQuestionOptions(question.options).join(" · ")}</p>}
            {savedIdeaQuestionOptions(question.options).length === 0 && <p>This saved question has no selectable choices. You can revise its answer in your own words.</p>}
            {field && (editableFields.includes(field) || field === "proposed_options") &&
              <button type="button" disabled={isWorking || !!syncError} onClick={() => openQuestionCorrection(question, reply, field)}>
                Change this answer
              </button>}
            {field === "proposed_options" && session.state === "confirmed" && <p>Saving this choice starts a new direction review with your selection as a draft. Select Continue when you want to clarify that direction.</p>}
            {editingQuestionId === question.id && field && <form className={styles.composer} onSubmit={(event) => { event.preventDefault(); void saveQuestionCorrection(field, question.id); }}>
              {savedIdeaQuestionOptions(question.options).length > 0 ? <fieldset className={styles.options} data-detailed={detailed || undefined}>
                <legend>{questionSelectionMode(question.options && !Array.isArray(question.options) ? question.options.selection_mode : undefined) === "multiple" ? "Select all that apply" : "Select one option"}</legend>
                {savedIdeaQuestionOptions(question.options).map((option, i) => <label key={option} className={styles.option}>
                  <input type={questionSelectionMode(question.options && !Array.isArray(question.options) ? question.options.selection_mode : undefined) === "multiple" ? "checkbox" : "radio"}
                    name={`correction-${question.id}`} checked={correctionChoices.includes(option)}
                    onChange={() => updateCorrection("question", question.id, revisionEdit,
                      toggleIdeaChoice(correctionChoices, option, questionSelectionMode(question.options && !Array.isArray(question.options) ? question.options.selection_mode : undefined)))} />
                  <ChoiceText option={option} label={shortOptionLabel(option, savedIdeaOptionLabels(question.options), i)} />
                </label>)}
              </fieldset> : null}
              {(!question.options || Array.isArray(question.options) || question.options.allow_free_text !== false) && <>
                <label htmlFor={`correction-${question.id}`}>Additional context or your own answer</label>
                <textarea id={`correction-${question.id}`} value={revisionEdit} onChange={(event) => updateCorrection("question", question.id, event.target.value, correctionChoices)} maxLength={4000} />
              </>}
              <div className={styles.answerActions}>
                <button type="submit" disabled={isWorking || !!syncError || !composeIdeaAnswer(correctionChoices, revisionEdit).trim()}>{session.state === "confirmed" && field === "proposed_options" ? "Start review with this choice" : "Save correction"}</button>
                <button type="button" onClick={() => cancelCorrection("question", question.id)}>Cancel</button>
              </div>
            </form>}
          </div>)}
          <h3>Current understanding</h3>
          {editableFields.map((field) => {
            const value = session.current_revision?.structured?.[field];
            return <div key={field} id={`idea-field-${field}`} className={styles.revisionField}>
              <strong>{field.replaceAll("_", " ")}</strong>
              {editingField === field ? <form onSubmit={(event) => { event.preventDefault(); void saveRevision(field); }}>
                <textarea value={revisionEdit} onChange={(event) => updateCorrection("field", field, event.target.value, [])} maxLength={2000} aria-label={`Correct ${field.replaceAll("_", " ")}`} />
                <button type="submit" disabled={isWorking || !!syncError}>Save correction</button>
                <button type="button" onClick={() => cancelCorrection("field", field)}>Cancel</button>
              </form> : <><p>{Array.isArray(value) ? value.join("\n") || "Not answered yet" : typeof value === "string" && value ? value : "Not answered yet"}</p>
                {session.state !== "confirmed" && <button type="button" disabled={isWorking || !!syncError} onClick={() => openCorrection(field, value)}>Edit</button>}</>}
            </div>;
          })}
        </details>}
        {session && <details className={styles.details}>
          <summary>View conversation details</summary>
          {previousAnswers.length > 0 && <button type="button" onClick={() => openPreviousAnswerReview(answerReview.current)}>Review previous answers and choices</button>}
          {(session.turns || []).filter((turn) => turn.kind !== "opening").map((turn) => <p key={turn.id}><strong>{turn.role === "founder" ? "You" : "Supymem"}</strong> {turn.role === "founder" ? founderChoiceWords(turn.text) : turn.text}</p>)}
        </details>}
        </details>
      </main>
    </AppShell>
  );
}
