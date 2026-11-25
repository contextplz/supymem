import { createContext, useContext } from "react";

export function createActivityRefresh(ventureId: string) {
  let connected = false;
  const listeners = new Set<(event: "connection" | "change") => void>();
  const changed = () => listeners.forEach(listener => listener("change"));
  return {
    ventureId,
    connected: () => connected,
    changed,
    setConnected(next: boolean) {
      if (connected === next) return;
      connected = next;
      listeners.forEach(listener => listener("connection"));
    },
    subscribe(listener: (event: "connection" | "change") => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}

type ActivityRefresh = ReturnType<typeof createActivityRefresh>;
export const ActivityRefreshContext = createContext<ActivityRefresh | null>(null);
export const ACTIVITY_SAFETY_MS = 30_000;

export function useActivityRefresh(ventureId?: string): ActivityRefresh | null {
  const feed = useContext(ActivityRefreshContext);
  return feed?.ventureId === ventureId ? feed ?? null : null;
}

export function watchActivity(feed: ActivityRefresh | null, read: () => Promise<unknown>, initial = true) {
  let disposed = false, inFlight = false, queued = false, pending = false, missed = false;
  let timer: number | undefined;
  const refresh = async () => {
    if (disposed) return;
    if (document.hidden) { missed = true; return; }
    if (inFlight) { pending = true; return; }
    missed = false;
    inFlight = true;
    try { await read(); }
    finally {
      inFlight = false;
      if (pending) { pending = false; enqueue(); }
    }
  };
  const enqueue = () => {
    if (queued || disposed) return;
    queued = true;
    queueMicrotask(() => { queued = false; void refresh(); });
  };
  const stopTimer = () => { if (timer !== undefined) window.clearInterval(timer); timer = undefined; };
  const syncTimer = () => {
    if (feed?.connected() || document.hidden) stopTimer();
    else timer ??= window.setInterval(() => { if (!inFlight) enqueue(); }, ACTIVITY_SAFETY_MS);
  };
  let disconnected = false;
  const unsubscribe = feed?.subscribe(event => {
    if (event === "change") { enqueue(); return; }
    syncTimer();
    if (!feed.connected()) disconnected = true;
    else if (disconnected) { disconnected = false; enqueue(); }
  });
  const wake = () => {
    syncTimer();
    if (missed || !feed?.connected()) enqueue();
  };
  window.addEventListener("focus", wake);
  window.addEventListener("online", wake);
  document.addEventListener("visibilitychange", wake);
  syncTimer();
  if (initial) void refresh();
  return () => {
    disposed = true;
    unsubscribe?.();
    stopTimer();
    window.removeEventListener("focus", wake);
    window.removeEventListener("online", wake);
    document.removeEventListener("visibilitychange", wake);
  };
}
