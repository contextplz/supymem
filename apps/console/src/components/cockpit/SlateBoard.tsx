"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/client";
import { ProvenanceChip, type ProvenanceMode } from "@/components/primitives/ProvenanceChip";
import { EmptyState } from "@/components/primitives/EmptyState";
import { TaskBrief } from "@/components/tasks/TaskBrief";
import { asNumber, asString, asStringList, isRecord, provenanceMode } from "./parse";
import { cx } from "@/lib/cx";
import styles from "./cockpit.module.css";

export type SlateItem = {
  id: string;
  rank?: number;
  title: string;
  why_now?: string;
  evidence?: string[];
  expected_outcome?: string;
  expected_metric?: string;
  expected_delta?: number;
  capability_id?: string;
  assignee_agent_id?: string;
  effort?: string;
  cost_usd_estimate?: number;
  autonomy?: string;
  blocked_by?: string[];
  phase?: number;
  origin?: string;
  kill_criteria?: string;
  provenance?: ProvenanceMode;
  runnable?: boolean;
  owner_name?: string;
};

function effortLabel(effort?: string): string | null {
  if (effort === "minutes") return "~20m";
  if (effort === "hours") return "~1h";
  if (effort === "days") return "~1d";
  return effort || null;
}

function slateFrom(value: unknown): { items: SlateItem[]; generatedBecause?: string } {
  if (!isRecord(value)) return { items: [] };
  const nested = isRecord(value.slate) ? value.slate : null;
  const raw = Array.isArray(value.items)
    ? value.items
    : Array.isArray(nested?.items)
      ? nested.items
      : Array.isArray(value.tasks)
        ? value.tasks
        : [];
  const generatedBecause =
    asString(value.generated_because) || (nested ? asString(nested.generated_because) : undefined);
  const items = raw.flatMap((row, index) => {
    if (!isRecord(row)) return [];
    const title = asString(row.title);
    if (!title) return [];
    const blocked = asStringList(row.blocked_by);
    const itemProvenance: ProvenanceMode = blocked.length
      ? "simulated"
      : provenanceMode(row.provenance) === "no_data"
        ? "live"
        : provenanceMode(row.provenance);
    return [
      {
        id: asString(row.id) || `slate-${asNumber(row.rank) ?? index + 1}`,
        rank: asNumber(row.rank) ?? index + 1,
        title,
        why_now: asString(row.why_now),
        evidence: asStringList(row.evidence),
        expected_outcome: asString(row.expected_outcome),
        expected_metric: asString(row.expected_metric),
        expected_delta: asNumber(row.expected_delta),
        capability_id: asString(row.capability_id),
        assignee_agent_id: asString(row.assignee_agent_id),
        effort: asString(row.effort),
        cost_usd_estimate: asNumber(row.cost_usd_estimate),
        autonomy: asString(row.autonomy),
        blocked_by: blocked,
        phase: asNumber(row.phase),
        origin: asString(row.origin),
        kill_criteria: asString(row.kill_criteria),
        provenance: itemProvenance,
        runnable: row.runnable === false ? false : blocked.length === 0,
        owner_name: asString(row.owner_name),
      },
    ];
  });
  return { items, generatedBecause };
}

