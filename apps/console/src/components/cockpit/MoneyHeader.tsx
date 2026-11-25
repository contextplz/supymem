import { ProvenanceChip, type ProvenanceMode } from "@/components/primitives/ProvenanceChip";
import { EmptyState } from "@/components/primitives/EmptyState";
import styles from "./cockpit.module.css";

function formatMoney(earned: number, currency: string): string {
  try {
    return new Intl.NumberFormat("en-IN", {
      style: "currency",
      currency,
    }).format(earned);
  } catch {
    return `${earned.toFixed(2)} ${currency}`;
  }
}

function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const max = Math.max(...values, 1);
  return (
    <div className={styles.spark} aria-hidden>
      {values.slice(-14).map((value, index) => (
        <span
          key={`${index}-${value}`}
          className={styles.sparkBar}
          style={{ height: `${Math.max(8, Math.round((value / max) * 100))}%` }}
        />
      ))}
    </div>
  );
}

export function MoneyHeader({
  earned,
  chargeCount,
  provenance = earned == null ? "no_data" : "live",
  currency = "USD",
  sparkValues,
  setupHref: _setupHref,
}: {
  earned?: number | null;
  chargeCount?: number | null;
  provenance?: ProvenanceMode;
  currency?: string;
  sparkValues?: number[];
  setupHref?: string;
}) {
  const noData = provenance === "no_data" || earned == null;
  const genuineZero = !noData && earned === 0;

  return (
    <section className={styles.pulseCell} aria-label="Gross verified customer payments">
      <p className={styles.eyebrow}>Gross payments</p>
      {noData ? (
        <EmptyState
          title="○ No verified payment recorded"
          body="A live customer payment has not been independently confirmed here. Missing records do not establish that no payments occurred."
        />
      ) : (
        <>
          <p className={styles.money}>{formatMoney(earned, currency)}</p>
          {genuineZero ? (
            <p className={styles.detail}>0 verified charges recorded</p>
          ) : chargeCount != null ? (
            <p className={styles.detail}>
              {chargeCount} {chargeCount === 1 ? "charge" : "charges"}
            </p>
          ) : null}
          {sparkValues && sparkValues.length > 1 ? <Sparkline values={sparkValues} /> : null}
          <p className={styles.detail}>Refunds and fees are not reconciled. Net earnings and bank payouts are unconfirmed.</p>
        </>
      )}
      <ProvenanceChip mode={noData ? "no_data" : provenance} />
    </section>
  );
}
