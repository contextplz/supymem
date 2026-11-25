"use client";

import { OpportunityCard, type Opportunity } from "./OpportunityCard";

type Props = {
  opportunities: Opportunity[];
  busy?: boolean;
  onReject?: (id: string) => void;
  onPromote?: (id: string) => void;
};

export function SlateGrid({ opportunities, busy, onReject, onPromote }: Props) {
  if (!opportunities.length) {
    return (
      <p style={{ color: "var(--fg-1)", textAlign: "center" }}>
        No openings yet. Try Surprise me from the home composer.
      </p>
    );
  }
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
        gap: "var(--s-4)",
      }}
    >
      {opportunities.map((o) => (
        <OpportunityCard
          key={o.id}
          opportunity={o}
          busy={busy}
          onReject={onReject}
          onPromote={onPromote}
        />
      ))}
    </div>
  );
}
