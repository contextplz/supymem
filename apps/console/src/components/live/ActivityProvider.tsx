"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, apiLive, GATEWAY } from "@/lib/client";
import { ACTIVITY_SAFETY_MS, ActivityRefreshContext, createActivityRefresh } from "@/lib/activityRefresh";
import { isLocalConsole } from "@/lib/localMode";
import { createAuthenticatedEventSource, type AuthenticatedEventSource } from "@/lib/nativeTransport";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import type {
  ActivityLine,
  ConnectionState,
  OnboardingState,
  RunSummary,
} from "@/lib/api-types";

type ActivityContextValue = {
  lines: ActivityLine[];
  run: RunSummary;
  connection: ConnectionState;
  onboarding: OnboardingState | null;
  refreshOnboarding: () => Promise<OnboardingState | null>;
  retry: () => void;
};

const ActivityContext = createContext<ActivityContextValue | null>(null);

function readActivity(path: string, signal?: AbortSignal) {
  return isLocalConsole() ? api(path, { signal }) : apiLive(path, signal);
}

function mergeLines(prev: ActivityLine[], incoming: ActivityLine[]): ActivityLine[] {
  const bySeq = new Map<number, ActivityLine>();
  for (const l of prev) bySeq.set(l.seq, l);
  for (const l of incoming) {
    const existing = bySeq.get(l.seq);
    if (!existing) {
      bySeq.set(l.seq, l);
      continue;
    }
    if (l.step_id && existing.step_id === l.step_id && l.status !== "running") {
      bySeq.set(l.seq, l);
    } else {
      bySeq.set(l.seq, { ...existing, ...l });
    }
  }
  const byStep = new Map<string, number>();
  const sorted = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  for (const l of sorted) {
    if (!l.step_id) continue;
    const prevSeq = byStep.get(l.step_id);
    if (prevSeq != null && l.status !== "running") {
      bySeq.delete(prevSeq);
    }
    byStep.set(l.step_id, l.seq);
  }
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

export function ActivityProvider({
  ventureId,
  children,
}: {
  ventureId: string;
  children: ReactNode;
}) {
  const local = isLocalConsole();
  const activity = useMemo(() => createActivityRefresh(ventureId), [ventureId]);
  const [lines, setLines] = useState<ActivityLine[]>([]);
  const [run, setRun] = useState<RunSummary>({ status: "idle" });
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [onboarding, setOnboarding] = useState<OnboardingState | null>(null);
  const lastSeq = useRef(0);
  const esRef = useRef<AuthenticatedEventSource | null>(null);
  const pollRef = useRef<number | null>(null);
  const reconnectRef = useRef<number | null>(null);
  const readController = useRef<AbortController | null>(null);
  const backoff = useRef(1000);
  const retryToken = useRef(0);
  const streamToken = useRef(0);
  const alive = useRef(true);
  const polling = useRef(false);
  const nextOnboardingPollAt = useRef(0);

  const refreshOnboarding = useCallback(async () => {
    const generation = retryToken.current;
    try {
      const r = await readActivity(`/api/ventures/${ventureId}/onboarding`, readController.current?.signal);
      if (!alive.current || generation !== retryToken.current) return null;
      if (r.status === 404 || (r.ok && (r.data as { status?: string } | null)?.status === "idle")) {
        setOnboarding(null);
        setRun({ status: "idle" });
        // An absent run is a normal venture state. Explicit and SSE
        // refreshes stay immediate; only fallback polling is delayed.
        nextOnboardingPollAt.current = Date.now() + 60_000;
        return null;
      }
      if (!r.ok) return null;
      const data = r.data as OnboardingState;
      nextOnboardingPollAt.current = 0;
      setOnboarding(data);
      setRun({
        status: data.status,
        phase: data.phase,
        phases_done: data.phases_done,
        phases_total: data.phases_total,
        cost_usd: data.cost_usd,
        elapsed_ms: data.elapsed_ms,
      });
      return data;
    } catch {
      return null;
    }
  }, [ventureId]);

  const fetchPage = useCallback(async () => {
    const generation = retryToken.current;
    try {
      const r = await readActivity(
        `/api/ventures/${ventureId}/activity?after_seq=${lastSeq.current}&limit=100`,
        readController.current?.signal,
      );
      if (!alive.current || generation !== retryToken.current || !r.ok) return false;
      const data = r.data as {
        items?: ActivityLine[];
        run?: RunSummary;
      };
      const items = data.items || [];
      if (items.length) {
        lastSeq.current = Math.max(lastSeq.current, ...items.map((item) => item.seq));
        setLines((prev) => {
          if (!alive.current || generation !== retryToken.current) return prev;
          const next = mergeLines(prev, items);
          return next;
        });
      }
      if (data.run) setRun((prev) => ({ ...prev, ...data.run }));
      return true;
    } catch {
      return false;
    }
  }, [ventureId]);

  const stopPoll = useCallback(() => {
    polling.current = false;
    if (pollRef.current != null) {
      window.clearTimeout(pollRef.current);
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const startPoll = useCallback((immediately = true) => {
    if (!alive.current) return;
    stopPoll();
    polling.current = true;
    setConnection("reconnecting");
    let delay = local ? ACTIVITY_SAFETY_MS : 1500;
    const generation = retryToken.current;
    let failures = 0;
    const current = () => alive.current && polling.current && generation === retryToken.current;
    const tick = async () => {
      if (!current()) return;
      try {
        const [ok] = await Promise.all([fetchPage(), Date.now() >= nextOnboardingPollAt.current ? refreshOnboarding() : null]);
        if (!current()) return;
        if (!ok) {
          failures++;
          setConnection(failures >= 3 ? "offline" : "reconnecting");
          if (!local) delay = Math.min(12000, delay * 2);
        } else {
          failures = 0;
          setConnection(local ? "reconnecting" : "live");
          if (!local) delay = 15000;
        }
      } catch {
        if (!current()) return;
        failures++;
        setConnection(failures >= 3 ? "offline" : "reconnecting");
        if (!local) delay = Math.min(12000, delay * 2);
      }
      if (!current()) return;
      pollRef.current = window.setTimeout(() => void tick(), delay);
    };
    if (immediately) void tick();
    else pollRef.current = window.setTimeout(() => void tick(), delay);
  }, [fetchPage, local, refreshOnboarding, stopPoll]);

  const connectSse = useCallback(() => {
    if (typeof window === "undefined" || !ventureId || !alive.current) return;
    esRef.current?.close();
    if (reconnectRef.current != null) window.clearTimeout(reconnectRef.current);
    if (!readController.current || readController.current.signal.aborted) readController.current = new AbortController();
    const token = ++streamToken.current;
    if (!polling.current) startPoll(!local);
    let closed = false;
    const current = () => !closed && token === streamToken.current && alive.current;

    const reconnect = () => {
      if (!current()) return;
      closed = true;
      activity.setConnected(false);
      esRef.current?.close();
      if (local && !polling.current) startPoll(false);
      const wait = backoff.current;
      backoff.current = Math.min(60000, wait * 2);
      if (reconnectRef.current != null) window.clearTimeout(reconnectRef.current);
      reconnectRef.current = window.setTimeout(() => {
        if (token === streamToken.current && alive.current) connectSse();
      }, wait + Math.random() * 400);
    };

    {
      const path = `/api/ventures/${ventureId}/activity/stream`;
      let es: AuthenticatedEventSource;
      try {
        es = createAuthenticatedEventSource(path, GATEWAY, String(lastSeq.current));
      } catch {
        reconnect();
        return;
      }
      esRef.current = es;
      setConnection("reconnecting");

      es.addEventListener("open", () => {
        if (!current()) return;
        if (local) stopPoll();
        activity.setConnected(true);
        setConnection("live");
        backoff.current = 1000;
        if (reconnectRef.current != null) window.clearTimeout(reconnectRef.current);
        reconnectRef.current = null;
      });

      es.addEventListener("activity", (ev) => {
        if (!current()) return;
        try {
          const line = JSON.parse((ev as MessageEvent).data) as ActivityLine;
          if (line.seq > lastSeq.current) activity.changed();
          lastSeq.current = Math.max(lastSeq.current, line.seq);
          setLines((prev) => {
            if (token !== streamToken.current || !alive.current) return prev;
            const next = mergeLines(prev, [line]);
            return next;
          });
          // Absent-run ventures 404 here; honor the same 60s backoff as polling.
          if ((line.status === "running" || line.kind === "search")
            && Date.now() >= nextOnboardingPollAt.current) {
            void refreshOnboarding();
          }
        } catch {
          /* ignore malformed */
        }
      });

      es.addEventListener("phase", (ev) => {
        if (!current()) return;
        try {
          const p = JSON.parse((ev as MessageEvent).data) as Partial<RunSummary>;
          setRun((prev) => ({ ...prev, ...p }));
          activity.changed();
        } catch {
          /* ignore */
        }
      });

      es.addEventListener("paused", () => {
        if (!current()) return;
        activity.changed();
        void refreshOnboarding();
      });

      es.addEventListener("done", () => {
        if (!current()) return;
        activity.changed();
        void refreshOnboarding();
      });

      es.onerror = reconnect;
      es.addEventListener("unavailable", reconnect);

      reconnectRef.current = window.setTimeout(() => {
        if (token === streamToken.current && alive.current && es.readyState !== 1) reconnect();
      }, 10000);
    }
  }, [activity, ventureId, local, refreshOnboarding, startPoll, stopPoll]);

  const retry = useCallback(() => {
    stopPoll();
    activity.setConnected(false);
    retryToken.current++;
    readController.current?.abort();
    backoff.current = 1000;
    nextOnboardingPollAt.current = 0;
    connectSse();
  }, [activity, connectSse, stopPoll]);

  useEffect(() => {
    alive.current = true;
    backoff.current = 1000;
    lastSeq.current = 0;
    nextOnboardingPollAt.current = 0;
    setLines([]);
    setRun({ status: "idle" });
    setOnboarding(null);
    setConnection("idle");
    // Older builds cached activity by venture only. Never restore that across accounts.
    try { sessionStorage.removeItem(`supymem:activity:${ventureId}`); } catch { /* storage disabled */ }
    retry();
    const resume = () => { if (!document.hidden && (!local || !activity.connected())) retry(); };
    window.addEventListener("online", resume);
    window.addEventListener("focus", resume);
    document.addEventListener("visibilitychange", resume);
    return () => {
      alive.current = false;
      activity.setConnected(false);
      retryToken.current += 1;
      streamToken.current += 1;
      esRef.current?.close();
      readController.current?.abort();
      if (reconnectRef.current != null) window.clearTimeout(reconnectRef.current);
      stopPoll();
      window.removeEventListener("online", resume);
      window.removeEventListener("focus", resume);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [ventureId]); // eslint-disable-line react-hooks/exhaustive-deps

  const value = useMemo(
    () => ({ lines, run, connection, onboarding, refreshOnboarding, retry }),
    [lines, run, connection, onboarding, refreshOnboarding, retry],
  );

  return (
    <ActivityContext.Provider value={value}>
      <ErrorNotice error={connection === "offline" ? "You cannot get updates right now. Your saved work is kept." : ""} source="activity.connection" as="div">
        You cannot get updates right now. Your saved work is kept. <button type="button" className="ghost sm" onClick={retry}>Try again</button>
      </ErrorNotice>
      <ActivityRefreshContext.Provider value={activity}>{children}</ActivityRefreshContext.Provider>
    </ActivityContext.Provider>
  );
}

export function useActivity(): ActivityContextValue {
  const ctx = useContext(ActivityContext);
  if (!ctx) {
    return {
      lines: [],
      run: { status: "idle" },
      connection: "idle",
      onboarding: null,
      refreshOnboarding: async () => null,
      retry: () => undefined,
    };
  }
  return ctx;
}
