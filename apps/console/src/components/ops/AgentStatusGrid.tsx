"use client";

import { useOpsAgentStatus } from "@/hooks/useOpsAgentStatus";
import { cx } from "@/lib/cx";
import styles from "./AgentStatusGrid.module.css";

const STATUS_CLS: Record<string, string> = {
  completed: styles.completed,
  in_progress: styles.running,
  failed: styles.failed,
};

export function AgentStatusGrid() {
  const { statuses, loading, offline } = useOpsAgentStatus();

  if (loading) {
    return (
      <div className={styles.grid}>
        {Array.from({ length: 9 }).map((_, i) => (
          <div key={i} className={styles.skeleton} aria-hidden />
        ))}
      </div>
    );
  }

  if (offline || statuses.length === 0) {
    return (
      <div className={styles.grid}>
        <p className={styles.empty} data-dead-end="empty_no_action">
          {offline
            ? "Ops API offline, agent status unavailable."
            : "No agents reported yet."}
        </p>
      </div>
    );
  }

  return (
    <div className={styles.grid}>
      {statuses.map((s) => {
        const statusCls = STATUS_CLS[s.observed_status ?? ""] ?? "";
        return (
          <div key={s.id} className={styles.card}>
            <p className={styles.name}>
              {s.label}
            </p>
            <span className={cx(styles.badge, statusCls)}>
              {s.observed_status ?? "not reported"}
            </span>
            <p className={styles.meta}>{s.tasks_today} tasks created today (UTC) · Latest recorded state</p>
          </div>
        );
      })}
    </div>
  );
}
