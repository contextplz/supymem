/** Pure presentation helpers for the plan timeline. No new data, only shorter views of existing fields. */

/** Title trimmed to at most `max` words (default 6), with an ellipsis when cut. */
export function shortTitle(title: string, max = 6): string {
  const words = (title || "").trim().split(/\s+/).filter(Boolean);
  return words.length <= max ? words.join(" ") : `${words.slice(0, max).join(" ")}…`;
}

/** First sentence of text, capped at maxChars (about two lines). */
export function oneLine(text: string, maxChars = 140): string {
  const src = (text || "").replace(/\s+/g, " ").trim();
  const first = src.match(/^.*?[.!?](?=\s|$)/)?.[0] ?? src;
  return first.length > maxChars ? `${first.slice(0, maxChars - 1).trimEnd()}…` : first;
}

export type TimelineStatus = "done" | "active" | "upcoming" | "excluded";

/** done = every task done; active = the milestone holding the next unfinished task. */
export function milestoneStatus(
  milestone: { included: boolean; tasks: { id?: string; state?: string }[] },
  nextTaskId: string | undefined,
): TimelineStatus {
  if (!milestone.included) return "excluded";
  if (milestone.tasks.length > 0 && milestone.tasks.every(t => t.state === "done")) return "done";
  if (nextTaskId && milestone.tasks.some(t => t.id === nextTaskId)) return "active";
  return "upcoming";
}

/** Percent of included tasks that are done, 0-100. */
export function planProgress(milestones: { included: boolean; tasks: { state?: string }[] }[]): number {
  const tasks = milestones.filter(m => m.included).flatMap(m => m.tasks);
  return tasks.length ? Math.round((tasks.filter(t => t.state === "done").length / tasks.length) * 100) : 0;
}
