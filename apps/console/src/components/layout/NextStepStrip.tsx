"use client";

import Link from "@/components/chat/ChatLink";
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { api, verifiedSessionSubject, verifiedSessionEpoch } from "@/lib/client";
import { RUN_STATUS_REFRESH, type IdeaPageStatus } from "@/lib/runStatusPresentation";
import { asServerStep, failureOwnsCta, hasPageChoices, nextStepButton, ownedByGtmFlow, type ServerNextStep } from "@/lib/nextStep";
import styles from "./NextStepStrip.module.css";
import { PageChoices } from "@/components/site/PageChoices";
import type { ResearchProgress } from "@/lib/durableJobs";
import { FounderUIContext } from "@/lib/founderUI";

export const ASK_EVENT = "supymem:ask";
export const ChatOpenContext = createContext(false);
export const ClarificationSkipContext = createContext<{ skip: () => Promise<boolean>; disabled: boolean } | null>(null);
const dashboardPath = (id: string) => `/api/ventures/${encodeURIComponent(id)}/dashboard`;
type ScopedStep = ServerNextStep & { runState?: string; researchReportReady: boolean; researchProgress?: ResearchProgress | null; receiptScope: { venture: string; owner: string; epoch: number } };
const scopedStep = (step: ServerNextStep | null, id?: string, runState?: string, researchReportReady = false, researchProgress?: ResearchProgress | null): ScopedStep | null => {
  const owner = verifiedSessionSubject();
  return step && id && owner ? { ...step, runState, researchReportReady, researchProgress, receiptScope: { venture: id, owner, epoch: verifiedSessionEpoch() } } : null;
};
const owned = (step: ScopedStep | null, id?: string) => Boolean(step && step.receiptScope.venture === id
  && step.receiptScope.owner === verifiedSessionSubject() && step.receiptScope.epoch === verifiedSessionEpoch());

/** Fresh receipts cannot cross venture/account scope or revive pre-edit cached actions. */
export function useNextStep(ventureId?: string): ScopedStep | null {
  const pathname = usePathname();
  const [step, setStep] = useState<ScopedStep | null>(null);
  useEffect(() => {
    let stopped = false;
    let reads = 0;
    const refresh = () => {
      if (!ventureId) return;
      const ticket = ++reads;
      const owner = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
      const adopt = (r: Awaited<ReturnType<typeof api>>) => {
        const value = r.data as { venture?: { id?: string }; next_step?: unknown; run_status?: { state?: string; research_report_ready?: boolean; research_progress?: ResearchProgress | null } } | null;
        if (!stopped && ticket === reads && owner === verifiedSessionSubject() && epoch === verifiedSessionEpoch()
            && r.ok && value?.venture?.id === ventureId) setStep(scopedStep(asServerStep(value.next_step), ventureId, value.run_status?.state, value.run_status?.research_report_ready === true, value.run_status?.research_progress));
      };
      return api(dashboardPath(ventureId)).then(adopt).catch(() => {});
    };
    const reset = () => { setStep(null); void refresh(); };
    const changed = (event: Event) => {
      const detail = (event as CustomEvent<{ ventureId?: string; actorId?: string; epoch?: number }>).detail;
      if (stopped || detail?.ventureId !== ventureId || !detail.actorId || detail.actorId !== verifiedSessionSubject() || detail.epoch !== verifiedSessionEpoch()) return;
      setStep(null); void refresh();
    };
    reset();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 15000);
    window.addEventListener("supymem-session-changed", reset);
    window.addEventListener("supymem-venture-changed", changed);
    window.addEventListener(RUN_STATUS_REFRESH, reset);
    return () => { stopped = true; clearInterval(timer); window.removeEventListener("supymem-session-changed", reset); window.removeEventListener("supymem-venture-changed", changed); window.removeEventListener(RUN_STATUS_REFRESH, reset); };
  }, [ventureId, pathname]);
  return owned(step, ventureId) ? step : null;
}

export function NextStepButton({ step, className }: { step: ScopedStep; className?: string }) {
  const pathname = usePathname();
  const chatOpen = useContext(ChatOpenContext);
  const chatFirst = useContext(FounderUIContext) === "home";
  const sourceHref = typeof window === "undefined" ? pathname : window.location.href;
  const active = useRef({ step, pathname, sourceHref, live: true });
  if (active.current.step !== step || active.current.pathname !== pathname || active.current.sourceHref !== sourceHref)
    active.current = { step, pathname, sourceHref, live: true };
  const renderedScope = active.current;
  useEffect(() => {
    renderedScope.live = true;
    return () => { renderedScope.live = false; };
  }, [renderedScope]);
  const current = () => renderedScope.live && active.current === renderedScope && owned(step, step.receiptScope.venture)
    && pathname === window.location.pathname && sourceHref === window.location.href;
  if (hasPageChoices(step)) return <PageChoices ventureId={step.receiptScope.venture} />;
  const button = nextStepButton(step, pathname, chatOpen, chatFirst);
  if (!button) return null;
  const props = { className: className || (pathname.endsWith("/site") ? styles.quietButton : styles.button), "data-usage-control": "next_step_action" };
  if (button.kind === "link") return <Link {...props} href={button.href} onClick={(event) => { if (!current()) event.preventDefault(); }}>{button.label}</Link>;
  return <button type="button" {...props} onClick={() => {
    if (!current()) return;
    if (button.kind === "action") window.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: { action: button.action, payload: button.payload } }));
    else document.getElementById(button.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }}>{button.label}</button>;
}

