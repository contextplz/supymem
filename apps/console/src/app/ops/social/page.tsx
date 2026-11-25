"use client";

import { useOpsQuery } from "@/hooks/useOpsQuery";
import { cx } from "@/lib/cx";
import { getSocialPosts } from "@/lib/opsApi";
import styles from "@/components/ops/ops.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export default function OpsSocialPage() {
  const result = useOpsQuery(getSocialPosts);
  const posts = result.data || [];
  if (result.loading) return <p role="status">Loading company data…</p>;
  if (result.error) return <ErrorNotice error={result.error} as="div" source="ops.social.load"><p>{result.error}</p><button type="button" onClick={result.reload}>Retry</button></ErrorNotice>;

  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>Social Media</h1>
      <div className={styles.stack}>
        {posts.length === 0 && (
          <p className={styles.empty} data-dead-end="empty_no_action">No posts yet.</p>
        )}
        {posts.map((p) => (
          <div key={p.id} className={styles.rowCard}>
            <div className={styles.rowBody}>
              <p className={styles.rowTitle}>{p.content}</p>
              <p className={styles.rowMeta}>
                <span
                  className={cx(
                    p.status === "published" ? styles.statusOk : styles.statusWarn,
                  )}
                >
                  {p.status}
                </span>
                {p.published_at && (
                  <> · {new Date(p.published_at).toLocaleDateString()}</>
                )}
                {p.engagement &&
                  Object.entries(p.engagement).map(([k, v]) => (
                    <span key={k}>
                      {" "}
                      · {k}: {v}
                    </span>
                  ))}
              </p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
