"use client";

import { useState } from "react";
import { downloadFile } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export type VerdictBriefData = {
  verdict: "Strong" | "Promising" | "Mixed" | "Weak" | "Don't" | "Preliminary" | "Unavailable";
  confidence: number | null;
  strongestFor?: string;
  strongestAgainst?: string;
  changesVerdict?: string;
  recommendation: string;
  competitor_count?: number;
  demand_signal_count?: number;
  differentiation_score?: number;
  summary?: string;
  mode?: string;
  reason?: string;
  provider?: string;
  competitors?: { name?: string; website?: string; note?: string }[];
  demand_signals?: { claim?: string; source?: string }[];
  dossier_download?: string;
  dossier_engine?: string;
  elapsed_ms?: number;
  depth?: string;
  sources?: { title?: string; url?: string }[];
  evidence_dimensions?: Record<string, {
    status: "unknown" | "supporting_leads" | "challenging_leads" | "mixed_leads";
    support?: { url?: string; finding?: string }[];
    challenge?: { url?: string; finding?: string }[];
    unknown?: string;
  }>;
};

const dimensionLabels: Record<string, string> = {
  problem_validation: "Problem validation", customer_demand: "Customer demand", competition: "Competition",
  pricing: "Pricing", willingness_to_pay: "Willingness to pay", market_size: "Market size", risks: "Risks",
};
const evidenceStatus = { unknown: "Unknown", supporting_leads: "Supporting leads",
  challenging_leads: "Challenging leads", mixed_leads: "Mixed leads" };

