import { EmptyState } from "@/components/primitives/EmptyState";
import { ChatAnchor } from "@/components/chat/ChatLink";
import styles from "./cockpit.module.css";

export type BlockerItem = {
  id: string;
  title: string;
  detail?: string;
  href?: string;
  actionLabel?: string;
};

export function Blockers({ items }: { items?: BlockerItem[] }) {
  const rows = items || [];

  return (
    <section className={styles.card} aria-labelledby="blockers-heading">
      <div className={styles.sectionHeader}>
        <h2 id="blockers-heading">Blockers</h2>
        <span className={styles.detail}>{rows.length}</span>
      </div>
      {rows.length ? (
        <>
          <p className={styles.sectionLede}>Work that cannot continue until something unblocks.</p>
          <div>
            {rows.map((item) => (
              <div className={styles.need} key={item.id}>
                <p className={styles.needTitle}>{item.title}</p>
                {item.detail ? <p className={styles.detail}>{item.detail}</p> : null}
                {item.href ? (
                  <ChatAnchor href={item.href} className="ghost sm">
                    {item.actionLabel || "Unblock this"}
                  </ChatAnchor>
                ) : null}
              </div>
            ))}
          </div>
        </>
      ) : (
        <EmptyState
          title="No blockers"
          body="Nothing is stuck. The company should keep running."
        />
      )}
    </section>
  );
}
