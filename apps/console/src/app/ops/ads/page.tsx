"use client";

import { useOpsQuery } from "@/hooks/useOpsQuery";
import { getAdCampaigns } from "@/lib/opsApi";
import styles from "@/components/ops/ops.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export default function OpsAdsPage() {
  const result = useOpsQuery(getAdCampaigns);
  const campaigns = result.data || [];
  if (result.loading) return <p role="status">Loading company data…</p>;
  if (result.error) return <ErrorNotice error={result.error} as="div" source="ops.ads.load"><p>{result.error}</p><button type="button" onClick={result.reload}>Retry</button></ErrorNotice>;

  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>Ad Campaigns</h1>
      <div className={styles.stack}>
        {campaigns.length === 0 && (
          <p className={styles.empty} data-dead-end="empty_no_action">No campaigns yet.</p>
        )}
        {campaigns.map((c) => (
          <div key={c.id} className={styles.rowCard}>
            <div className={styles.rowBody}>
              <p className={styles.rowTitle}>{c.name}</p>
              <p className={styles.rowMeta}>
                {c.platform} · {c.status}
              </p>
            </div>
            <div style={{ textAlign: "right" }}>
              <p className={styles.rowTitle}>Spend data unavailable</p>
              <p className={styles.rowMeta}>${c.daily_budget_usd}/day budget</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
