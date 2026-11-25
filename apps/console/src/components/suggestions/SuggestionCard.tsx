"use client";

import { ChatAnchor } from "@/components/chat/ChatLink";
import { DECISION_LABEL, suggestionPlan, type Decision, type Suggestion } from "@/lib/suggestions";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "./suggestions.module.css";

/** One offer: what Supymem will do, why now, what it costs, and the three choices. */
export function SuggestionCard({ item, busy, error, pendingDecision, confirmedDecision, onRetry, onDecide, onDetails, showVenture, hideSave, reviewHref }: {
  item: Suggestion;
  busy?: boolean;
  error?: string;
  pendingDecision?: Decision;
  confirmedDecision?: Decision;
  onRetry?: () => void;
  onDecide: (id: string, decision: Decision) => void;
  onDetails?: (item: Suggestion) => void;
  showVenture?: boolean;
  hideSave?: boolean;
  reviewHref?: string;
}) {
  const decisions: Decision[] = hideSave ? ["accept", "dismiss"] : ["accept", "save", "dismiss"];
  const browserReview = item.action_kind === "browser.task.prepare";
  const plan = suggestionPlan(item);
  return (
    <article className={styles.card} aria-busy={busy || undefined} aria-label={item.title}>
      {showVenture && item.venture_name ? <p className={styles.venture}>{item.venture_name}</p> : null}
      <h3 className={styles.title}>{item.title}</h3>
      {item.why ? <p className={styles.why}>{item.why}</p> : null}
      {plan ? <p className={styles.plan}><strong>What I will do:</strong> {plan}</p> : null}
      <p className={styles.meta}>
        {item.requires_mac ? <span className={styles.chip}>You review it on your Mac first</span> : null}
        {onDetails ? <button type="button" className={styles.link} onClick={() => onDetails(item)}>See details</button> : null}
      </p>
      <div className={styles.buttons}>
        {reviewHref ? <ChatAnchor href={reviewHref}>Open this venture to review</ChatAnchor> : decisions.map((d) => (
          <button
            key={d}
            type="button"
            disabled={busy || !!confirmedDecision}
            title={busy && !confirmedDecision ? "Waiting for this decision to finish" : undefined}
            data-usage-control={d === "accept" ? "suggestions_accept" : d === "save" ? "suggestions_save" : "suggestions_dismiss"}
            className={d === "accept" ? styles.primary : d === "save" ? styles.secondary : styles.quiet}
            onClick={() => onDecide(item.id, d)}
          >
            {browserReview && d === "accept" ? confirmedDecision === d ? "Prepared for review" : busy && d === (pendingDecision || "accept") ? "Preparing review…" : "Prepare for review"
              : confirmedDecision === d ? ({ accept: "Approved", save: "Saved", dismiss: "Dismissed" }[d])
              : busy && d === (pendingDecision || "accept") ? ({ accept: "Approving…", save: "Saving…", dismiss: "Dismissing…" }[d]) : DECISION_LABEL[d]}
          </button>
        ))}
      </div>
      {confirmedDecision ? <p role="status">{browserReview && confirmedDecision === "accept" ? "Prepared for review; nothing has run" : { accept: "Approved", save: "Saved", dismiss: "Dismissed" }[confirmedDecision]}</p> : null}
      <ErrorNotice error={error} source="suggestions.card.error" className={styles.error} />
      {error && onRetry ? <button type="button" disabled={busy} onClick={onRetry}>Retry</button> : null}
    </article>
  );
}
