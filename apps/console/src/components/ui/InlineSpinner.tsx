import styles from "./InlineSpinner.module.css";

export function InlineSpinner() {
  return <span className={styles.spinner} aria-hidden="true" />;
}
