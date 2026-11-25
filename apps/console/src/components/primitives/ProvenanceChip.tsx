"use client";

import styles from "./ProvenanceChip.module.css";

export type ProvenanceMode = "live" | "simulated" | "projected" | "no_data";

const labels: Record<ProvenanceMode, string> = {
  live: "● live",
  simulated: "◐ simulated",
  projected: "◑ projected",
  no_data: "○ no data",
};

export function ProvenanceChip({ mode }: { mode: ProvenanceMode }) {
  return (
    <span className={`${styles.chip} ${styles[mode]}`} aria-label={`Data provenance: ${labels[mode]}`}>
      {labels[mode]}
    </span>
  );
}
