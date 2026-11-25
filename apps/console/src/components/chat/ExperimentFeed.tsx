"use client";

import type { ChatMessage } from "@/lib/api-types";
import { safeLoopText } from "@/lib/loopProgress";
import { planChatActions } from "./chatActionPolicy";
import styles from "./ExperimentFeed.module.css";

type Threshold = { op: string; value: number; metric?: string; min_events?: number };
type Spec = { id: string; venture_id: string; title?: string; hypothesis?: string; scan?: string;
  catalog_key?: string; runner?: string; state?: string; approval?: string; metric?: string;
  min_sample?: number; max_duration_days?: number; founder_minutes?: number; ceiling_micro?: number;
  success_threshold?: Threshold; failure_threshold?: Threshold };
type Experiment = { id: string; venture_id: string; hypothesis?: string; state?: string; outcome?: string | null;
  started_at?: string | null; spec: Spec; result?: { outcome?: string; measured?: { events?: number } } | null;
  spend?: { spent_micro: number; cap_micro: number | null; status: string } };
export type ExperimentAutopilot = { venture_id: string; experiments?: Experiment[]; proposed_experiments?: Spec[];
  decision_recommendation?: { actions?: ChatMessage["actions"] } | null };
type Action = NonNullable<ChatMessage["actions"]>[number];
type OnAction = (name: string, payload: Record<string, unknown>) => boolean | void | Promise<boolean | void>;

const outcomes: Record<string, string> = { supported: "Worked", refuted: "Didn't work", inconclusive: "Too early to tell" };
const comparisons: Record<string, string> = { ">=": "at least", ">": "above", "<=": "at most", "<": "below", "==": "equal to", "!=": "different from" };
const decisions: Record<string, { label: string }> = {
  "experiment.persevere": { label: "Persevere" },
  "experiment.pivot": { label: "Pivot" },
  "experiment.spinoff": { label: "Spin off" },
  "experiment.kill": { label: "Kill" },
};

function plain(value: unknown) {
  return safeLoopText(value, 600).replace(/\bevidence level\b/gi, "what happened")
    .replace(/\b(?:hypothesis|assumption)\b/gi, "idea");
}

function amount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function thresholdLine(label: string, threshold: Threshold | undefined, spec: Spec) {
  const minimum = threshold?.min_events ?? spec.min_sample;
  if (!threshold || !Object.hasOwn(comparisons, threshold.op) || typeof threshold.value !== "number"
    || !Number.isFinite(threshold.value) || !Number.isSafeInteger(minimum) || !amount(minimum)) return null;
  const name = threshold.metric || spec.metric;
  const metric = typeof name === "string" ? plain(name.replaceAll("_", " ")) : "";
  return metric ? <p>{label}: {metric} is {comparisons[threshold.op]} {threshold.value}, after {minimum} collected.</p> : null;
}

function Scoreboard({ experiment }: { experiment: Experiment }) {
  const spec = experiment.spec, collected = experiment.result?.measured?.events;
  const outcome = experiment.outcome || experiment.result?.outcome || "";
  const supportedOutcome = Object.hasOwn(outcomes, outcome);
  const started = typeof experiment.started_at === "string" && /(?:Z|[+-]\d\d:\d\d)$/.test(experiment.started_at)
    ? Date.parse(experiment.started_at) : NaN;
  const deadline = Number.isFinite(started) && amount(spec.max_duration_days)
    ? new Date(started + spec.max_duration_days * 86400000) : null;
  const reviewDate = deadline && Number.isFinite(deadline.getTime()) ? deadline.toISOString() : null;
  return <li className={styles.card}>
    <h3>{plain(experiment.hypothesis || spec.hypothesis || spec.title)}</h3>
    <span className="tag" data-outcome={supportedOutcome ? outcome : undefined}>
      {supportedOutcome ? outcomes[outcome] : experiment.state === "running" ? "Testing" : "Too early to tell"}</span>
    {Number.isSafeInteger(collected) && amount(collected) && Number.isSafeInteger(spec.min_sample) && amount(spec.min_sample) && spec.min_sample > 0
      && <p>{collected} of {spec.min_sample} collected</p>}
    {thresholdLine("Works if", spec.success_threshold, spec)}
    {thresholdLine("Doesn't work if", spec.failure_threshold, spec)}
    {reviewDate && <p>Review by: <time dateTime={reviewDate}>{new Date(reviewDate).toLocaleString()}</time></p>}
  </li>;
}

function decisionEvidence(action: Action, ventureId: string) {
  const confirmation = action.confirmation, state = confirmation?.resulting_state;
  if (!action.action || !Object.hasOwn(decisions, action.action) || confirmation?.action !== action.action
    || state?.parent_venture_id !== ventureId || !Array.isArray(state.evidence)) return "";
  return state.evidence.filter(item => item && typeof item === "object" && Object.hasOwn(outcomes, item.outcome)
    && !!plain(item.learning || item.hypothesis))
    .map(item => `${outcomes[item.outcome]}: ${plain(item.learning || item.hypothesis)}`).join(" ");
}

