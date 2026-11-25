"use client";

import { useEffect, useState } from "react";
import { api, verifiedSessionEpoch } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";

type Spending = { month: string; month_cost_usd_micro: number; monthly_cap_usd_micro: number | null;
  by_feature_usd_micro?: Record<string, number> };
const money = (amount: number) => `$${(amount / 1_000_000).toFixed(2)}`;
const validAmount = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const failure = "Open Keys and connections, check Your monthly spend cap and your data folder permissions, then choose Refresh spending.";

export function SpendSection({ ventureId }: { ventureId?: string }) {
  const local = useLocalMode();
  const [loaded, setLoaded] = useState<{ ventureId?: string; epoch: number; data: Spending } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const view = loaded?.ventureId === ventureId && loaded?.epoch === verifiedSessionEpoch() ? loaded.data : null;
  useEffect(() => {
    if (!local) return;
    let active = true, generation = 0;
    async function load() {
      const ticket = ++generation, epoch = verifiedSessionEpoch();
      setLoaded(null); setError(""); setBusy(true);
      try {
        const result = await api(ventureId ? `/api/ventures/${encodeURIComponent(ventureId)}/spend` : "/api/local/spend");
        if (!active || ticket !== generation || epoch !== verifiedSessionEpoch()) return;
        const data = result.data as Spending | undefined;
        if (!result.ok || !data || typeof data.month !== "string" || !/^\d{4}-\d{2}$/.test(data.month)
          || !validAmount(data.month_cost_usd_micro)
          || (data.monthly_cap_usd_micro !== null && !validAmount(data.monthly_cap_usd_micro))
          || (data.by_feature_usd_micro !== undefined && (!data.by_feature_usd_micro || typeof data.by_feature_usd_micro !== "object"
            || Array.isArray(data.by_feature_usd_micro) || !Object.values(data.by_feature_usd_micro).every(validAmount)))) {
          setError(failure);
        } else setLoaded({ ventureId, epoch, data });
      } catch {
        if (active && ticket === generation && epoch === verifiedSessionEpoch()) setError(failure);
      } finally {
        if (active && ticket === generation && epoch === verifiedSessionEpoch()) setBusy(false);
      }
    }
    void load();
    window.addEventListener("supymem-session-changed", load);
    return () => { active = false; window.removeEventListener("supymem-session-changed", load); };
  }, [ventureId, attempt, local]);
  if (!local) return null;
  return <section aria-label="Your monthly spending">
    <h2>Your monthly spending</h2>
    {busy && <p role="status">Loading your spending. Please wait.</p>}
    <ErrorNotice error={error} source="settings.local.spending" />
    {view && <>
      <p>This month ({view.month}), {ventureId ? "for this venture" : "across all your ventures"}: {money(view.month_cost_usd_micro)}</p>
      <p>Monthly limit across all your ventures: {view.monthly_cap_usd_micro === null ? "No monthly limit" : money(view.monthly_cap_usd_micro)}</p>
      <p>Your recorded costs include estimates for calls awaiting a final receipt. Check your provider account for its final bill.</p>
      {ventureId && <ul aria-label="Your venture spending by feature">{Object.entries(view.by_feature_usd_micro || {}).map(([feature, amount]) =>
        <li key={feature}>{feature.replaceAll("_", " ")}: {money(amount)}</li>)}</ul>}
    </>}
    <p><a href="/settings/local">Open Keys and connections to change your monthly spend cap</a></p>
    <button type="button" disabled={busy} aria-busy={busy} onClick={() => setAttempt(current => current + 1)}>Refresh spending</button>
  </section>;
}
