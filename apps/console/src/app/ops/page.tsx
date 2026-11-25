"use client";

import { useOpsQuery } from "@/hooks/useOpsQuery";
import { ActivityFeed } from "@/components/ops/ActivityFeed";
import { AgentStatusGrid } from "@/components/ops/AgentStatusGrid";
import { MetricsCard } from "@/components/ops/MetricsCard";
import styles from "@/components/ops/ops.module.css";
import { getDashboardSummary } from "@/lib/opsApi";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export default function OpsDashboardPage() {
  const result = useOpsQuery(getDashboardSummary, 30000);
  const summary = result.data;
  if (result.loading) return <p role="status">Loading company data…</p>;
  if (result.error) return <ErrorNotice error={result.error} as="div" source="ops.dashboard.load"><p>{result.error}</p><button type="button" onClick={result.reload}>Retry</button></ErrorNotice>;
  const kpis = summary?.kpis ?? {};

  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>Dashboard</h1>

      <div className={styles.metricsRow}>
        <MetricsCard
          title="Tasks Created Today (UTC)"
          value={summary?.tasks_today_total ?? "Not recorded"}
          subtitle={`${summary?.tasks_today_completed ?? 0} completed · ${summary?.tasks_today_failed ?? 0} failed`}
        />
        {summary?.kpis && <>
        <MetricsCard
          title="Configured MRR"
          value={kpis.mrr_usd != null ? `$${kpis.mrr_usd}` : "Not recorded"}
        />
        <MetricsCard
          title="Configured Active Customers"
          value={(kpis.active_customers as string | number | undefined) ?? "Not recorded"}
        />
        <MetricsCard
          title="Configured Churn Rate"
          value={kpis.churn_rate != null ? `${kpis.churn_rate}%` : "Not recorded"}
          trend={Number(kpis.churn_rate) > 5 ? "down" : "neutral"}
        />
        </>}
      </div>
      {summary?.kpis && <p>Workspace-configured values; not verified product analytics.</p>}

      {!summary && (
        <p className={styles.empty} data-dead-end="empty_no_action">
          Ops API offline, metrics will appear when `/api/v1/dashboard/summary` is reachable.
        </p>
      )}

      <div className={styles.split}>
        <div className={styles.splitCol}>
          <h2 className={styles.sectionLabel}>Agent Status</h2>
          <AgentStatusGrid />
        </div>
        <div className={styles.splitCol}>
          <h2 className={styles.sectionLabel}>Historical workspace activity</h2>
          <div className={styles.feedWrap}>
            <ActivityFeed />
          </div>
        </div>
      </div>
    </div>
  );
}
