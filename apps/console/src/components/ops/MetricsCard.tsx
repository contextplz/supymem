import { cx } from "@/lib/cx";
import styles from "./MetricsCard.module.css";

export interface MetricsCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  trend?: "up" | "down" | "neutral";
  loading?: boolean;
}

export function MetricsCard({ title, value, subtitle, trend, loading }: MetricsCardProps) {
  const trendCls =
    trend === "up" ? styles.up : trend === "down" ? styles.down : styles.neutral;

  return (
    <div className={styles.card}>
      <p className={styles.title}>{title}</p>
      {loading ? (
        <div className={styles.skeleton} aria-hidden />
      ) : (
        <p className={styles.value}>{value}</p>
      )}
      {subtitle && <p className={cx(styles.subtitle, trendCls)}>{subtitle}</p>}
    </div>
  );
}
