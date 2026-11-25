"use client";

import { useOpsAgentStatus } from "@/hooks/useOpsAgentStatus";
import styles from "@/components/ops/ops.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export default function OpsAgentsPage() {
  const { statuses, loading, error } = useOpsAgentStatus();
  return <div className={styles.page}>
    <h1 className={styles.pageTitle}>Workflow status</h1>
    <p>Latest recorded work in this company selection. These records do not indicate a continuously running agent. Start work from its Idea, Plan, Build or other workflow.</p>
    {loading ? <p role="status">Loading workflow records…</p> : error ? <ErrorNotice error={error} source="ops.agents.load" /> : !statuses.length ? <p>No workflow records yet.</p> :
      <div className={styles.stack}>{statuses.map(s => <div key={s.id} className={styles.rowCard}>
        <div className={styles.rowBody}>
          <h3 className={styles.rowTitle}>{s.label}</h3>
          <p className={styles.rowMeta}>Latest recorded state: {s.observed_status}</p>
          <p className={styles.rowMeta}>{s.observed_at ? new Date(s.observed_at).toLocaleString() : "Not reported"} · {s.tasks_today} tasks created today (UTC) · {s.tasks_total} total</p>
          <p className={styles.rowMeta}>{s.provenance === "durable_job" ? "Durable workflow records" : "Historical workspace operations"}</p>
        </div>
      </div>)}</div>}
  </div>;
}
