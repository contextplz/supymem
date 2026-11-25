"use client";

import styles from "./Outreach.module.css";
import { ChatAnchor } from "@/components/chat/ChatLink";
import { ActionFeedback, useOutreachAction } from "./useOutreachAction";

export type QueueItem = {
  id: string;
  first_name: string | null;
  email: string | null;
  role: string | null;
  band: string | null;
  explain: string | null;
  company: string | null;
};

export function DailyQueue({
  items,
  ready,
  remaining,
  outreachWeek,
  onPick,
}: {
  items: QueueItem[];
  ready: boolean;
  remaining?: { connect?: number; dm?: number; email?: number } | null;
  outreachWeek?: number | null;
  onPick: (id: string) => void | Promise<void>;
}) {
  const { pending, error, run } = useOutreachAction();
  return (
    <section className={styles.card}>
      <h2>Customers to review</h2>
      <ActionFeedback pending={pending} error={error} />
      {(outreachWeek || remaining) && (
        <p className={styles.muted}>
          Week {outreachWeek ?? "Not recorded"} · remaining connect {remaining?.connect ?? "Not recorded"} / DM{" "}
          {remaining?.dm ?? "Not recorded"} / email {remaining?.email ?? "Not recorded"}
        </p>
      )}
      {!ready && (
        <p className={styles.muted}>
          Your customer list will show real people you can review. Start with the missing outreach detail above.
        </p>
      )}
      {ready && items.length === 0 && (
        <div><p className={styles.muted}>No customers to review yet. Your list will show real people and where you found them.</p><ChatAnchor className={styles.btn} href="/gtm">Find my first customers</ChatAnchor></div>
      )}
      <div className={styles.list}>
        {items.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={pending}
            onClick={() => void run(() => onPick(item.id))}
          >
            <span className={styles.pill}>{item.band || "unscored"}</span>{" "}
            {item.first_name || "Unknown"} {item.company ? `· ${item.company}` : ""}
            <div className={styles.muted}>{item.explain}</div>
          </button>
        ))}
      </div>
    </section>
  );
}
