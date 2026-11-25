"use client";

import { ageMs, formatAge, RelativeTime } from "./RelativeTime";
import styles from "./Heartbeat.module.css";

export const HEARTBEAT_STALE_MS = 90_000;

export function isHeartbeatStale(iso?: string | null): boolean {
  const age = ageMs(iso);
  return age == null || age > HEARTBEAT_STALE_MS;
}

export function Heartbeat({ at }: { at?: string | null }) {
  const stale = isHeartbeatStale(at);
  const label = at ? `last beat ${formatAge(at) || "unknown"}` : "no heartbeat";

  return (
    <span className={stale ? styles.stopped : styles.live} title={at || undefined}>
      <span className={styles.dot} aria-hidden />
      {at ? (
        <>
          last beat <RelativeTime iso={at} />
        </>
      ) : (
        label
      )}
    </span>
  );
}
