/** A result refresh is read-only and acknowledged only after it succeeds. */
type ResultRun = { run_id: string; state: string; result: { post_id?: string; topic_ids?: string[] } };
export function contentResultKey(run: ResultRun | null): string {
  if (!run || ["queued", "running"].includes(run.state)) return "";
  if (!run.result.post_id && !run.result.topic_ids?.length) return "";
  return JSON.stringify([run.run_id, run.state, run.result.post_id || "", run.result.topic_ids || []]);
}
export function contentCompletionTracker() {
  const done = new Set<string>(), pending = new Set<string>();
  return {
    async refresh(key: string, readSavedResults: () => Promise<void>, current: () => boolean): Promise<boolean> {
      if (!key || !current() || done.has(key) || pending.has(key)) return false;
      pending.add(key);
      try {
        await readSavedResults();
        if (!current()) return false;
        done.add(key);
        return true;
      } finally { pending.delete(key); }
    },
  };
}

/** Older snapshots must not overwrite results fetched after worker completion. */
export function contentRefreshFence() {
  let sequence = 0;
  return {
    invalidate() { sequence++; },
    next() { const captured = ++sequence; return () => captured === sequence; },
  };
}

/** Apply saved views independently; optional event reads cannot delay completion. */
export async function refreshContentViews<S, P, E>(
  reads: { status: Promise<S>; posts: Promise<P>; events: Promise<E> },
  apply: { status: (value: S) => void; posts: (value: P) => void; events: (value: E) => void },
  current: () => boolean, requireGenerated = false,
): Promise<unknown | null> {
  const status = reads.status.then(value => { if (current()) apply.status(value); });
  const posts = reads.posts.then(value => { if (current()) apply.posts(value); });
  const events = reads.events.then(value => { if (current()) apply.events(value); });
  void events.catch(() => { /* Optional events do not gate generation results. */ });
  const [s, p, e] = await Promise.allSettled([status, posts, ...(requireGenerated ? [] : [events])]);
  if (!current()) {
    if (requireGenerated) throw new Error("A newer result refresh superseded this read.");
    return null;
  }
  if (requireGenerated && (s.status === "rejected" || p.status === "rejected")) {
    throw new Error("Saved generation results could not be refreshed.");
  }
  const failed = [s, p, e].find(value => value?.status === "rejected");
  return failed?.status === "rejected" ? failed.reason : null;
}
