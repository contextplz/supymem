"use client";

import type { ChatMessage } from "@/lib/api-types";
import { safeUrl } from "@/components/planning/CaseReferences";
import styles from "@/components/planning/CaseReferences.module.css";

export function IdeaReferences({ references }: { references?: ChatMessage["founder_references"] }) {
  if (!references || (!references.startups.length && !references.yc_advice.length)) return null;
  return <section className={`${styles.section} ${styles.coaching}`} aria-label="Founder references">
    {references.startups.length > 0 && <>
      <h2>Startups that tried this</h2>
      {references.startups.map(item => <article key={item.id} className={styles.card}>
        <header className={styles.coachingHeading}><h3>{item.name}</h3><span className={styles.caseStudy}>Case study</span></header>
        <p className={styles.meta}>{item.relevance_reason}</p>
        <dl className={styles.coachingRows}>
          <div><dt>What they did</dt><dd>{item.what_they_did}</dd></div>
          <div><dt>Reported result</dt><dd>{item.outcome}</dd></div>
          <div><dt>One lesson</dt><dd>{item.lesson}</dd></div>
        </dl>
        <p className={styles.meta}>{item.important_difference}</p>
        {safeUrl(item.url) && <a href={safeUrl(item.url)} target="_blank" rel="noopener noreferrer">Read source</a>}
      </article>)}
      <p className={styles.meta}>Historical accounts from the case-study workbook, not independently verified by Supymem. Their result isn’t proof yours will work.</p>
    </>}
    {references.yc_advice.length > 0 && <>
      <h2>YC advice for your stage</h2>
      {references.yc_advice.map(item => <article key={item.id} className={styles.card}>
        <h3>{item.title}</h3><p>{item.line}</p>
        {safeUrl(item.url) && <a href={safeUrl(item.url)} target="_blank" rel="noopener noreferrer">Watch source</a>}
      </article>)}
      <p className={styles.meta}>Transcript-derived talk advice; verify against the video. Not affiliated with or endorsed by Y Combinator.</p>
    </>}
  </section>;
}
