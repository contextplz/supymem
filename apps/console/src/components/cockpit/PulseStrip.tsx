import { ProvenanceChip, type ProvenanceMode } from "@/components/primitives/ProvenanceChip";
import { Heartbeat, isHeartbeatStale } from "@/components/primitives/Heartbeat";
import { EmptyState } from "@/components/primitives/EmptyState";
import { MoneyHeader } from "./MoneyHeader";
import styles from "./cockpit.module.css";

export function PulseStrip({
  earned,
  chargeCount,
  moneyMode,
  currency,
  sparkValues,
  setupHref,
  interest,
  interestMode,
  workingCount,
  agentNames,
  lastHeartbeat,
  agentsReady,
}: {
  earned?: number | null;
  chargeCount?: number | null;
  moneyMode: ProvenanceMode;
  currency?: string;
  sparkValues?: number[];
  setupHref?: string;
  interest?: number | null;
  interestMode: ProvenanceMode;
  workingCount?: number | null;
  agentNames: string[];
  lastHeartbeat?: string | null;
  agentsReady: boolean;
}) {
  // Agents merely existing is not evidence they are running. Without a fresh
  // heartbeat we cannot observe them, so the claim is "no data", not "live".
  const observed = agentsReady && !isHeartbeatStale(lastHeartbeat);
  const unnamed = workingCount != null ? Math.max(0, workingCount - agentNames.length) : 0;

  return (
    <div className={styles.pulse}>
      <MoneyHeader
        earned={earned}
        chargeCount={chargeCount}
        provenance={moneyMode}
        currency={currency}
        sparkValues={sparkValues}
        setupHref={setupHref}
      />
      <section className={styles.pulseCell} aria-label="Demand">
        <p className={styles.eyebrow}>Interest</p>
        {interestMode === "no_data" || interest == null ? (
          <EmptyState title="○ no data" body="Waitlist signups will show here once the site is up." />
        ) : (
          <>
            <p className={styles.stat}>{interest} interested</p>
            <p className={styles.detail}>
              {interestMode === "simulated"
                ? "Waitlist signups for this venture, not sent to a model."
                : "People who signed the waitlist"}
            </p>
          </>
        )}
        <ProvenanceChip mode={interestMode} />
      </section>
      <section className={styles.pulseCell} aria-label="Team at work">
        <p className={styles.eyebrow}>Team</p>
        {!agentsReady ? (
          <EmptyState
            title="No agents yet"
            body="They're created when your plan is approved."
          />
        ) : (
          <>
            <p className={styles.stat}>
              {workingCount == null
                ? "○"
                : `${workingCount} agent${workingCount === 1 ? "" : "s"} ${
                    observed ? "working" : "on the roster"
                  }`}
            </p>
            {agentNames.length ? (
              <p className={styles.agentNames}>
                {agentNames.join(", ")}
                {unnamed > 0 ? ` +${unnamed} more` : ""}
              </p>
            ) : (
              <p className={styles.detail}>Hired, not yet named in this view</p>
            )}
            <Heartbeat at={lastHeartbeat} />
          </>
        )}
        <ProvenanceChip mode={observed ? "live" : "no_data"} />
      </section>
    </div>
  );
}
