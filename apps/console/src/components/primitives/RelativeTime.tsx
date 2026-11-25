"use client";

import { useEffect, useState } from "react";
import { relativeTime } from "@/lib/format";

export function ageMs(iso?: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Date.now() - t;
}

export function formatAge(iso?: string | null): string {
  const age = ageMs(iso);
  if (age == null) return "";
  const sec = Math.max(0, Math.round(age / 1000));
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  return relativeTime(iso);
}

export function RelativeTime({ iso }: { iso?: string | null }) {
  const [, setTick] = useState(0);

  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), 4000);
    return () => window.clearInterval(id);
  }, []);

  if (!iso) return null;
  const label = formatAge(iso) || relativeTime(iso);
  if (!label) return null;
  return <time dateTime={iso}>{label}</time>;
}
