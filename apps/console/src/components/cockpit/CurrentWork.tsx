import { plainJob } from "@/lib/jobCopy";
import Link from "@/components/chat/ChatLink";
import { EmptyState } from "@/components/primitives/EmptyState";
import { prettyName, prettyRole } from "@/components/office/avatars";
import { TaskBrief } from "@/components/tasks/TaskBrief";
import styles from "./cockpit.module.css";

export type CurrentWorkItem = {
  id: string;
  title: string;
  owner_name?: string | null;
  owner_role?: string | null;
  status?: string;
  runnable?: boolean;
  last_error?: string | null;
};

export function CurrentWork({
  ventureId,
  items,
  nextTitle,
  autopilotOn,
}: {
  ventureId: string;
  items?: CurrentWorkItem[];
  nextTitle?: string | null;
  autopilotOn?: boolean;
}) {
  const rows = items || [];

  return (
    <section className={styles.card} aria-labelledby="current-work-heading">
      <div className={styles.sectionHeader}>
        <h2 id="current-work-heading">Being done now</h2>
        <Link href={`/v/${ventureId}/now`} data-usage-control="work_all_current" className="ghost sm">
          All current work
        </Link>
      </div>
      {rows.length ? (
        <div className={styles.itemList}>
          {rows.map((item) => {
            const owner = item.owner_name
              ? `${prettyName(item.owner_name)}${
                  item.owner_role ? ` · ${prettyRole(item.owner_role)}` : ""
                }`
              : item.owner_role
                ? prettyRole(item.owner_role)
                : "Unassigned";
            return (
              <div className={styles.slateItem} key={item.id}>
                <div>
                  <TaskBrief
                    ventureId={ventureId}
                    taskId={item.id}
                    title={item.title}
                    className={styles.itemTitle}
                  />
                  <p className={styles.itemWhy}>{owner}</p>
                  {item.last_error && !item.last_error.startsWith("needs_you:") ? (
                    <p className={styles.detail}>{plainJob(item).body}</p>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <EmptyState
          title={autopilotOn ? "Nothing in flight yet" : "Work is paused"}
          body={
            autopilotOn
              ? nextTitle
                ? `Next up: ${nextTitle}. Autopilot will pick it up.`
                : "The queue is empty. Replan if you want a new cut of work."
              : "Resume the venture and the team will take the next open task."
          }
        />
      )}
    </section>
  );
}
