"use client";

import Link from "next/link";
import type { MouseEvent } from "react";
import { displayName, pausedReasonLabel, relativeTime, stageLabel, teamCountLabel, ventureStatusLabel } from "@/lib/format";
import { cx } from "@/lib/cx";
import type { HubVenture } from "./VentureHub";
import styles from "./VentureCard.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

function isPaused(venture: HubVenture): boolean {
  if (venture.paused === true) return true;
  if (venture.paused === false) return false;
  return venture.status?.code === "paused" || venture.stage === "paused";
}

function echoes(name: string, idea: string): boolean {
  const a = name.trim().toLowerCase();
  const b = idea.trim().toLowerCase();
  if (!a || !b) return false;
  const head = b.slice(0, 24);
  return a.startsWith(head) || b.startsWith(a.slice(0, 24));
}

function stoppedLine(venture: HubVenture): string | null {
  const stopped = venture.stopped;
  if (!stopped) return null;
  const parts: string[] = [];
  if (typeof stopped.agents === "number" && stopped.agents > 0) {
    parts.push(`${stopped.agents} ${stopped.agents === 1 ? "agent" : "agents"}`);
  }
  if (typeof stopped.tasks === "number" && stopped.tasks > 0) {
    parts.push(
      `${stopped.tasks} running ${stopped.tasks === 1 ? "task" : "tasks"}`,
    );
  }
  if (parts.length) return `Stopped ${parts.join(" and ")}`;
  if (stopped.onboarding) return "Stopped onboarding";
  return null;
}

function statusKind(venture: HubVenture): "live" | "paused" | "needs" | "setup" {
  if (isPaused(venture)) return "paused";
  const code = venture.status?.code || "";
  if (code === "needs_you") return "needs";
  if (
    code === "live" ||
    code === "watching" ||
    venture.stage === "operate" ||
    venture.stage === "live"
  ) {
    return "live";
  }
  return "setup";
}

function guard(event: MouseEvent) {
  event.preventDefault();
  event.stopPropagation();
  const menu = event.currentTarget.closest("details");
  if (menu) {
    menu.open = false;
    menu.querySelector("summary")?.focus();
  }
}

export function VentureCard({
  venture,
  busy,
  error,
  onPause,
  onResume,
  onDelete,
}: {
  venture: HubVenture;
  busy: boolean;
  error: string;
  onPause: () => void;
  onResume: () => void;
  onDelete: () => void;
}) {
  const name = displayName(venture.name, venture.raw_idea);
  const paused = isPaused(venture);
  const kind = statusKind(venture);
  const stopped = paused ? stoppedLine(venture) : null;
  const updated = relativeTime(venture.updated_at || venture.created_at);
  const team = teamCountLabel(venture.agents?.hired, venture.agents?.working);
  const statusDetail = (venture.status?.detail || "").trim();
  const idea = (venture.raw_idea || "").trim();
  // On a live venture status.detail *is* the roster count, and the name is often
  // derived from the idea — so both would repeat what the card already shows.
  const detail =
    (statusDetail && statusDetail !== team ? statusDetail : "") ||
    (idea && !echoes(name, idea) ? idea : "") ||
    (venture.stage ? `Stage: ${stageLabel(venture.stage)}` : "");

  return (
    <article
      className={cx(styles.card, paused && styles.cardPaused)}
      aria-busy={busy}
      aria-labelledby={`venture-${venture.id}-title`}
    >
      <Link href={`/v/${venture.id}`} className={styles.body}>
        <div className={styles.cardTop}>
          <span
            className={cx(
              styles.status,
              kind === "live" && styles.statusLive,
              kind === "paused" && styles.statusPaused,
              kind === "needs" && styles.statusNeeds,
            )}
          >
            <i aria-hidden />
            {paused ? "Paused" : ventureStatusLabel(venture.stage, venture.status)}
          </span>
        </div>
        <h3 id={`venture-${venture.id}-title`}>{name}</h3>
        {detail ? <p>{detail}</p> : null}
        {venture.agents && <p className={styles.team}>{team}</p>}
        {stopped && <p className={styles.stopped}>{stopped}</p>}
        {paused && venture.paused_reason ? (
          <p className={styles.reason}>{pausedReasonLabel(venture.paused_reason)}</p>
        ) : null}
      </Link>

      <ErrorNotice error={error} className={styles.inlineError} source="venturecard.action" />

      <div className={styles.footer}>
        {updated ? <span className={styles.meta}>Updated {updated}</span> : null}
        <details
          className={styles.actions}
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false;
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.currentTarget.open = false;
              event.currentTarget.querySelector("summary")?.focus();
            }
          }}
        >
          <summary aria-label={`Actions for ${name}`} title={`Actions for ${name}`} data-usage-control="venture_more">
            <span aria-hidden="true">…</span>
          </summary>
          <div className={styles.menu}>
            <button
              type="button"
              className="ghost sm"
              disabled={busy}
              aria-label={paused ? `Resume ${name}` : `Pause ${name}`}
              data-usage-control={paused ? "venture_resume" : "venture_pause"}
              onClick={(event) => {
                guard(event);
                if (paused) onResume();
                else onPause();
              }}
            >
              {busy ? (paused ? "Resuming…" : "Pausing…") : paused ? "Resume" : "Pause"}
            </button>
            <button
              type="button"
              className="ghost sm"
              disabled={busy}
              aria-label={`Delete ${name}`}
              onClick={(event) => {
                guard(event);
                onDelete();
              }}
            >
              Delete venture…
            </button>
          </div>
        </details>
      </div>
    </article>
  );
}