export function ExperimentFeed({ data, ventureId, onAction, disabled = false, completed = [] }: {
  data?: ExperimentAutopilot | null; ventureId: string; onAction?: OnAction; disabled?: boolean; completed?: string[];
}) {
  // shortcut: the rounds endpoint must supply this flag-gated read payload, wire it server-side before enabling this UI.
  if (!data || data.venture_id !== ventureId) return null;
  const experiments = Array.isArray(data.experiments) ? data.experiments.filter(item => item && typeof item.id === "string"
    && item.venture_id === ventureId && item.spec && typeof item.spec === "object" && !Array.isArray(item.spec)
    && item.spec.venture_id === ventureId && !!plain(item.hypothesis || item.spec.hypothesis || item.spec.title)) : [];
  const specs = Array.isArray(data.proposed_experiments) ? data.proposed_experiments.filter(item => item
    && typeof item.id === "string" && !!item.id.trim() && item.venture_id === ventureId && item.state === "proposed"
    && !!plain(item.hypothesis || item.title)) : [];
  const runs = specs.map(spec => ({ label: "Run", action: "experiment.prepare", payload: { experiment_id: spec.id } }));
  const recommended = data.decision_recommendation?.actions;
  const seen = new Set<string>();
  const choices = Array.isArray(recommended) ? recommended.filter(action => {
    if (!action || !decisionEvidence(action, ventureId) || seen.has(action.action!)) return false;
    seen.add(action.action!); return true;
  }).map(action => ({ label: decisions[action.action!].label, action: action.action,
    request: action.label,
    disabled_reason: action.disabled_reason, evidence: decisionEvidence(action, ventureId) })) : [];
  const actionPlan = planChatActions([...choices.slice(0, 1), ...runs, ...choices.slice(1)]);
  const visibleRuns = actionPlan.visible.filter(action => action.action === "experiment.prepare");
  const moreRuns = actionPlan.overflow.filter(action => action.action === "experiment.prepare");
  const visibleChoices = actionPlan.visible.filter(action => action.action !== "experiment.prepare");
  const moreChoices = actionPlan.overflow.filter(action => action.action !== "experiment.prepare");
  if (!experiments.length && !specs.length && !choices.length) return null;
  const idea = (action: typeof runs[number]) => {
    const spec = specs.find(item => item.id === action.payload.experiment_id)!;
    const contacts = spec.runner === "outreach_split" || spec.approval === "each_send";
    const redPill = spec.scan === "redteam" || /^E(?:19|37|39)(?:_|$)/.test(spec.catalog_key || "");
    const blocked = disabled || !onAction || completed.includes(`${action.action}:${spec.id}`);
    return <li key={spec.id} className={styles.card}>
      <span className="tag">{redPill ? "Red-pill idea" : "Sensible idea"}</span>
      <h3>{plain(spec.title || spec.hypothesis)}</h3><p>{plain(spec.hypothesis)}</p>
      {amount(spec.max_duration_days) && <p>Time: up to {spec.max_duration_days} {spec.max_duration_days === 1 ? "day" : "days"}</p>}
      {amount(spec.founder_minutes) && <p>Your time: {spec.founder_minutes} {spec.founder_minutes === 1 ? "minute" : "minutes"}</p>}
      {contacts && <><span className="tag">Contacts people</span><p>You review sending separately. No one is contacted without your approval.</p></>}
      <div className={styles.actions}><button type="button" className={action === actionPlan.primary ? "primary sm" : "secondary sm"}
        disabled={blocked} aria-label={`Run: ${plain(spec.title || spec.hypothesis)}`}
        onClick={() => { if (!blocked) void onAction?.(action.action, action.payload); }}>Run</button></div>
    </li>;
  };
  const choice = (action: typeof choices[number]) => <li key={action.action} className={styles.card}>
    <p>{action.evidence}</p>
    <button type="button" className={action === actionPlan.primary ? "primary sm" : "secondary sm"}
      disabled={disabled || !onAction || !!action.disabled_reason} title={plain(action.disabled_reason)} onClick={() => {
        if (!disabled && !action.disabled_reason) void onAction?.("chat.reply", {text: `Please ${action.request}.`});
      }}>{action.label}</button>
    {action.disabled_reason && <p>{plain(action.disabled_reason)}</p>}
  </li>;
  return <section className={`panel-card ${styles.feed}`} aria-label="Your experiment feed">
    {!!experiments.length && <details open={experiments.length <= 2}><summary>Your tests ({experiments.length})</summary>
      <ul className={styles.list} tabIndex={0} aria-label="Your experiment scoreboards">{experiments.map(experiment => <Scoreboard key={experiment.id} experiment={experiment} />)}</ul>
    </details>}
    {!!specs.length && <details open={specs.length <= 2}><summary>Your experiment ideas ({specs.length})</summary>
      {!!visibleRuns.length && <ul className={styles.list} tabIndex={0} aria-label="Your proposed experiments">{visibleRuns.map(action => idea(action as typeof runs[number]))}</ul>}
      {!!moreRuns.length && <details><summary>More ideas ({moreRuns.length})</summary>
        <ul className={styles.list} tabIndex={0} aria-label="More proposed experiments">{moreRuns.map(action => idea(action as typeof runs[number]))}</ul></details>}
    </details>}
    {!!choices.length && <section aria-label="Your experiment decision"><h3>Your decision</h3>
      <p>Choose what you want to do next. Your choice runs through chat.</p>
      {!!visibleChoices.length && <ul className={styles.choices}>{visibleChoices.map(action => choice(action as typeof choices[number]))}</ul>}
      {!!moreChoices.length && <details><summary>More decisions ({moreChoices.length})</summary>
        <ul className={styles.choices}>{moreChoices.map(action => choice(action as typeof choices[number]))}</ul></details>}
    </section>}
  </section>;
}
