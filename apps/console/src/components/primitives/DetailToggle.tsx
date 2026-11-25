"use client";

import { useDetailLevel, type DetailLevel } from "@/lib/detailLevel";
import styles from "./OneCard.module.css";

export function DetailToggle() {
  const OPTS: { v: DetailLevel; label: string }[] = [{ v: "brief", label: "Simple" }, { v: "detailed", label: "Detailed" }];
  const [level, setLevel] = useDetailLevel();
  return (
    <div className={styles.toggle} role="group" aria-label="Detail level">
      {OPTS.map((o) => (
        <button key={o.v} type="button" className={styles.toggleBtn} aria-pressed={level === o.v} onClick={() => setLevel(o.v)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
