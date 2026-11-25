"use client";

import { routeInChat } from "@/lib/chatRouteAction";

import styles from "./Outreach.module.css";
import { ActionFeedback, useOutreachAction } from "./useOutreachAction";

type Source = {
  provider: string;
  status: string;
  connected_account_id?: string | null;
  fake?: boolean;
};

export function EvidenceConnect({
  sources,
  onAction,
}: {
  sources: Source[] | undefined;
  onAction: (provider: string, action: string) => Promise<void>;
}) {
  const { pending, error, run } = useOutreachAction();
  const rows = sources?.length
    ? sources
    : [
        { provider: "slack", status: "absent", fake: false },
        { provider: "github", status: "absent", fake: false },
        { provider: "gmail", status: "absent", fake: false },
      ];
  return (
    <section className={styles.card}>
      <h2>Evidence sources</h2>
      <ActionFeedback pending={pending} error={error} />
      <p className={styles.muted}>
        Paste still works. Connecting Slack, GitHub, or Gmail is the other first-class path. We
        will not invent names while a connector is empty, and we do not pretend OAuth finished.
      </p>
      <div className={styles.list}>
        {rows.map((s) => (
          <div key={s.provider} className={styles.row} style={{ alignItems: "center" }}>
            <span className={styles.pill}>{s.provider}</span>
            <span className={styles.muted}>
              {s.status === "connected"
                ? `connected${s.connected_account_id ? ` · ${s.connected_account_id}` : ""}`
                : s.status === "started"
                  ? "started, backfilling when OAuth lands (paste still available)"
                  : s.status === "deferred"
                    ? "connect later"
                    : "not connected"}
            </span>
            {s.status !== "connected" && (
              <>
                <button
                  className={styles.btn}
                  type="button"
                  disabled={pending}
                  onClick={() => {
                    if (!routeInChat(`/connections?connect=${encodeURIComponent(s.provider)}`)) window.location.href = `/connections?connect=${encodeURIComponent(s.provider)}`;
                  }}
                >
                  Connect {s.provider}
                </button>
                <button
                  className={`${styles.btn} ${styles.btnGhost}`}
                  type="button"
                  disabled={pending}
                  onClick={() => void run(() => onAction(s.provider, "connect_later"))}
                >
                  Connect later
                </button>
              </>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
