import type { AgentWork } from "./OfficeProvider";
import styles from "./office.module.css";

export function WorkLines({
  work,
  compact = false,
}: {
  work?: AgentWork | null;
  compact?: boolean;
}) {
  if (!work) return null;
  if (compact) {
    return (
      <>
        {work.doing && <span className={styles.teamBrief}>{work.doing}</span>}
        {work.next && (
          <span className={styles.teamBriefNext}>Next: {work.next}</span>
        )}
      </>
    );
  }
  return (
    <>
      <section>
        <p className={styles.deskLabel}>Did</p>
        <p className={styles.deskNote}>{work.did}</p>
      </section>
      <section>
        <p className={styles.deskLabel}>Doing now</p>
        <p className={styles.deskNote}>{work.doing}</p>
      </section>
      <section>
        <p className={styles.deskLabel}>Next</p>
        <p className={styles.deskNote}>{work.next}</p>
        {work.next_provisional && (
          <p className={styles.deskHint}>Queued, may change as the plan updates.</p>
        )}
      </section>
    </>
  );
}
