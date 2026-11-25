import { useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import { cx } from "@/lib/cx";
import { announceUsageControl } from "@/lib/usageControl";
import styles from "./cockpit.module.css";

export function VentureHeader({
  name,
  oneLiner,
  statusCode,
  autonomyLevel,
  onRename,
}: {
  name: string;
  oneLiner?: string | null;
  statusCode?: string | null;
  autonomyLevel?: string | null;
  onRename?: (name: string) => Promise<boolean>;
}) {
  const code = (statusCode || "").toLowerCase();
  const paused = code === "paused";
  const live = !paused && (code === "live" || code === "watching" || code === "building");
  const statusLabel = paused
    ? "⏸ paused"
    : live
      ? "● autopilot on"
      : code === "archived"
        ? "archived"
        : code === "setup"
          ? "setting up"
          : code
            ? code.replace(/_/g, " ")
            : "setting up";

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!editing) setDraft(name);
  }, [name, editing]);

  async function save(e?: FormEvent) {
    e?.preventDefault();
    const next = draft.trim();
    if (!onRename || next.length < 2 || next === name) {
      setEditing(false);
      setDraft(name);
      return;
    }
    setSaving(true);
    announceUsageControl("venture_rename");
    const ok = await onRename(next);
    setSaving(false);
    if (ok) setEditing(false);
  }

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Escape") {
      setDraft(name);
      setEditing(false);
    }
  }

  return (
    <header className={styles.header}>
      <div className={styles.identity}>
        {editing ? (
          <form onSubmit={(e) => void save(e)}>
            <input
              className={styles.nameInput}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => void save()}
              onKeyDown={onKey}
              maxLength={80}
              autoFocus
              aria-label="Venture name"
              disabled={saving}
            />
          </form>
        ) : (
          <h2 className={styles.name}>
            {name}
            {onRename ? (
              <button
                type="button"
                className={styles.rename}
                onClick={() => setEditing(true)}
              >
                Edit name
              </button>
            ) : null}
          </h2>
        )}
        {oneLiner ? <p className={styles.oneliner}>{oneLiner}</p> : null}
      </div>
      <span className={cx(styles.status, live && styles.statusLive, paused && styles.statusPaused)}>
        {statusLabel}
      </span>
      {autonomyLevel ? <span className={styles.autonomy}>{autonomyLevel}</span> : null}
    </header>
  );
}