export function VerdictBrief({
  data,
  onDecision,
  onRetryResearch,
  retrying,
}: {
  data: VerdictBriefData;
  onDecision: (decision: "proceed" | "dont_proceed") => void;
  onRetryResearch?: () => void;
  retrying?: boolean;
}) {
  const [downloading, setDownloading] = useState(false);
  const blocked = data.verdict === "Unavailable" || (data.mode && data.mode !== "live");
  const preliminary = data.verdict === "Preliminary";
  const proceedLabel = blocked
    ? "Skip without a market read"
    : data.verdict === "Don't"
      ? "Build it anyway"
      : "Yes, proceed";
  const forLine =
    data.strongestFor ||
    (data.demand_signal_count != null
      ? data.demand_signal_count ? `${data.demand_signal_count} unverified demand lead${data.demand_signal_count === 1 ? "" : "s"} from research` : "No explicit customer-demand lead was found in this scan."
      : "Customer demand remains unmeasured.");
  const againstLine =
    data.strongestAgainst ||
    (data.competitor_count ? `${data.competitor_count} possible competitors need comparison.` : "No contrary market evidence was established by this scan.");
  const changesLine =
    data.changesVerdict ||
    "New evidence that changes demand, crowding, or how different this is.";
  const live = data.mode === "live";
  const competitors = (data.competitors || []).slice(0, 5);
  const signals = (data.demand_signals || []).slice(0, 4);
  const sources = (data.sources || []).slice(0, 5);

  return (
    <section id="validation" style={{ padding: "var(--s-4)", border: "1px solid var(--line)", borderRadius: "var(--r-3)" }}>
      <div className="eyebrow">{blocked || preliminary ? "Research status" : "Verdict"}</div>
      <h2 style={{ margin: "6px 0" }}>{blocked ? "DID NOT RUN" : data.verdict.toUpperCase()}</h2>
      {preliminary && <p className="muted">This is an unverified search scan, not a market verdict. Missing evidence means the question is open.</p>}
      {data.summary && <p className="muted">{data.summary}</p>}
      {blocked && (
        <ErrorNotice error="The live web scan did not complete." style={{ color: "var(--danger, #b33)" }} source="verdictbrief.scan_blocked">
          We did not invent a market score. The live web scan did not complete
          {data.reason ? ` (${data.reason})` : ""}.
          {data.provider ? ` Last provider: ${data.provider}.` : ""} Retry now, DuckDuckGo is used
          when no Firecrawl key is set.
        </ErrorNotice>
      )}
      {!blocked && !live && (
        <ErrorNotice error="Live web research did not land." style={{ color: "var(--danger, #b33)" }} source="verdictbrief.no_live">
          Live web research did not land{data.reason ? ` (${data.reason})` : ""}.
        </ErrorNotice>
      )}
      <p>
        <strong>For:</strong> {forLine}
      </p>
      <p>
        <strong>Against:</strong> {againstLine}
      </p>
      <p>
        <strong>What changes it:</strong> {changesLine}
      </p>
      {!blocked && !preliminary && data.confidence != null && Number.isFinite(data.confidence) && (
      <p className="muted">
        Confidence {Math.round(data.confidence * 100)}%
        {data.competitor_count != null && ` · ${data.competitor_count} competitors`}
        {data.demand_signal_count != null && ` · ${data.demand_signal_count} demand signals`}
        {data.differentiation_score != null &&
          ` · differentiation ${Math.round(data.differentiation_score * 100)}%`}
      </p>
      )}
      {preliminary && <p className="muted">{data.competitor_count ?? 0} possible competitors · {data.demand_signal_count ?? 0} explicit demand leads. These counts are research coverage, not a score of the idea.</p>}
      {(data.provider || data.dossier_engine || typeof data.elapsed_ms === "number") && (
        <p className="muted" style={{ marginTop: "0.5rem", fontSize: "0.85rem" }}>
          {data.depth === "snippet_scan" || !data.dossier_engine
            ? "This is a live title/snippet scan"
            : "Research engine"}
          {": "}
          {(data.provider || data.dossier_engine || "unknown").replace(/_/g, " ")}
          {typeof data.elapsed_ms === "number" ? ` in ${(data.elapsed_ms / 1000).toFixed(1)}s` : ""}
          {data.depth === "snippet_scan"
            ? ", four web queries, no page fetch. Open Research for the written report."
            : "."}
        </p>
      )}
      <p>{data.recommendation}</p>

      {data.evidence_dimensions && <section aria-label="Evidence by research question" style={{ marginTop: "1rem" }}>
        <h3>Evidence and open questions</h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 15rem), 1fr))", gap: "0.75rem" }}>
          {Object.entries(dimensionLabels).map(([key, title]) => {
            const item = data.evidence_dimensions?.[key];
            return <article key={key} style={{ border: "1px solid var(--line)", borderRadius: "var(--r-2)", padding: "0.75rem" }}>
              <h4 style={{ margin: "0 0 0.4rem" }}>{title}</h4>
              <p style={{ margin: "0 0 0.4rem" }}><strong>{item ? evidenceStatus[item.status] || "Unknown" : "Unknown"}</strong></p>
              <p className="muted" style={{ margin: 0 }}>{item?.unknown || "This question needs more evidence."}</p>
            </article>;
          })}
        </div>
      </section>}

      {competitors.length > 0 && (
        <div style={{ marginTop: "1rem" }}>
          <h3 style={{ margin: "0 0 0.4rem", fontSize: "0.95rem" }}>Competitors seen online</h3>
          <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
            {competitors.map((c) => (
              <li key={`${c.name}-${c.website}`} style={{ marginBottom: 6 }}>
                <strong>{c.name || "Unnamed"}</strong>
                {c.website && (
                  <>
                    {" · "}
                    <a href={c.website} target="_blank" rel="noreferrer">
                      {c.website.replace(/^https?:\/\//, "").slice(0, 48)}
                    </a>
                  </>
                )}
                {c.note && <div className="muted" style={{ fontSize: "0.85rem" }}>{c.note}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {signals.length > 0 && (
        <div style={{ marginTop: "1rem" }}>
          <h3 style={{ margin: "0 0 0.4rem", fontSize: "0.95rem" }}>Unverified demand leads</h3>
          <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
            {signals.map((s, i) => (
              <li key={s.source || i} style={{ marginBottom: 6 }}>
                {s.claim}
                {s.source && (
                  <>
                    {" "}
                    <a href={s.source} target="_blank" rel="noreferrer" className="muted">
                      source
                    </a>
                  </>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {sources.length > 0 && (
        <div style={{ marginTop: "1rem" }}>
          <h3 style={{ margin: "0 0 0.4rem", fontSize: "0.95rem" }}>Sources</h3>
          <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
            {sources.map((s) => (
              <li key={s.url || s.title}>
                {s.url ? (
                  <a href={s.url} target="_blank" rel="noreferrer">
                    {s.title || s.url}
                  </a>
                ) : (
                  s.title
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: "1rem" }}>
        {onRetryResearch ? (
          <button className="primary" type="button" disabled={retrying} onClick={() => onRetryResearch()}>
            {retrying ? "Scanning the web…" : "Retry live research"}
          </button>
        ) : null}
        {data.dossier_download && (
          <button
            className="chip"
            type="button"
            disabled={downloading}
            onClick={() => {
              setDownloading(true);
              void downloadFile(
                data.dossier_download!,
                `validation-research-${data.dossier_engine || "dossier"}.md`,
              ).finally(() => setDownloading(false));
            }}
          >
            {downloading ? "Downloading…" : "Download research dossier"}
          </button>
        )}
        <button
          className={blocked ? "chip" : "primary"}
          type="button"
          onClick={() => onDecision("proceed")}
        >
          {proceedLabel}
        </button>
        <button className="chip" type="button" onClick={() => onDecision("dont_proceed")}>
          Don&apos;t proceed
        </button>
      </div>
    </section>
  );
}
