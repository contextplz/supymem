"use client";

import { useState, type ReactNode } from "react";
import { api } from "@/lib/client";
import { renderLightMd } from "@/lib/lightMd";
import { cx } from "@/lib/cx";
import styles from "./TaskBrief.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Brief = {
  body_md?: string;
  source?: string;
  phase?: string;
  cached?: boolean;
};

const cache = new Map<string, Brief>();

function cacheKey(ventureId: string, taskId: string, title: string) {
  return `${ventureId}:${taskId}:${title}`;
}

export function TaskBrief({
  ventureId,
  taskId,
  title,
  className,
  children,
  detail,
}: {
  ventureId: string;
  taskId: string;
  title: string;
  className?: string;
  children?: ReactNode;
  /** Saved task detail: shown as-is, so reading it never needs a new summary. */
  detail?: string | null;
}) {
  const key = cacheKey(ventureId, taskId, title);
  const [open, setOpen] = useState(false);
  const [brief, setBrief] = useState<Brief | null>(() => cache.get(key) || null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    if (cache.has(key)) {
      setBrief(cache.get(key) || null);
      return;
    }
    setLoading(true);
    setError("");
    const r = await api(`/api/ventures/${ventureId}/tasks/${encodeURIComponent(taskId)}/summary`, {
      method: "POST",
      body: JSON.stringify({ title }),
    });
    setLoading(false);
    if (!r.ok) {
      setError("Couldn’t write a summary for this yet.");
      return;
    }
    const data = (r.data || {}) as Brief;
    if (!data.body_md) {
      setError("Couldn’t write a summary for this yet.");
      return;
    }
    cache.set(key, data);
    setBrief(data);
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !detail) void load();
  }

  return (
    <div>
      <button
        type="button"
        className={cx(styles.trigger, className)}
        aria-expanded={open}
        onClick={toggle}
      >
        <span className={styles.chevron} aria-hidden>
          {open ? "▾" : "▸"}
        </span>
        <span className={styles.label}>{children ?? title}</span>
      </button>
      {open ? (
        <div className={styles.panel}>
          {detail ? (
            <>
              <p>{detail}</p>
              {!brief && !loading ? (
                <button type="button" onClick={() => void load()}>Write a plain-language summary</button>
              ) : null}
            </>
          ) : null}
          {loading && !brief ? <p className={styles.waiting}>Writing a summary…</p> : null}
          <ErrorNotice error={brief ? null : error} className={styles.fail} source="tasks.brief" intent="task_brief" />
          {brief?.body_md ? (
            <>
              <div dangerouslySetInnerHTML={{ __html: renderLightMd(brief.body_md) }} />
              {brief.source ? (
                <p className={styles.source}>
                  {brief.source === "llm" ? "Written just now" : "From the work record"}
                  {brief.cached ? " · cached" : ""}
                </p>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