export function JourneyChoices({ step, restore, compact = false }: { step: ScopedStep; restore?: string; compact?: boolean }) {
  const clarification = useContext(ClarificationSkipContext);
  const [skipping, setSkipping] = useState(false);
  const [feedback, setFeedback] = useState("");
  const owner = step.receiptScope;
  const pathname = usePathname();
  const sourceHref = typeof window === "undefined" ? pathname : window.location.href;
  const active = useRef({ step, pathname, sourceHref, live: true });
  if (active.current.step !== step || active.current.pathname !== pathname || active.current.sourceHref !== sourceHref)
    active.current = { step, pathname, sourceHref, live: true };
  const rendered = active.current;
  useEffect(() => { rendered.live = true; return () => { rendered.live = false; }; }, [rendered]);
  function ask(action: string, target: string, why: string) {
    if (!rendered.live || active.current !== rendered || !owned(step, owner.venture)
        || pathname !== window.location.pathname || sourceHref !== window.location.href) return;
    window.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: { action, payload: { step: target, why } } }));
  }
  async function skip() {
    if (!rendered.live || active.current !== rendered || !owned(step, owner.venture)
        || pathname !== window.location.pathname || sourceHref !== window.location.href || skipping) return;
    if (step.skip_step !== "clarification" || !clarification) { ask("journey.skip_step", step.skip_step!, "I chose to skip this step."); return; }
    if (clarification.disabled) return;
    setSkipping(true); setFeedback("Skipping questions…");
    try {
      const saved = await clarification.skip();
      if (rendered.live && active.current === rendered) setFeedback(saved ? "Questions skipped. Preparing your next step…" : "Questions could not be skipped. Try again.");
    } catch {
      if (rendered.live && active.current === rendered) setFeedback("Questions could not be skipped. Try again.");
    } finally { setSkipping(false); }
  }
  if (restore) return <button type="button" className="ghost sm" onClick={() => ask("journey.restore_step", restore, "I want to restore this step.")}>Restore this step</button>;
  const hint = "You can skip this; Supymem will use what you've told it so far.";
  // The shell strip stays one row (a wrapped hint would push page content off small screens); the hint becomes the tooltip.
  return <div className={compact ? styles.choices : undefined}>
    {step.skip_step && <><button type="button" className="ghost sm" title={compact ? hint : undefined} disabled={skipping || step.skip_step === "clarification" && clarification?.disabled} onClick={() => void skip()}>Skip this step</button>
      {!compact && <><p className="muted">{hint}</p><p className="muted">Missing checks can lower confidence in the next recommendation.</p></>}</>}
    {feedback && <span role="status">{feedback}</span>}
    {step.has_website && step.skip_step !== "find_customers" && <button type="button" className="ghost sm"
      onClick={() => ask("journey.jump_to", "find_customers", "I already have a website and want to find customers now.")}>Skip to Find customers</button>}
  </div>;
}

/** Quiet "Next step" line: full in the shell below the status bar, compact at the end of a page. */
export function NextStepStrip({ ventureId, compact = false, inline = false, ideaStatus }: { ventureId?: string; compact?: boolean; inline?: boolean; ideaStatus?: IdeaPageStatus }) {
  const savedStep = useNextStep(ventureId);
  const pathname = usePathname();
  const step = pathname.endsWith("/research") && savedStep && Object.keys(savedStep.journey_overrides?.skipped || {}).length
    && !savedStep.researchReportReady && /^(?:idea\.|site\.|gtm\.|venture\.review$)/.test(savedStep.key)
    ? { ...savedStep, key: "research.start", title: "Research from saved context",
        detail: "Use your saved idea or website. If a topic is missing, we will ask for it before starting.",
        cta_label: "Review research request", href: `/v/${ventureId}/research`, chat_action: "chat.prefill",
        payload: { text: "Start research from my saved idea and website context." }, skip_step: "research" }
    : savedStep;
  const chatOpen = useContext(ChatOpenContext);
  const pageOwnsChoices = pathname.endsWith("/research") && Boolean(hasPageChoices(step));
  if ((pathname.endsWith("/idea") || pathname === "/new") && ideaStatus) return inline
    ? <span className={styles.inline} aria-label="Next step"><span>Next step · {ideaStatus.activity}</span>{step && <JourneyChoices step={step} compact />}</span>
    : <section className={compact ? styles.compact : styles.strip} aria-label="Next step"><div className={styles.text}><strong>Next step · {ideaStatus.activity}</strong></div>{step && <JourneyChoices step={step} compact />}</section>;
  if (inline) return step ? <span className={styles.inline} aria-label="Next step"><span>Next step · {step.title}</span> {!pageOwnsChoices && <>{!failureOwnsCta(step, step.runState) && <NextStepButton step={step} />}<JourneyChoices step={step} compact /></>}</span> : null;
  const reserved = compact || !ventureId ? null : <div className={`${styles.strip} ${styles.placeholder}`} aria-hidden="true" />;
  // Research owns its current-revision actions in the report body, including
  // immediate retirement while a rerun is being admitted.
  if (pathname.endsWith("/research") && !step?.skip_step) return null;
  if (ownedByGtmFlow(step, pathname)) return null;
  if (!step || step.key === "work.running" || (!/^(?:site\.building|research\.running)$/.test(step.key)
    && (step.runState === "working" || step.runState === "queued"))) return reserved;
  // Compact nudges only appear when they lead somewhere; the shell strip always explains the step.
  if (compact && !nextStepButton(step, pathname, chatOpen)) return null;
  return <section className={compact ? styles.compact : styles.strip} aria-label="Next step">
    <div className={styles.text}><strong><span>Next step · </span>{step.title}</strong>{!compact && step.detail && <p>{step.detail}</p>}</div>
    {!failureOwnsCta(step, step.runState) && <NextStepButton step={step} />}
    <JourneyChoices step={step} compact />
  </section>;
}
