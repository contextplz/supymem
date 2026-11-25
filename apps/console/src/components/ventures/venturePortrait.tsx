import styles from "./venturePortrait.module.css";

const TILE_COLORS = [
  "#0091ea",
  "#e50914",
  "#7b1fa2",
  "#00897b",
  "#f57c00",
  "#3949ab",
  "#c2185b",
  "#43a047",
];

export function tileColor(id: string): string {
  let hash = 0;
  for (let i = 0; i < id.length; i += 1) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  return TILE_COLORS[hash % TILE_COLORS.length];
}

/** Same face tile as the venture chooser profiles. */
export function FaceMark({ seed }: { seed: string }) {
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) hash = (hash * 33 + seed.charCodeAt(i)) >>> 0;
  const variant = hash % 3;
  if (variant === 0) {
    return (
      <svg className={styles.face} viewBox="0 0 100 100" aria-hidden="true">
        <circle cx="34" cy="40" r="6" fill="#fff" />
        <circle cx="66" cy="40" r="6" fill="#fff" />
        <path d="M30 62c8 12 32 12 40 0" fill="none" stroke="#fff" strokeWidth="5" strokeLinecap="round" />
      </svg>
    );
  }
  if (variant === 1) {
    return (
      <svg className={styles.face} viewBox="0 0 100 100" aria-hidden="true">
        <rect x="28" y="34" width="10" height="14" rx="5" fill="#fff" />
        <rect x="62" y="34" width="10" height="14" rx="5" fill="#fff" />
        <path d="M28 70h44" stroke="#fff" strokeWidth="5" strokeLinecap="round" />
      </svg>
    );
  }
  return (
    <svg className={styles.face} viewBox="0 0 100 100" aria-hidden="true">
      <circle cx="34" cy="42" r="5" fill="#fff" />
      <circle cx="66" cy="42" r="5" fill="#fff" />
      <path d="M32 68c10-10 26-10 36 0" fill="none" stroke="#fff" strokeWidth="5" strokeLinecap="round" />
    </svg>
  );
}
