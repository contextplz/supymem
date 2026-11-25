"use client";

import { useEffect, useState } from "react";
import { opsApi, type ActivityEvent } from "@/lib/opsApi";

/** Authenticated, workspace-scoped polling until a scoped stream is available. */
export function useOpsActivityFeed(maxEvents = 100) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let delay = 10000;
    let attempted = false;
    async function sync() {
      if (!active) return;
      // Load the initial snapshot even when macOS opens the window in the
      // background. Visibility only suppresses subsequent polling.
      if (attempted && document.visibilityState === "hidden") { timer = setTimeout(sync, delay); return; }
      attempted = true;
      try {
        const rows = await opsApi.get<ActivityEvent[]>(`/dashboard/activity?limit=${Math.min(100, maxEvents)}`);
        if (active) { setEvents(rows.slice(0, maxEvents)); setConnected(true); }
        delay = 10000;
      } catch {
        if (active) setConnected(false);
        delay = Math.min(delay * 2, 60000);
      } finally {
        if (active) { setLoading(false); timer = setTimeout(sync, delay); }
      }
    }
    void sync();
    return () => { active = false; clearTimeout(timer); };
  }, [maxEvents]);
  return { events, connected, loading };
}
