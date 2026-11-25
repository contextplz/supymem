"use client";

import { useOpsQuery } from "@/hooks/useOpsQuery";
import { MetricsCard } from "@/components/ops/MetricsCard";
import { getFinanceSummary } from "@/lib/opsApi";
import styles from "@/components/ops/ops.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

function formatCents(cents: number): string {
  return `$${(cents / 100).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatObservedCents(cents: number | null): string {
  if (cents == null) return "Not recorded";
  return formatCents(cents);
}

export default function OpsFinancePage() {
  const result = useOpsQuery(getFinanceSummary);
  const summary = result.data;
  if (result.loading) return <p role="status">Loading company data…</p>;
  if (result.error) return <ErrorNotice error={result.error} as="div" source="ops.finance.load"><p>{result.error}</p><button type="button" onClick={result.reload}>Retry</button></ErrorNotice>;

  return (
    <div className={styles.page}>
      <div className={styles.headerRow}>
        <h1 className={styles.pageTitle}>Finance</h1>
        {summary?.last_snapshot_date && (
          <p className={styles.empty}>Last snapshot: {summary.last_snapshot_date}</p>
        )}
      </div>

      {!summary && (
        <p className={styles.empty} data-dead-end="empty_no_action">
          Ops API offline, finance summary unavailable.
        </p>
      )}

      <div className={styles.metricsRow} style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
        <MetricsCard
          title="MRR"
          value={summary ? formatObservedCents(summary.mrr_cents) : "Unavailable"}
        />
        <MetricsCard
          title="ARR"
          value={summary ? formatObservedCents(summary.arr_cents) : "Unavailable"}
        />
        <MetricsCard
          title="Active Subscribers"
          value={summary?.active_subscribers ?? "Not recorded"}
        />
        <MetricsCard
          title="Stripe Balance"
          value={summary ? formatObservedCents(summary.stripe_balance_cents) : "Unavailable"}
        />
        <MetricsCard
          title="Total Ad Spend"
          value={summary ? `$${summary.total_ad_spend_usd.toFixed(2)}` : "Not recorded"}
          trend="neutral"
        />
        <MetricsCard
          title="Expenses (Month)"
          value={summary ? formatCents(summary.total_expenses_month_cents) : "Not recorded"}
          trend="neutral"
        />
      </div>
    </div>
  );
}
