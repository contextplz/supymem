import styles from "./ChatDock.module.css";

export function AttachFileButton({ disabled, onClick }: { disabled?: boolean; onClick: () => void }) {
  return <button type="button" className={styles.attachButton} aria-label="Attach files"
    title="Attach files" disabled={disabled} onClick={onClick}>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m21 11-8 8a6 6 0 0 1-8.5-8.5l8-8a4 4 0 0 1 5.7 5.7l-8 8a2 2 0 0 1-2.8-2.8l7.3-7.3" />
    </svg>
    <span>Attach files</span>
  </button>;
}
