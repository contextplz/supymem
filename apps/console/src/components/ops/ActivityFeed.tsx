"use client";

import { useOpsActivityFeed } from "@/hooks/useOpsActivityFeed";
import { cx } from "@/lib/cx";
import styles from "./ActivityFeed.module.css";

const LEVEL_DOT: Record<string, string> = {
  success: styles.levelSuccess,
  error: styles.levelError,
  warning: styles.levelWarning,
  info: styles.levelInfo,
};

const LEVEL_AGENT: Record<string, string> = {
  success: styles.agentSuccess,
  error: styles.agentError,
  warning: styles.agentWarning,
  info: styles.agentInfo,
};

export function ActivityFeed() {
  const { events, connected, loading } = useOpsActivityFeed();

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <h2 className={styles.title}>Recorded workspace activity</h2>
        <span className={cx(styles.status, connected && styles.statusLive)}>
          <span className={cx(styles.dot, connected && styles.dotLive)} />
          {loading ? "Loading…" : connected ? "Synced" : "Connection unavailable"}
        </span>
      </div>
      <div className={styles.list}>
        {events.length === 0 && connected && (
          <p className={styles.empty}>
            No historical workspace activity is recorded here. Durable workflow tasks are shown in Tasks.
          </p>
        )}
        {events.map((e) => (
          <div key={e.id} className={styles.row}>
            <span className={cx(styles.levelDot, LEVEL_DOT[e.level])} />
            <div>
              <span className={cx(styles.agent, LEVEL_AGENT[e.level] ?? styles.agentDefault)}>
                {e.agent_type}
              </span>
              <span className={styles.sep}>·</span>
              <span className={styles.summary}>{e.summary}</span>
              <div className={styles.time}>
                {new Date(e.created_at).toLocaleTimeString()}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
