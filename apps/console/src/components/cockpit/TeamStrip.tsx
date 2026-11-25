import Link from "@/components/chat/ChatLink";
import {
  EmployeePortrait,
  prettyName,
  prettyRole,
} from "@/components/office/avatars";
import { EmptyState } from "@/components/primitives/EmptyState";
import { isHeartbeatStale } from "@/components/primitives/Heartbeat";
import type { AgentLiveItem, OrgAgent } from "@/lib/api-types";
import { cx } from "@/lib/cx";
import styles from "./cockpit.module.css";

export function TeamStrip({
  ventureId,
  agents,
  live,
}: {
  ventureId: string;
  agents?: OrgAgent[];
  live?: AgentLiveItem[];
}) {
  const rows = agents || [];
  const liveById = new Map((live || []).map((item) => [item.agent_id, item]));

  return (
    <section className={styles.card} aria-labelledby="team-strip-heading">
      <div className={styles.sectionHeader}>
        <h2 id="team-strip-heading">Team</h2>
        <Link href={`/v/${ventureId}/org`} className="ghost sm">
          Full team
        </Link>
      </div>
      {rows.length === 0 ? (
        <EmptyState
          title="No agents yet"
          body="They're created when your plan is approved."
        />
      ) : (
        <div className={styles.teamStrip}>
          {rows.map((agent) => {
            const liveRow = liveById.get(agent.id);
            const liveWorking =
              (liveRow?.state || "").toLowerCase() === "working" &&
              !isHeartbeatStale(liveRow?.last_heartbeat);
            const wasWorking = (liveRow?.state || "").toLowerCase() === "working";
            const stale = wasWorking && isHeartbeatStale(liveRow?.last_heartbeat);
            const blocked = liveRow?.state === "blocked" || stale;
            const doing = blocked
              ? liveRow?.blocked_reason || "stopped, heartbeat went quiet"
              : liveWorking
                ? liveRow?.current_task_desc || agent.work?.doing || "working"
                : "on the roster";
            const seed = agent.name || agent.id;
            return (
              <Link
                key={agent.id}
                href={`/v/${ventureId}/org`}
                className={styles.teammate}
              >
                <EmployeePortrait seed={seed} size={36} />
                <div>
                  <p className={styles.mateName}>{prettyName(agent.name || agent.role || "Teammate")}</p>
                  <p className={cx(styles.mateDoing, blocked && styles.mateStopped)}>
                    {prettyRole(agent.role || "")} · {doing}
                  </p>
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </section>
  );
}
