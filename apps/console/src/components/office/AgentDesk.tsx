"use client";

import { useEffect, useState } from "react";
import Link from "@/components/chat/ChatLink";
import { api } from "@/lib/client";
import { agentStatusHelp, agentStatusLabel, relativeTime } from "@/lib/format";
import { agentActivity, EmployeePortrait, prettyName, prettyRole } from "./avatars";
import { useOffice, type AgentWork, type OfficeAgent } from "./OfficeProvider";
import { WorkLines } from "./WorkLines";
import styles from "./office.module.css";

export function AgentDesk() {
  const {
    ventureId,
    selectedAgent,
    setSelectedAgent,
    setMentionPrefix,
    activateTeam,
    activating,
    activateNote,
    org,
  } = useOffice();
  const [deskWork, setDeskWork] = useState<AgentWork | null>(null);

  useEffect(() => {
    if (!selectedAgent || !ventureId) {
      setDeskWork(null);
      return;
    }
    let cancelled = false;
    const load = async () => {
      const r = await api(`/api/ventures/${ventureId}/org/agents/${selectedAgent.id}`);
      if (cancelled || !r.ok) return;
      const data = r.data as { work?: AgentWork; agent?: OfficeAgent };
      if (data.work) setDeskWork(data.work);
    };
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [selectedAgent?.id, ventureId]);

  if (!selectedAgent || !ventureId) return null;

  const name = prettyName(selectedAgent.name || selectedAgent.role);
  const activity = agentActivity(selectedAgent.status);
  const watching = ["quarantined", "shadow"].includes((selectedAgent.status || "").toLowerCase());
  const work = deskWork || selectedAgent.work;
  const log = work?.today || [];

  return (
    <div className={styles.deskOverlay} onClick={() => setSelectedAgent(null)}>
      <aside
        className={styles.deskPanel}
        role="dialog"
        aria-label={`${name} desk`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className={styles.deskHeader}>
          <div className={styles.deskIdentity}>
            <EmployeePortrait
              seed={selectedAgent.id || selectedAgent.role || name}
              size={48}
              className={styles.deskAvatar}
            />
            <div>
              <h2 className={styles.deskName}>{name}</h2>
              <p className={styles.deskRole}>
                {(selectedAgent.title || prettyRole(selectedAgent.role))} · {activity.label}
              </p>
            </div>
          </div>
          <button
            type="button"
            className={styles.deskClose}
            aria-label="Close"
            onClick={() => setSelectedAgent(null)}
          >
            ✕
          </button>
        </div>
        <div className={styles.deskBody}>
          <WorkLines work={work} />
          {selectedAgent.charge && (
            <section>
              <p className={styles.deskLabel}>This desk</p>
              <p className={styles.deskNote}>{selectedAgent.charge}</p>
            </section>
          )}
          <section>
            <p className={styles.deskLabel}>Today</p>
            {log.length === 0 ? (
              <p className={styles.deskNote}>No attributed log lines yet today.</p>
            ) : (
              <ol className={styles.deskLog}>
                {log.map((row, index) => (
                  <li key={`${row.at || "row"}-${index}`}>
                    <span className={styles.deskLogMeta}>
                      {relativeTime(row.at)} · {row.kind}
                      {row.status ? ` · ${row.status}` : ""}
                    </span>
                    <span className={styles.deskLogText}>{row.text}</span>
                  </li>
                ))}
              </ol>
            )}
          </section>
          <section>
            <p className={styles.deskLabel}>Status</p>
            <p className={styles.deskNote}>
              {agentStatusLabel(selectedAgent.status)}. {agentStatusHelp(selectedAgent.status)}
            </p>
          </section>
          {activateNote && (
            <p className={styles.deskNote} style={{ color: "var(--fg-2)" }}>
              {activateNote}
            </p>
          )}
          <div className={styles.deskActions}>
            <button
              type="button"
              className="primary"
              onClick={() => {
                setMentionPrefix(`@${name} `);
                setSelectedAgent(null);
              }}
            >
              Message {name}
            </button>
            {watching && (
              <button type="button" disabled={activating} onClick={() => void activateTeam()}>
                {activating ? "Activating…" : "Activate watching agents"}
              </button>
            )}
            <Link href={`/v/${ventureId}/tasks`} onClick={() => setSelectedAgent(null)}>
              Shared task board
            </Link>
            <p className={styles.deskNote}>
              {org?.summary.working ?? 0} working · {org?.summary.watching ?? 0} watching ·{" "}
              {org?.summary.hired ?? 0} hired
            </p>
          </div>
        </div>
      </aside>
    </div>
  );
}
