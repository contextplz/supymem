import type { ResearchProgress } from "./durableJobs.ts";

const saved = new Map<string, ResearchProgress>();
const listeners = new Map<string, Set<(progress: ResearchProgress) => void>>();

export const gtmProgressOwner = (subject: string | null, epoch: number, venture: string) => JSON.stringify([subject, epoch, venture]);

export function shareGtmProgress(owner: string, progress: ResearchProgress): ResearchProgress {
  const previous = saved.get(owner);
  if (previous?.run_id !== progress.run_id && previous?.started_at && progress.started_at
    && Date.parse(previous.started_at) > Date.parse(progress.started_at)) return previous;
  const next = previous && previous.run_id === progress.run_id && (previous.percent ?? 0) > (progress.percent ?? 0) ? previous : progress;
  saved.set(owner, next);
  listeners.get(owner)?.forEach(listener => listener(next));
  return next;
}

export function subscribeGtmProgress(owner: string, listener: (progress: ResearchProgress) => void): () => void {
  const group = listeners.get(owner) ?? new Set();
  group.add(listener);
  listeners.set(owner, group);
  const current = saved.get(owner);
  if (current) listener(current);
  return () => { group.delete(listener); if (!group.size) { listeners.delete(owner); saved.delete(owner); } };
}
