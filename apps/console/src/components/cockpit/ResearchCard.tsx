import { ChatAnchor } from "@/components/chat/ChatLink";
import type { ResearchBlock, ValidationTileData } from "@/lib/api-types";
import { ProvenanceChip } from "@/components/primitives/ProvenanceChip";
import { EmptyState } from "@/components/primitives/EmptyState";
import styles from "./cockpit.module.css";

export function ResearchCard({
  ventureId,
  research,
  validation,
  generatedBecause,
}: {
  ventureId: string;
  research?: ResearchBlock;
  validation?: ValidationTileData;
  generatedBecause?: string;
}) {
  const queries = validation?.queries?.filter((q) => q.trim()) || [];
  const href = research?.href || `/v/${ventureId}/research`;
  const why =
    generatedBecause ||
    (validation?.recommendation ? `Verdict leaning ${validation.recommendation}` : null);
  const hasAnything =
    queries.length > 0 ||
    Boolean(why) ||
    Boolean(validation?.demand && validation.demand !== "unknown") ||
    research?.available;

  return (
    <section className={styles.card} aria-labelledby="research-heading">
      <div className={styles.sectionHeader}>
        <h2 id="research-heading">Research</h2>
        <ProvenanceChip
          mode={
            validation?.unvalidated
              ? "projected"
              : queries.length > 0 || research?.available
                ? "live"
                : "no_data"
          }
        />
      </div>
      {!hasAnything ? (
        <EmptyState
          title="No research yet"
          body="Queries will appear here as the team looks things up, not a spinner."
        />
      ) : (
        <>
          {why ? <p className={styles.itemWhy}>{why}</p> : null}
          {validation?.demand && validation.demand !== "unknown" ? (
            <p className={styles.detail}>
              Demand: {validation.demand}
              {validation.confidence != null ? ` · conf ${validation.confidence.toFixed(2)}` : ""}
            </p>
          ) : null}
          {queries.length ? (
            <ol className={styles.queryList}>
              {queries.slice(0, 6).map((query) => (
                <li key={query}>{query}</li>
              ))}
            </ol>
          ) : (
            <p className={styles.empty}>
              {research?.available
                ? "The dossier has the queries and sources, open it."
                : "No queries recorded yet."}
            </p>
          )}
        </>
      )}
      <ChatAnchor href={href} className="ghost sm">
        Open research
      </ChatAnchor>
    </section>
  );
}
