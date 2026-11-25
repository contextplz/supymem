"use client";

import { plainJob } from "@/lib/jobCopy";

import { useOpsQuery } from "@/hooks/useOpsQuery";
import { cx } from "@/lib/cx";
import { getTasks } from "@/lib/opsApi";
import styles from "@/components/ops/ops.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

const STATUS_CLS: Record<string, string> = {
  completed: styles.badgeOk,
  in_progress: styles.badgeInfo,
  pending: styles.badgeWarn,
  failed: styles.badgeDanger,
};

export default function OpsTasksPage() {
  const result = useOpsQuery(getTasks, 30000);
  const tasks = result.data || [];
  if (result.loading) return <p role="status">Loading company data…</p>;
  if (result.error) return <ErrorNotice error={result.error} as="div" source="ops.tasks.load"><p>{result.error}</p><button type="button" onClick={result.reload}>Retry</button></ErrorNotice>;

  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>Tasks</h1>
      <div className={styles.stack}>
        {tasks.length === 0 && (
          <p className={styles.empty} data-dead-end="empty_no_action">
            No tasks have been recorded for this company selection.
          </p>
        )}
        {tasks.map((t) => (
          <div key={t.id} className={styles.rowCard}>
            <div className={styles.rowBody}>
              <p className={styles.rowTitle}>{t.title}</p>
              <p className={styles.rowMeta}>
                {t.agent_type.replace(/_/g, " ")} · Priority {t.priority}
              </p>
              <p className={styles.rowMeta}>
                {new Date(t.created_at).toLocaleString()} · {t.provenance === "durable_job" ? "Durable workflow" : "Historical workspace task"}
                {t.error_code ? ` · ${plainJob(t).body}` : ""}
              </p>
            </div>
            <span className={cx(styles.badge, STATUS_CLS[t.status])}>{plainJob(t).title}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
