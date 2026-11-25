"use client";

import styles from "./Outreach.module.css";
import { ActionFeedback, useOutreachAction } from "./useOutreachAction";

export function IngestPaste({
  onIngest,
}: {
  onIngest: (raw: string, source: "profile_paste" | "job_post_paste" | "thread_url_paste") => Promise<void>;
}) {
  const { pending, error, run } = useOutreachAction();
  return (
    <section className={styles.card}>
      <h2>Paste evidence</h2>
      <ActionFeedback pending={pending} error={error} />
      <p className={styles.muted}>
        Paste notes you already have: a LinkedIn URL you opened in your own browser, a job post, a
        public thread. We store the text you paste, we do not fetch LinkedIn. No harvest, no
        auto-send.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const fd = new FormData(e.currentTarget);
          const source = String(fd.get("source") || "profile_paste") as
            | "profile_paste"
            | "job_post_paste"
            | "thread_url_paste";
          void run(() => onIngest(String(fd.get("raw") || ""), source));
        }}
      >
        <label>
          <span className={styles.label}>Kind</span>
          <select className={styles.input} name="source" defaultValue="profile_paste">
            <option value="profile_paste">Profile</option>
            <option value="job_post_paste">Job post</option>
            <option value="thread_url_paste">Public thread</option>
          </select>
        </label>
        <label>
          <span className={styles.label}>Paste</span>
          <textarea
            className={styles.textarea}
            name="raw"
            required
            placeholder={
              'Name: Ada Founder\nCompany: Acme\nhttps://www.linkedin.com/in/… (copied from your browser)\n"we still run this in a spreadsheet"'
            }
          />
        </label>
        <div className={styles.row}>
          <button className={styles.btn} type="submit" disabled={pending}>
            Score this paste
          </button>
        </div>
      </form>
    </section>
  );
}
