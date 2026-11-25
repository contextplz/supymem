import type { ResearchProgress as Progress } from "@/lib/durableJobs";
import styles from "./ResearchProgress.module.css";

export function ResearchProgress({ progress }: { progress?: Progress | null }) {
  if (!progress) return null;
  const queued = progress.state === "queued";
  const percent = typeof progress.percent === "number" && Number.isFinite(progress.percent)
    && progress.percent >= 0 && progress.percent <= 100 ? progress.percent : null;
  return <div className={styles.progress}>
    <span>{progress.step_label}{!queued && percent !== null ? ` · ${percent}%` : ""}</span>
    {(queued || percent !== null) && <progress className={styles.bar} role="progressbar"
      aria-label={progress.step_label} aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={queued ? undefined : percent!} max={100} value={queued ? undefined : percent!} />}
  </div>;
}
