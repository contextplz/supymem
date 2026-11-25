"use client";

import { useEffect, useState } from "react";
import { opsApi, type AgentStatus } from "@/lib/opsApi";

export function useOpsAgentStatus(pollIntervalMs = 30000) {
  const [statuses, setStatuses] = useState<AgentStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;

    const load = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        const data = await opsApi.get<AgentStatus[]>("/agents/status");
        if (!cancelled) {
          setStatuses(Array.isArray(data) ? data : []);
          setError(null);
          setOffline(false);
        }
      } catch (e) {
        if (!cancelled) {
          setError(String(e));
          setOffline(true);
        }
      } finally {
        inFlight = false;
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    const interval = setInterval(() => void load(), pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [pollIntervalMs]);

  return { statuses, loading, error, offline };
}
