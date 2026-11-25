"use client";

type Evidence = { claim?: string; source?: string; kind?: string };

export type Opportunity = {
  id: string;
  title: string;
  one_liner: string;
  first_customer: string;
  wedge: string;
  why_now: string;
  kill_criteria: string;
  effort_days: number;
  evidence?: Evidence[];
  evidence_stamp?: string;
  status?: string;
};

type Props = {
  opportunity: Opportunity;
  busy?: boolean;
  onReject?: (id: string) => void;
  onPromote?: (id: string) => void;
};

export function OpportunityCard({ opportunity, busy, onReject, onPromote }: Props) {
  return (
    <article
      style={{
        border: "1px solid var(--line)",
        borderRadius: "var(--r-3)",
        padding: "var(--s-4)",
        background: "var(--bg-2)",
        display: "grid",
        gap: "var(--s-3)",
        minWidth: 0,
      }}
    >
      <header>
        <h3 style={{ margin: 0, font: "var(--t-title)", color: "var(--fg-0)" }}>
          {opportunity.title}
        </h3>
        <p style={{ margin: "6px 0 0", color: "var(--fg-1)", font: "var(--t-body)" }}>
          {opportunity.one_liner}
        </p>
      </header>

      <dl
        style={{
          margin: 0,
          display: "grid",
          gap: 8,
          font: "var(--t-meta)",
          color: "var(--fg-1)",
        }}
      >
        <div>
          <dt style={{ color: "var(--fg-2)" }}>First customer</dt>
          <dd style={{ margin: 0, color: "var(--fg-0)" }}>{opportunity.first_customer}</dd>
        </div>
        <div>
          <dt style={{ color: "var(--fg-2)" }}>Wedge · ~{opportunity.effort_days}d</dt>
          <dd style={{ margin: 0, color: "var(--fg-0)" }}>{opportunity.wedge}</dd>
        </div>
        <div>
          <dt style={{ color: "var(--fg-2)" }}>Why now</dt>
          <dd style={{ margin: 0, color: "var(--fg-0)" }}>{opportunity.why_now}</dd>
        </div>
        <div>
          <dt style={{ color: "var(--fg-2)" }}>Would be wrong if…</dt>
          <dd style={{ margin: 0, color: "var(--fg-0)" }}>{opportunity.kill_criteria}</dd>
        </div>
      </dl>

      {opportunity.evidence && opportunity.evidence.length > 0 && (
        <ul style={{ margin: 0, paddingLeft: 16, color: "var(--fg-1)", font: "var(--t-meta)" }}>
          {opportunity.evidence.slice(0, 3).map((e, i) => (
            <li key={i}>
              {e.claim}
              {e.source ? (
                <span style={{ color: "var(--fg-2)" }}> · {e.source}</span>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      {opportunity.evidence_stamp && (
        <div className="composer-hint">{opportunity.evidence_stamp}</div>
      )}

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button
          type="button"
          className="primary"
          disabled={busy || opportunity.status === "promoted"}
          onClick={() => onPromote?.(opportunity.id)}
        >
          Build this
        </button>
        <button
          type="button"
          className="chip"
          disabled={busy || opportunity.status === "rejected"}
          onClick={() => onReject?.(opportunity.id)}
        >
          Not this
        </button>
      </div>
    </article>
  );
}
