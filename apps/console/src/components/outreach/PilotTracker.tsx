"use client";

import { useState } from "react";
import styles from "./Outreach.module.css";
import { ActionFeedback, useOutreachAction } from "./useOutreachAction";

type Pilot = {
  id: string;
  use_case: string;
  success_criterion: string;
  status: string;
  paid_decision_at?: string | null;
  permission_to_demo_at?: string | null;
};

export function PilotTracker({
  contactId,
  items,
  onCreate,
  onPatch,
}: {
  contactId: string | null;
  items: Pilot[];
  onCreate: (body: { use_case: string; success_criterion: string; team: string }) => Promise<void>;
  onPatch: (id: string, body: Record<string, unknown>) => Promise<void>;
}) {
  const { pending, error, run } = useOutreachAction();
  const [useCase, setUseCase] = useState("");
  const [criterion, setSuccess] = useState("");
  return (
    <section className={styles.card}>
      <h2>Pilots</h2>
      <ActionFeedback pending={pending} error={error} />
      <p className={styles.muted}>
        One use case, one success criterion, a recorded paid decision date. Permission to demo is recorded
        explicitly.
      </p>
      <label>
        <span className={styles.label}>Use case</span>
        <input className={styles.input} value={useCase} onChange={(e) => setUseCase(e.target.value)} />
      </label>
      <label>
        <span className={styles.label}>Success criterion</span>
        <input
          className={styles.input}
          value={criterion}
          onChange={(e) => setSuccess(e.target.value)}
          placeholder="Measurable baseline they already have"
        />
      </label>
      <div className={styles.row}>
        <button
          className={styles.btn}
          type="button"
          disabled={pending || !contactId || useCase.length < 4 || criterion.length < 8}
          onClick={() => void run(() => onCreate({ use_case: useCase, success_criterion: criterion, team: "one team" }))}
        >
          Propose pilot
        </button>
      </div>
      <div className={styles.list}>
        {items.map((p) => (
          <div key={p.id} className={styles.row}>
            <span className={styles.pill}>{p.status}</span>
            <span>{p.use_case}</span>
            <button
              className={`${styles.btn} ${styles.btnGhost}`}
              type="button"
              disabled={pending || Boolean(p.permission_to_demo_at)}
              onClick={() => void run(() => onPatch(p.id, { permission_to_demo: true }))}
            >
              Permission to demo
            </button>
            <button
              className={`${styles.btn} ${styles.btnGhost}`}
              type="button"
              disabled={pending || Boolean(p.paid_decision_at)}
              onClick={() => void run(() => onPatch(p.id, { paid_decision: true }))}
            >
              Record paid decision
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
