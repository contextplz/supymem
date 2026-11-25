"use client";

import { useState } from "react";
import { ChatAnchor } from "@/components/chat/ChatLink";
import type { ConnectionState } from "@/lib/api-types";
import { ProvenanceChip, type ProvenanceMode } from "@/components/primitives/ProvenanceChip";
import { RelativeTime } from "@/components/primitives/RelativeTime";
import { EmptyState } from "@/components/primitives/EmptyState";
import styles from "./cockpit.module.css";

export type NowRailItem = {
  id: string;
  text: string;
  timestamp?: string;
  provenance?: ProvenanceMode;
  query?: string | null;
};

function clockTime(timestamp?: string) {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  if (Number.isNaN(date.valueOf())) return "";
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(date);
}

export function NowRail({
  items,
  connection,
  timelineHref,
}: {
  items?: NowRailItem[];
  connection?: ConnectionState;
  timelineHref?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const all = [...(items || [])].reverse();
  const visibleItems = expanded ? all.slice(0, 24) : all.slice(0, 4);
  const reconnecting = connection === "reconnecting" || connection === "offline";
  const headerMode: ProvenanceMode = reconnecting
    ? "no_data"
    : visibleItems.some((item) => item.provenance === "simulated") &&
        !visibleItems.some((item) => item.provenance !== "simulated")
      ? "simulated"
      : visibleItems.length
        ? "live"
        : "no_data";

  return (
    <section className={styles.card} aria-labelledby="now-heading">
      <div className={styles.sectionHeader}>
        <h2 id="now-heading">Now</h2>
        {reconnecting ? (
          <span className={styles.reconnect}>
            {connection === "offline" ? "Offline, polling" : "Reconnecting…"}
          </span>
        ) : (
          <ProvenanceChip mode={headerMode} />
        )}
      </div>
      {visibleItems.length ? (
        <div>
          {visibleItems.map((item) => {
            const simulated = item.provenance === "simulated";
            return (
              <div className={`${styles.activity} ${styles.now}`} key={item.id}>
                <span className={styles.timestamp}>
                  {clockTime(item.timestamp) || <RelativeTime iso={item.timestamp} />}
                </span>
                {simulated ? (
                  <span className={styles.simMark} aria-label="Simulated">
                    ◐
                  </span>
                ) : null}
                <span className={styles.activityText}>
                  {item.query ? `Searching: ${item.query}` : item.text}
                </span>
                {simulated ? <ProvenanceChip mode="simulated" /> : null}
              </div>
            );
          })}
        </div>
      ) : (
        <EmptyState
          title="Quiet so far"
          body="As the team works, the last few actions will stream here."
        />
      )}
      <div className={styles.urlRow}>
        {all.length > 4 && (
          <button type="button" className="ghost sm" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "Show less" : `▸ ${all.length - 4} more`}
          </button>
        )}
        {timelineHref ? (
          <ChatAnchor href={timelineHref} className="ghost sm">
            ▸ full timeline
          </ChatAnchor>
        ) : null}
      </div>
    </section>
  );
}
