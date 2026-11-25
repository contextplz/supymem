"use client";

import type { CSSProperties, ReactNode } from "react";
import { cx } from "@/lib/cx";
import { evidenceSegments, noDash, sourceDomain } from "@/lib/researchBrief";
import styles from "./ResearchBrief.module.css";

const VERDICT_BADGE = { proceed: "Worth building", test_first: "Test first", not_yet: "Not yet" } as const;

export function displayResearchText(value?: string | null): string {
  const text = value?.trim() || "";
  return /^(unknown\b|still unknown:|potential existing solution;|source excerpt and relevance|insufficient public|no (strong public |direct |usable |completed |cited |relevant )?(evidence|sources?|pricing|prior attempts)|search excerpts are leads)|\b(need(s)? verification|does not (imply|establish|prove)|not proof of market demand)\b/i.test(text) ? "" : text;
}

export function VerdictBadge({ verdict }: { verdict: keyof typeof VERDICT_BADGE }) {
  return <span className={cx(styles.badge, styles[verdict])}>{VERDICT_BADGE[verdict]}</span>;
}

/** CSS-only count-up: the real number stays in the accessible label. */
export function CountUp({ value, label }: { value: number; label: string }) {
  return <span className={styles.count} role="img" aria-label={`${value} ${label}`} style={{ "--to": value } as CSSProperties} />;
}

export function Stat({ value, label }: { value: number; label: string }) {
  return <div className={styles.stat}><CountUp value={value} label={label} /><span className={styles.statLabel}>{label}</span></div>;
}

/** Coverage meter: how many research areas have any evidence. Not a quality score. */
export function CoverageMeter({ known, total }: { known: number; total: number }) {
  if (!total) return null;
  return <div className={styles.meter} role="meter" aria-valuemin={0} aria-valuemax={total} aria-valuenow={known}
    aria-label={`${known} of ${total} research areas have evidence`}>
    <div className={styles.meterTrack}><div className={styles.meterFill} style={{ "--pct": `${(known / total) * 100}%` } as CSSProperties} /></div>
    <span className={styles.meterText}>{known} of {total} areas have evidence</span>
  </div>;
}

/** Three-segment evidence strength bar; unknown renders empty. */
export function StrengthBar({ status, support = 0 }: { status?: string; support?: number }) {
  const on = evidenceSegments(status, support);
  return <span className={styles.strength} aria-hidden="true">{[0, 1, 2].map((i) => <i key={i} className={i < on ? styles.segOn : undefined} />)}</span>;
}

export function SourceChip({ url, title }: { url: string | null | undefined; title?: string }) {
  const domain = sourceDomain(url);
  if (!domain) return null;
  if (domain === "invalid" || domain.endsWith(".invalid")) return <span className={styles.chip} title={title}>Sample source, not a real website</span>;
  return <a className={styles.chip} href={url!} target="_blank" rel="noopener noreferrer" title={title || domain}><span className={styles.initial} aria-hidden="true">{domain.charAt(0).toUpperCase()}</span>{domain}</a>;
}

/** Indeterminate shimmer shown while research runs, so the page never looks stuck. */
export function RunningShimmer({ children }: { children: ReactNode }) {
  return <div className={styles.running} role="status"><div className={styles.shimmer} aria-hidden="true" /><p>{children}</p></div>;
}

export function Reveal({ children, delay = 0 }: { children: ReactNode; delay?: number }) {
  return <div className={styles.reveal} style={{ "--delay": `${delay}ms` } as CSSProperties}>{children}</div>;
}

export function KeyLines({ lines }: { lines: string[] }) {
  return <ol className={styles.lines}>{lines.map((l, i) => <li key={i} style={{ "--delay": `${i * 90}ms` } as CSSProperties}>{l}</li>)}</ol>;
}

export function RiskLine({ text }: { text: string }) {
  return <p className={styles.risk}><span>Top risk</span>{noDash(text)}</p>;
}
