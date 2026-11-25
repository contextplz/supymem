"use client";

import { useState } from "react";
import styles from "./Outreach.module.css";
import { ActionFeedback, useOutreachAction } from "./useOutreachAction";

export function DraftReview({
  draft,
  onCopy,
  onSent,
  onWhy,
}: {
  draft: {
    id: string;
    body: string | null;
    blocked?: boolean;
    reason?: string | null;
    personalize?: string;
    explain?: string | null;
    band?: string | null;
    sent_at?: string | null;
  } | null;
  onCopy: () => Promise<void>;
  onSent: () => Promise<void>;
  onWhy: (why: string) => Promise<void>;
}) {
  const { pending, error, run } = useOutreachAction();
  const [why, setWhy] = useState("");
  if (!draft) {
    return (
      <section className={styles.card}>
        <h2>Draft</h2>
        <ActionFeedback pending={pending} error={error} />
        <p className={styles.muted}>Pick someone from the queue or paste evidence first.</p>
      </section>
    );
  }
  const copyEnabled = Boolean(draft.body) && !draft.blocked && draft.personalize === "ok";
  return (
    <section className={styles.card}>
      <h2>Draft review</h2>
      <ActionFeedback pending={pending} error={error} />
      <p className={styles.muted}>
        Copy this, send it yourself on LinkedIn (or email). We never send LinkedIn messages. Then
        mark sent so the queue stays honest.
      </p>
      <p className={styles.muted}>
        Band <span className={styles.pill}>{draft.band || "Not recorded"}</span> · personalize{" "}
        {draft.personalize || "Not recorded"}
      </p>
      {draft.explain && <p className={styles.muted}>{draft.explain}</p>}
      {draft.blocked && (
        <p className={styles.err}>
          We will not fake a personal note ({draft.reason || "refused"}). Skip or use a generic
          one-pager.
        </p>
      )}
      <pre className={styles.textarea} style={{ whiteSpace: "pre-wrap" }}>
        {draft.body || "(no body)"}
      </pre>
      <label>
        <span className={styles.label}>Why this person this week (required before mark sent)</span>
        <input
          className={styles.input}
          value={why}
          onChange={(e) => setWhy(e.target.value)}
          placeholder="They posted a hiring complaint this week"
        />
      </label>
      <div className={styles.row}>
        <button className={`${styles.btn} ${styles.btnGhost}`} type="button" disabled={pending} onClick={() => void run(() => onWhy(why))}>
          Save why
        </button>
        <button className={styles.btn} type="button" disabled={pending || !copyEnabled} onClick={() => void run(onCopy)}>
          Copy
        </button>
        <button
          className={`${styles.btn} ${styles.btnGhost}`}
          type="button"
          disabled={pending || !draft.body || Boolean(draft.sent_at)}
          onClick={() => void run(onSent)}
        >
          Mark sent
        </button>
      </div>
    </section>
  );
}
