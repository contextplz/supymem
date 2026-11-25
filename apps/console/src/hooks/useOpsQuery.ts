"use client";

import { useEffect, useState } from "react";

/** Fetch only after the authenticated workspace gate has mounted this screen. */
export function useOpsQuery<T>(load: () => Promise<T>, pollIntervalMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    let inFlight = false;
    const refresh = () => {
      if (!active || inFlight) return;
      inFlight = true;
      void load().then((result) => { if (active) { setData(result); setError(""); } })
      .catch((err) => { if (active) setError(err instanceof Error ? err.message : "Company data is unavailable."); })
      .finally(() => { inFlight = false; if (active) setLoading(false); });
    };
    refresh();
    const timer = pollIntervalMs >= 10000 ? setInterval(refresh, pollIntervalMs) : undefined;
    return () => { active = false; if (timer) clearInterval(timer); };
  }, [load, attempt, pollIntervalMs]);
  return { data, error, loading, reload: () => setAttempt((value) => value + 1) };
}