export function SlateBoard({
  ventureId,
  items,
  generatedBecause,
  onRunTask,
  onReplan,
  refreshKey = 0,
  replanning = false,
}: {
  ventureId?: string;
  items?: SlateItem[];
  generatedBecause?: string;
  onRunTask?: (taskId: string) => void;
  onReplan?: () => void | Promise<void>;
  refreshKey?: number;
  replanning?: boolean;
}) {
  const [fetchedItems, setFetchedItems] = useState<SlateItem[]>([]);
  const [fetchedBecause, setFetchedBecause] = useState<string | undefined>();
  const [loading, setLoading] = useState(Boolean(ventureId && !items));
  const [openEvidence, setOpenEvidence] = useState<string | null>(null);
  const visibleItems = (items || fetchedItems).slice(0, 5);
  const why = generatedBecause || fetchedBecause;

  useEffect(() => {
    if (!ventureId || items) return;
    let cancelled = false;
    setLoading(true);
    const started = Date.now();
    void api(`/api/ventures/${ventureId}/slate`).then((response) => {
      if (cancelled) return;
      if (response.ok) {
        const parsed = slateFrom(response.data);
        setFetchedItems(parsed.items);
        setFetchedBecause(parsed.generatedBecause);
      }
      const wait = Math.max(0, 400 - (Date.now() - started));
      window.setTimeout(() => {
        if (!cancelled) setLoading(false);
      }, wait);
    });
    const fallback = window.setTimeout(() => {
      if (!cancelled) setLoading(false);
    }, 8000);
    return () => {
      cancelled = true;
      window.clearTimeout(fallback);
    };
  }, [ventureId, items, refreshKey]);

  return (
    <section className={`${styles.card} ${styles.slateCard}`} aria-labelledby="next-five-heading">
      <div className={styles.sectionHeader}>
        <h2 id="next-five-heading">Next five</h2>
        <ProvenanceChip mode={visibleItems.length ? "live" : "no_data"} />
        {onReplan && (
          <button
            type="button"
            className={cx("ghost", "sm")}
            disabled={replanning}
            onClick={() => void onReplan()}
          >
            {replanning ? "Changing…" : "Change"}
          </button>
        )}
      </div>
      {loading && !replanning ? (
        <p className={styles.empty}>Working out what&apos;s next…</p>
      ) : visibleItems.length ? (
        <div className={styles.itemList}>
          {visibleItems.map((item, index) => {
            const blocked = (item.blocked_by || []).length > 0;
            const evidenceCount = item.evidence?.length ?? 0;
            return (
              <div
                className={cx(styles.slateItem, blocked && styles.slateItemBlocked)}
                key={item.id}
              >
                <span className={styles.rank}>{item.rank ?? index + 1}</span>
                <div>
                  {ventureId ? (
                    <TaskBrief
                      ventureId={ventureId}
                      taskId={item.id}
                      title={item.title}
                      className={styles.itemTitle}
                    />
                  ) : (
                    <p className={styles.itemTitle}>{item.title}</p>
                  )}
                  {item.why_now ? <p className={styles.itemWhy}>{item.why_now}</p> : null}
                  {item.expected_outcome ? (
                    <p className={styles.itemOutcome}>→ {item.expected_outcome}</p>
                  ) : null}
                  <div className={styles.itemMeta}>
                    <ProvenanceChip mode={blocked ? "simulated" : item.provenance || "live"} />
                    {blocked ? (
                      <span>blocked{item.blocked_by?.[0] ? ` Not recorded ${item.blocked_by[0]}` : ""}</span>
                    ) : (
                      <>
                        {effortLabel(item.effort) ? <span>{effortLabel(item.effort)}</span> : null}
                        {item.owner_name ? <span>{item.owner_name}</span> : null}
                      </>
                    )}
                    {evidenceCount > 0 && (
                      <button
                        type="button"
                        className="ghost sm"
                        onClick={() =>
                          setOpenEvidence((id) => (id === item.id ? null : item.id))
                        }
                      >
                        ▸ evidence · {evidenceCount} source{evidenceCount === 1 ? "" : "s"}
                      </button>
                    )}
                    {onRunTask && item.runnable && !blocked && (
                      <button type="button" className="ghost sm" onClick={() => onRunTask(item.id)}>
                        Run
                      </button>
                    )}
                  </div>
                  {openEvidence === item.id && evidenceCount > 0 && (
                    <div className={styles.evidence}>
                      <p className={styles.detail}>Evidence this item rests on</p>
                      <ul>
                        {(item.evidence || []).map((ref) => (
                          <li key={ref}>{ref}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <EmptyState
          title="No next steps yet"
          body="Click Change to generate a fresh work queue, or finish the plan first."
        />
      )}
      {why ? <p className={styles.whyFive}>▸ why these five, {why}</p> : null}
    </section>
  );
}
