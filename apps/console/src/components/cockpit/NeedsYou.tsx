"use client";

import { ChatAnchor } from "@/components/chat/ChatLink";

import { useEffect, useRef, useState } from "react";
import { EmptyState } from "@/components/primitives/EmptyState";
import styles from "./cockpit.module.css";

export type NeedsYouItem = {
  id: string;
  title: string;
  timeEstimate?: string;
  unblocks?: string;
  href?: string;
  actionLabel?: string;
  steps?: Array<{ text?: string }>;
};

export function NeedsYou({
  items,
  onRequestAlternative,
}: {
  items?: NeedsYouItem[];
  onRequestAlternative?: (id: string, reason: string) => Promise<string>;
}) {
  const visibleItems = items || [];
  const focusedHash = useRef("");
  useEffect(() => {
    const focusTask = () => {
      const id = window.location.hash.slice(1);
      const target = id.startsWith("attention-") ? document.getElementById(id) : null;
      if (target && focusedHash.current !== id) {
        target.scrollIntoView({ block: "center" });
        target.focus({ preventScroll: true });
        focusedHash.current = id;
      }
    };
    focusTask();
    window.addEventListener("hashchange", focusTask);
    return () => window.removeEventListener("hashchange", focusTask);
  }, [items]);
  const [explaining, setExplaining] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  return (
    <section className={styles.card} aria-labelledby="needs-you-heading">
      <div className={styles.sectionHeader}>
        <h2 id="needs-you-heading">Needs you</h2>
        <span className={styles.detail}>{visibleItems.length}</span>
      </div>
      {visibleItems.length ? (
        <>
          <p className={styles.sectionLede}>
            Only you can do these. The team waits until they are done.
          </p>
          <div>
            {visibleItems.map((item) => (
              <div className={styles.need} key={item.id} id={`attention-${item.id}`} tabIndex={-1}>
                <p className={styles.needTitle}>{item.title}</p>
                {(item.timeEstimate || item.unblocks) && (
                  <p className={styles.detail}>
                    {[item.timeEstimate, item.unblocks].filter(Boolean).join(" · ")}
                  </p>
                )}
                {item.steps && item.steps.length > 0 && (
                  <ol className={styles.needSteps}>
                    {item.steps.slice(0, 3).map((step, index) => (
                      <li key={`${item.id}-${index}`}>{step.text}</li>
                    ))}
                  </ol>
                )}
                {item.href && (
                  <ChatAnchor href={item.href} className="ghost sm">
                    {item.actionLabel || "Do it now"}
                  </ChatAnchor>
                )}
                {onRequestAlternative && item.id !== "pending-approvals" && (
                  <>
                    <button type="button" className="ghost sm" onClick={() => {
                      setExplaining(explaining === item.id ? null : item.id);
                      setReason("");
                      setNote("");
                    }}>
                      I can’t do this right now
                    </button>
                    {explaining === item.id && (
                      <div className={styles.alternative}>
                        <label htmlFor={`alternative-${item.id}`}>
                          Why is this blocked? <span className={styles.detail}>(optional)</span>
                        </label>
                        <textarea
                          id={`alternative-${item.id}`}
                          value={reason}
                          onChange={(event) => setReason(event.target.value)}
                          placeholder="For example: I don’t have this account yet."
                        />
                        <button type="button" disabled={busy} onClick={async () => {
                          setBusy(true);
                          setNote("");
                          const message = await onRequestAlternative(item.id, reason);
                          setBusy(false);
                          setNote(message);
                          setExplaining(null);
                        }}>
                          {busy ? "Finding another way…" : "Find another way"}
                        </button>
                      </div>
                    )}
                  </>
                )}
              </div>
            ))}
            {note && <p className={styles.detail} role="status">{note}</p>}
          </div>
        </>
      ) : (
        <EmptyState
          title="Nothing needs you"
          body="That’s the point, the team is moving without a pile of chores. Pause anytime from You stay in control."
        />
      )}
    </section>
  );
}
