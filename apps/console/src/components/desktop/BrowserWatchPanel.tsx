"use client";

import { browserWatchView, type BrowserWatchFrame } from "@/lib/browserWatch";
import styles from "./BrowserWatchPanel.module.css";

/** Live view of the Supymem browser task: latest step frame, the agent cursor, and Stop. */
export function BrowserWatchPanel({ watch, state, busy, onStop }: {
  watch: BrowserWatchFrame | null | undefined; state?: string; busy: boolean; onStop: () => void;
}) {
  const view = browserWatchView(watch, state);
  if (!view) return null;
  return (
    <section className={styles.panel} aria-label="Watching the Supymem browser">
      <div className={styles.frame}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={view.src} alt={`Supymem browser, step ${view.step}`} className={styles.image} />
        {view.cursor && <svg className={styles.cursor} style={{ left: `${view.cursor.left}%`, top: `${view.cursor.top}%` }}
          width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><path d="M2 1l13 7-5.5 1.5L7 15z" /></svg>}
      </div>
      <div className={styles.bar}>
        <span>Step {view.step}{view.label ? ` · ${view.label}` : ""}</span>
        <button type="button" data-usage-control="computer_use_stop" disabled={busy} onClick={onStop}>Stop</button>
      </div>
    </section>
  );
}
