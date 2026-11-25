"use client";

import { useState, type ReactNode } from "react";
import { useDetailLevel } from "@/lib/detailLevel";
import { cx } from "@/lib/cx";
import styles from "./OneCard.module.css";

export type OneCardStatus = "idle" | "working" | "done" | "locked" | "needs_you" | "error";

export function OneCard({ id, step, title, body, status = "idle", actions, actionsFirst = false, details, children }: {
  id?: string; step?: string; title: string; body?: string; status?: OneCardStatus;
  actions?: ReactNode; actionsFirst?: boolean; details?: ReactNode; children?: ReactNode;
}) {
  const [level] = useDetailLevel();
  const [open, setOpen] = useState(false);
  const showDetails = details != null && (level === "detailed" || open);
  const actionRow = actions && status !== "locked" ? <div className={styles.actions}>{actions}</div> : null;
  return (
    <section id={id} className={cx(styles.card, styles[status])} aria-busy={status === "working"} data-status={status}
      data-error-notice={status === "error" ? "" : undefined}
      data-dead-end={status === "needs_you" ? "empty_no_action" : undefined}>
      <div className={styles.inner}>
        {step ? <div className={styles.eyebrow}>{step}</div> : null}
        <h2 className={styles.title}>
          {status === "done" ? <span aria-hidden="true" className={styles.check}>✓ </span> : null}
          {title}
        </h2>
        {body ? <p className={styles.body}>{body}</p> : null}
        {status === "working" ? (
          <div className={styles.workingIndicator} role="status"><span className={styles.spinner} aria-hidden="true" />Working…</div>
        ) : null}
        {status === "locked" ? <p className={styles.lock}>Locked until the step before is done.</p> : null}
        {actionsFirst ? actionRow : null}
        {children}
        {actionsFirst ? null : actionRow}
        {showDetails ? <div className={styles.details}>{details}</div> : null}
        {details != null && level === "brief" && status !== "locked" ? (
          <button type="button" className={styles.expand} aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            {open ? "Show less" : "Show all options"}
          </button>
        ) : null}
      </div>
    </section>
  );
}

export function OneCardStack({ children, flow = false }: { children: ReactNode; flow?: boolean }) {
  return <div className={cx(styles.stack, flow && styles.flow)}>{children}</div>;
}
