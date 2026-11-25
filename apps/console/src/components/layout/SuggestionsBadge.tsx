"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/client";
import { SUGGESTIONS_CHANGED, badgeLabel, suggestionsPath, type SuggestionList } from "@/lib/suggestions";

/** Count of new suggestions in the sidebar's venture scope. */
export function SuggestionsBadge({ ventureId }: { ventureId?: string }) {
  const [count, setCount] = useState(0);
  useEffect(() => {
    let live = true;
    setCount(0);
    const load = async () => {
      const r = await api(ventureId ? suggestionsPath(ventureId, "venture") : "/api/suggestions");
      if (live && r.ok) { const d = r.data as SuggestionList; setCount(d.new || 0); }
    };
    void load();
    const timer = window.setInterval(() => void load(), 60_000);
    window.addEventListener(SUGGESTIONS_CHANGED, load);
    return () => { live = false; window.clearInterval(timer); window.removeEventListener(SUGGESTIONS_CHANGED, load); };
  }, [ventureId]);
  const label = badgeLabel(count);
  if (!label) return null;
  return (
    <span aria-label={`${count} waiting ${ventureId ? "in this venture" : "across all ventures"}`} style={{ marginLeft: "auto", background: "var(--accent)", color: "var(--accent-ink)", borderRadius: 999, fontSize: 11, padding: "1px 7px", minWidth: 18, textAlign: "center" }}>
      {label}
    </span>
  );
}
