"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiLive } from "@/lib/client";
import { delayedJobNotice, isDurableJob, jobFinished, jobNeedsRecovery, jobWaitingLabel, reportJobFailure, statusObservationStalled, type DurableJob } from "@/lib/durableJobs";
export { jobSucceeded } from "@/lib/durableJobs";

export function useDurableJob(id: string | null, onFinished: (job: DurableJob) => void) {
  const [job, setJob] = useState<DurableJob | null>(null);
  const [error, setError] = useState("");
  const [clock, setClock] = useState(() => Date.now());
  const [lastConfirmedAt, setLastConfirmedAt] = useState(() => Date.now());
  const finished = useRef(onFinished);
  const reportedJobId = useRef<string | null>(null);
  const notifiedJobId = useRef<string | null>(null);
  const retryRef = useRef(() => {});
  const retry = useCallback(() => retryRef.current(), []);
  finished.current = onFinished;
  useEffect(() => {
    setJob(null); setError(""); setLastConfirmedAt(Date.now());
    if (!id) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let inFlight = false;
    const controller = new AbortController();
    const resume = () => {
      if (disposed || inFlight || notifiedJobId.current === id) return;
      clearTimeout(timer);
      void poll();
    };
    retryRef.current = resume;
    const onVisible = () => { if (!document.hidden) resume(); };
    async function poll() {
      if (disposed || inFlight) return;
      inFlight = true;
      // Native WebViews may report hidden while the founder operates the app.
      // Keep checking saved job state at a lower rate instead of starving completion.
      let result: Awaited<ReturnType<typeof apiLive>>;
      try {
        result = await apiLive(`/api/jobs/${encodeURIComponent(id!)}`, controller.signal);
      } catch {
        inFlight = false;
        if (!disposed) {
          setError("You cannot check your saved work right now. Try again. This will not restart it.");
          timer = setTimeout(poll, 15000);
        }
        return;
      }
      inFlight = false;
      if (disposed) return;
      if (!result.ok) {
        setError("You cannot check your saved work right now. Try again. This will not restart it.");
        // Auth refresh and eventual job visibility can recover without a page
        // reload. Keep the saved job ID and reconcile it; never submit again.
        timer = setTimeout(poll, 15000); return;
      }
      const current = result.data;
      if (!isDurableJob(current) || current.id !== id) {
        setError("You cannot read the saved progress right now. Try again. This will not restart your work.");
        timer = setTimeout(poll, 15000); return;
      }
      setJob(current); setError(""); setLastConfirmedAt(Date.now());
      if (jobFinished(current)) {
        // Strict Mode can re-run this effect for the same saved terminal job.
        if (reportedJobId.current !== current.id) {
          reportedJobId.current = current.id;
          reportJobFailure(current);
        }
        if (notifiedJobId.current !== current.id) {
          notifiedJobId.current = current.id;
          finished.current(current);
        }
      }
      else timer = setTimeout(poll, document.hidden ? 15000 : 3000);
    }
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", resume);
    window.addEventListener("focus", resume);
    void poll();
    return () => {
      disposed = true; clearTimeout(timer);
      controller.abort();
      retryRef.current = () => {};
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", resume);
      window.removeEventListener("focus", resume);
    };
  }, [id]);
  useEffect(() => {
    if (!id) return;
    const timer = setInterval(() => setClock(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, [id]);
  // A native IPC or browser request can remain unresolved even while the
  // underlying durable job proceeds. Surface that lack of observation rather
  // than presenting an endless spinner as proof that work is progressing.
  const stalled = statusObservationStalled(id, job, error, lastConfirmedAt, clock);
  const delayedNotice = delayedJobNotice(job, clock);
  const delayed = Boolean(delayedNotice);
  const needsRecovery = jobNeedsRecovery(error, stalled, delayed);
  const label = error || (stalled ? "You have not had a recent progress update. Check your saved work. This will not start it again." : null) || delayedNotice || (!job ? "Checking background work…" : jobWaitingLabel(job) || ({
    leased: "Working", running: "Working", succeeded: "Completed", done: "Completed",
    failed_dead: "Needs attention", failed: "Needs attention", poison: "Needs attention", cancelled: "Cancelled",
  }[job.status] || "Updating"));
  return { job, error, label, stalled, delayed, needsRecovery, retry };
}
