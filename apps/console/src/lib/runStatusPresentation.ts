import { plainText } from "./researchBrief.ts";

/** Fired after pause/resume so the top status strip re-reads the venture instead of waiting for its next poll. */
export const RUN_STATUS_REFRESH = "supymem-run-status-refresh";

export type IdeaPageStatus = {
  state: string;
  activity: string;
  action?: { label: string; onClick: () => void; disabled: boolean };
};

type Run = {
  state: string;
  activity: string;
  next_step?: string;
};

type Cta = { label: string; href: string };
/** A specific blocking action wins; otherwise the server's next step, then the run's own action. */
export function statusBarAction(run: { state: string; action?: Cta | null } | undefined,
  next: { cta_label?: string; href?: string } | undefined, historical: boolean): Cta | null {
  if (historical) return null;
  if (run?.state === "ready") return run.action ?? null;
  const blocked = run?.state === "needs_you" || run?.state === "failed";
  const server = next?.cta_label && next.href?.startsWith("/") && !next.href.startsWith("//") ? { label: next.cta_label, href: next.href } : null;
  // Same screen: the run's own link is more specific (it carries the run and the #anchor).
  const path = (href: string) => href.split(/[?#]/)[0];
  if (run?.state === "working" || run?.state === "queued") return run.action ?? server;
  const specific = run?.action && server && path(run.action.href) === path(server.href) ? run.action : null;
  return blocked ? run?.action ?? server : specific ?? server ?? run?.action ?? null;
}

/** `work.running` is also the key for any other background job; only the first-question step carries this title. */
const FIRST_QUESTION_TITLE = "Preparing your first question";
export const isPreparingFirstQuestion = (run: { state: string } | undefined, attentionCount: number, nextKey?: string, nextTitle?: string) =>
  nextKey === "work.running" && run?.state === "needs_you" && attentionCount === 0
  && (nextTitle === undefined || nextTitle === FIRST_QUESTION_TITLE);

/** Terminal outcomes are saved history, not an activity happening now. */
export function runStatusPresentation(run: Run | undefined, attentionCount: number, nextKey?: string, nextTitle?: string) {
  // The job is still preparing the first question: nothing to answer yet.
  const preparing = isPreparingFirstQuestion(run, attentionCount, nextKey, nextTitle);
  const historical = run?.state === "cancelled" || run?.state === "completed";
  return {
    historical,
    state: preparing ? "Working" : attentionCount > 0 || run?.state === "needs_you"
      ? "Needs you"
      : run?.state === "waiting_for_device" ? "Waiting for your Mac, it resumes when your Mac is awake" : historical ? "Idle" : run?.state
        ? run.state.charAt(0).toUpperCase() + run.state.slice(1) : null,
    activity: historical
      ? attentionCount > 0
        ? "No work is in progress. Review the saved action request."
        : "No work is in progress. The last result is saved in Details."
      : preparing ? "Preparing your first question" : run?.activity && plainText(run.activity),
    nextStep: historical
      ? attentionCount > 0
        ? "Review the saved action request."
        : "Open saved work when you are ready to continue."
      : run?.next_step,
  };
}
