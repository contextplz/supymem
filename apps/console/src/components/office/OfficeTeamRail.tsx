"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { agentStatusHelp } from "@/lib/format";
import { cx } from "@/lib/cx";
import { EmployeePortrait, prettyName, prettyRole } from "./avatars";
import { useOffice } from "./OfficeProvider";
import styles from "./office.module.css";

type LiveKind = "working" | "idle" | "blocked";

function liveKind(state?: string | null, status?: string | null): LiveKind {
  const live = (state || "").toLowerCase();
  if (live === "working") return "working";
  if (live === "blocked") return "blocked";
  if (live === "idle") return "idle";
  const s = (status || "").toLowerCase();
  if (s === "paused") return "blocked";
  return "idle";
}

function liveDotClass(kind: LiveKind): string {
  if (kind === "working") return styles.liveWorking;
  if (kind === "blocked") return styles.liveBlocked;
  return styles.liveIdle;
}

export function OfficeTeamRail() {
  const { ventureId, org, loading, activating, selectedAgent, setSelectedAgent, activateTeam } =
    useOffice();
  const pathname = usePathname();
  const teamActive = Boolean(ventureId && pathname.startsWith(`/v/${ventureId}/org`));
  const [expanded, setExpanded] = useState(teamActive);

  useEffect(() => {
    if (teamActive) setExpanded(true);
  }, [teamActive, pathname]);

  if (!ventureId) return null;

  const agents = org?.agents || [];
  const watching = org?.summary.watching ?? 0;
  const workingFromLive = agents.filter((a) => liveKind(a.work?.live_state) === "working").length;
  const working = org?.summary.working ?? workingFromLive;
  const headingCount = working > 0 ? ` · ${working} working` : "";

  return (
    <div className={styles.teamRail}>
      <button
        type="button"
        className={styles.teamToggle}
        aria-expanded={expanded}
        aria-controls="team-rail-items"
        onClick={() => setExpanded((value) => !value)}
      >
        <span>
          Team
          {headingCount ? <span className={styles.teamCount}>{headingCount}</span> : null}
        </span>
        <span className={cx(styles.teamChevron, expanded && styles.teamChevronExpanded)} aria-hidden="true" />
      </button>
      <div id="team-rail-items" className={styles.teamContents} hidden={!expanded}>
      {loading && !agents.length && <div className={styles.loadingTeam}>Hiring the team…</div>}
      {!loading && !agents.length && (
        <div className={styles.emptyTeam}>Teammates appear after the plan is locked.</div>
      )}
      <div className={styles.teamList}>
        {agents.map((agent) => {
          const name = prettyName(agent.name || agent.role);
          const role = agent.title || prettyRole(agent.role);
          const kind = liveKind(agent.work?.live_state, agent.status);
          const selected = selectedAgent?.id === agent.id;
          const doing = (agent.work?.doing || "").trim();
          return (
            <button
              key={agent.id}
              type="button"
              className={cx(styles.teamRow, selected && styles.teamRowActive)}
              onClick={() => setSelectedAgent(selected ? null : agent)}
              title={doing || agentStatusHelp(agent.status) || role}
            >
              <span className={styles.portraitWrap}>
                <EmployeePortrait seed={agent.id || agent.role || name} size={24} />
                <span className={cx(styles.liveDot, liveDotClass(kind))} aria-label={kind} />
              </span>
              <span className={styles.teamCopy}>
                <span className={styles.teamName}>{name}</span>
                <span className={styles.teamRole}>{role}</span>
                {doing ? <span className={styles.teamDoing}>{doing}</span> : null}
              </span>
            </button>
          );
        })}
      </div>
      {watching > 0 && (
        <button
          type="button"
          className={cx("sm", styles.activateBtn)}
          disabled={activating}
          onClick={() => void activateTeam()}
        >
          {activating ? "Activating…" : `Activate ${watching} watching`}
        </button>
      )}
      </div>
    </div>
  );
}
