"use client";

import { useState } from "react";
import styles from "./Outreach.module.css";
import { ActionFeedback, useOutreachAction } from "./useOutreachAction";

export function InterviewCapture({
  contactId,
  onSave,
}: {
  contactId: string | null;
  onSave: (notes: string, momTestOk: boolean) => Promise<{ hypotheticals_flagged?: string[] }>;
}) {
  const { pending, error, run } = useOutreachAction();
  const [notes, setNotes] = useState("");
  const [flags, setFlags] = useState<string[]>([]);
  return (
    <section className={styles.card}>
      <h2>Interview capture</h2>
      <ActionFeedback pending={pending} error={error} />
      <p className={styles.muted}>
        2 / 12 / 4 / 2. Ask when it last happened, what they did, who owned it, what it cost. Software
        flags hypotheticals; you confirm Mom-Test.
      </p>
      <textarea
        className={styles.textarea}
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        placeholder="Paste notes. Hypothetical questions are flagged, not deleted."
        disabled={pending || !contactId}
      />
      <div className={styles.row}>
        <button
          className={styles.btn}
          type="button"
          disabled={pending || !contactId || notes.trim().length < 12}
          onClick={() => void run(async () => {
            const out = await onSave(notes, false);
            setFlags(out.hypotheticals_flagged || []);
          })}
        >
          Extract flags
        </button>
      </div>
      {flags.length > 0 && (
        <p className={styles.err}>Mom-Test flags: {flags.join(", ")}</p>
      )}
    </section>
  );
}
